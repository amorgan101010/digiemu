//! Replay a recorded run of the live emulator in this core and check it.
//!
//!     cfreplay TRACE [--seeds OUT] [--reps N]
//!
//! Pass 1 follows the reference engine block by block with the interpreter,
//! feeds it every recorded hook and host write, and compares the whole CPU
//! state at the end of every step. Every read the host made of guest memory
//! is compared with this core's memory at that moment, and all of memory is
//! compared at the end. It also counts the instructions in each step. Pass 2
//! runs each step again on that instruction budget alone, with the
//! translated blocks where the build has them, makes the same checks, and is
//! timed. `--seeds` writes the block and hook addresses for `cfgen`.
use cfcore::board::{I2c, Panel};
use cfcore::cpu::Bus;
use cfcore::edma::Bank;
use cfcore::interp::Interp;
use cfcore::intfrc::Forced;
use cfcore::rtos::{Idle, Unblock};
use cfcore::ssi::Ssi;
use cfcore::timers::{Dtims, Host, Pits};
use cfcore::trace::{Item, Trace};
use cfcore::workload::State;
use cfcore::{aot, Cpu, Mem};
use std::cell::RefCell;
use std::collections::{BTreeSet, HashSet};
use std::rc::Rc;
use std::time::Instant;

/// Where the firmware's vector table is.
const VBR: u32 = 0x4000_0000;

/// The recorded items and how far the replay has consumed them.
struct Stream {
    tr: Trace,
    pos: usize,
    /// Pass 2 has no blocks to follow: step over them when looking ahead.
    skip_tb: bool,
    /// Register writes from the host, waiting for the instruction to finish.
    regs: Vec<(u8, u32)>,
    stop: bool,
    err: Option<String>,
    /// Host reads of guest memory that matched this core's memory.
    reads: u64,
    read_bytes: u64,
    /// Exception entries by the host that this core's own entry reproduced.
    entries: u64,
    /// The Rust timer models, run in place of the recorded Python ones.
    pits: Option<Pits>,
    dtims: Option<Dtims>,
    /// The forced-interrupt models, by recorded source number less 2.
    forced: [Option<Forced>; 2],
    /// The audio model, and the bytes of audio it handed on that matched.
    ssi: Option<Ssi>,
    audio_bytes: u64,
    /// The software eDMA channels.
    bank: Option<Bank>,
    /// The I2C bus and codec.
    i2c: Option<I2c>,
    /// The idle-loop hook and the semaphore stand-in.
    idle: Option<Idle>,
    unblock: Option<Unblock>,
    /// Traps this core took itself that match the recording.
    traps: u64,
    /// The front panel, and the inputs the window gave it.
    panel: Option<Panel>,
    inputs: u64,
    /// The guest access a model is answering: several Python hooks on one
    /// address each recorded an event, and a model that covers them all
    /// steps over the later ones.
    answering: Option<(bool, u32)>,
    /// A model wrote the PC, or asked for the step to end.
    model_pc: bool,
    end_steps: u64,
    /// The render-holds answers recorded for the model call in progress.
    holds: Vec<(u8, bool)>,
    /// Their calls that agreed with the recording: steps, services, hooks,
    /// the interrupts they raised, and the calls where the render's answers
    /// were mixed (the only ones where the order of asking could matter).
    model: [u64; 5],
}

/// The machine as a timer model sees it during a replay: everything the
/// model does is checked against what the Python model did at this point of
/// the recording, and applied.
struct VHost<'a> {
    s: &'a mut Stream,
    m: &'a mut Mem,
    c: Option<&'a mut Cpu>,
}

impl VHost<'_> {
    /// The next recorded host write, after checking the reads before it.
    fn next_write(&mut self) -> Option<Item> {
        loop {
            match self.s.tr.items.get(self.s.pos) {
                Some(Item::HRead { .. }) => {
                    self.s.apply_one(self.m);
                }
                Some(&Item::Mem { write, addr, .. }) if self.s.answering == Some((write, addr)) => {
                    self.s.pos += 1;
                }
                other => return other.copied(),
            }
        }
    }

    fn reg(&mut self, r: u8, v: u32) {
        match self.next_write() {
            Some(Item::WReg(rr, vv)) if rr == r && (vv == v || (r == 16 && vv & 0xffff == v & 0xffff)) => {
                self.s.pos += 1;
            }
            other => {
                self.s.err.get_or_insert(format!(
                    "the timer model wrote register {r} = {v:#x}, recorded next: {other:x?}"
                ));
            }
        }
    }
}

impl Host for VHost<'_> {
    fn read(&mut self, addr: u32, out: &mut [u8]) {
        self.m.read_bytes(addr, out);
    }

    fn write(&mut self, addr: u32, data: &[u8]) {
        match self.next_write() {
            Some(Item::WMem { addr: a, off, len })
                if a == addr && &self.s.tr.blob[off as usize..(off + len) as usize] == data =>
            {
                self.s.pos += 1;
            }
            other => {
                self.s.err.get_or_insert(format!(
                    "the timer model wrote {data:x?} at {addr:#010x}, recorded next: {other:x?}"
                ));
            }
        }
        self.m.write_bytes(addr, data);
    }

    fn sr(&mut self) -> u32 {
        self.c.as_ref().map_or(0, |c| c.full_sr() & 0xffff)
    }

    fn raise(&mut self, vec: u32, level: Option<u32>) -> bool {
        let mut slot = [0u8; 4];
        self.m.read_bytes(VBR + vec * 4, &mut slot);
        let handler = u32::from_be_bytes(slot);
        if handler == 0 {
            return false;
        }
        let (sr, pc, sp) = match self.c.as_ref() {
            Some(c) => (c.full_sr() & 0xffff, c.pc, c.r[15].wrapping_sub(8)),
            None => return false,
        };
        let mut frame = [0u8; 8];
        frame[..2].copy_from_slice(&((0x4000 | ((vec << 2) & 0x0ffc)) as u16).to_be_bytes());
        frame[2..4].copy_from_slice(&(sr as u16).to_be_bytes());
        frame[4..].copy_from_slice(&pc.to_be_bytes());
        let mut live = sr | 0x2000;
        if let Some(level) = level {
            live = (live & !0x0700) | ((level & 7) << 8);
        }
        self.write(sp, &frame);
        self.reg(15, sp);
        self.reg(16, live);
        self.reg(17, handler);
        if let Some(c) = self.c.as_mut() {
            c.r[15] = sp;
            c.set_sr(live);
            c.pc = handler;
        }
        self.s.model_pc = true;
        self.s.entries += 1;
        self.s.model[3] += 1;
        true
    }

    fn a7(&mut self) -> u32 {
        self.c.as_ref().map_or(0, |c| c.r[15])
    }

    fn audio(&mut self, data: &[u8]) {
        match self.next_write() {
            Some(Item::Audio { off, len }) if &self.s.tr.blob[off as usize..(off + len) as usize] == data => {
                self.s.pos += 1;
                self.s.audio_bytes += len as u64;
            }
            other => {
                self.s.err.get_or_insert(format!(
                    "the audio model handed on {} bytes that differ from the recording's next: {other:x?}",
                    data.len()
                ));
            }
        }
    }

    fn end_step(&mut self) {
        self.s.end_steps += 1;
    }

    fn mapped(&mut self, addr: u32) -> bool {
        self.m.is_mapped(addr)
    }

    fn render_holds(&mut self, _done: i64, level: Option<u32>) -> bool {
        // The answer depends only on the level and on the render's state,
        // which does not change inside one call of a model. Python asks in
        // the order its set happens to iterate and stops at the first no, so
        // the answers are matched by level, not by position.
        self.next_write();
        while let Some(&Item::RenderHolds(b, l)) = self.s.tr.items.get(self.s.pos) {
            self.s.holds.push((l, b));
            self.s.pos += 1;
            // The render's state follows what Python did: a yes there set
            // its waiting flag whether or not the Rust model asks.
            if let (true, Some(ssi)) = (b, self.s.ssi.as_mut()) {
                ssi.render.waiting = true;
            }
        }
        let asked = level.map_or(0xff, |l| l as u8);
        if let Some(&(_, b)) = self.s.holds.iter().find(|(l, _)| *l == asked) {
            // With the audio model in Rust the answer is its own to give.
            if let Some(ssi) = self.s.ssi.as_ref() {
                let own = ssi.render.would_hold(_done, level);
                if own != b {
                    self.s.err.get_or_insert(format!(
                        "the Rust render says {own} to a tick at level {level:?} at {_done}, Python said {b}"
                    ));
                }
            }
            return b;
        }
        if self.s.holds.iter().any(|(_, b)| !*b) {
            // Python stopped at a no before reaching this level; the
            // model's step comes out the same whatever the answer.
            return false;
        }
        self.s.err.get_or_insert(format!(
            "the model asked whether the render holds a tick at level {level:?}; the recording has {:?}",
            self.s.holds
        ));
        false
    }
}

impl Stream {
    /// A model call is over: the recording must end it the same way.
    fn finish_call(&mut self, src: u8, done: i64, next: Option<Item>, result: Option<i64>) {
        match (next, result) {
            (Some(Item::SrcStepEnd(s2, r)), Some(got)) if s2 == src => {
                let want = self.tr.nums[r as usize];
                if got != want {
                    self.err.get_or_insert(format!(
                        "timer model {src} step({done}) = {got}, recorded {want}"
                    ));
                }
                self.pos += 1;
                self.model[0] += 1;
            }
            (Some(Item::SrcServiceEnd(s2)), None) if s2 == src => {
                self.pos += 1;
                self.model[1] += 1;
            }
            (other, _) => {
                self.err.get_or_insert(format!(
                    "timer model {src} at {done}: the recording goes on with {other:x?}"
                ));
            }
        }
    }

    /// Run the timer models wherever the recording says the Python ones
    /// ran between two engine steps, and check them call by call.
    fn timers(&mut self, c: &mut Cpu, m: &mut Mem) {
        loop {
            self.effects_cpu(c, m);
            let (src, at, service) = match self.tr.items.get(self.pos) {
                Some(&Item::RenderHolds(yes, _)) => {
                    // Asked by a model that is still Python.
                    if let (true, Some(ssi)) = (yes, self.ssi.as_mut()) {
                        ssi.render.waiting = true;
                    }
                    self.pos += 1;
                    continue;
                }
                Some(Item::SrcStepEnd(..)) | Some(Item::SrcServiceEnd(_)) => {
                    self.pos += 1;
                    continue;
                }
                Some(&Item::Input(kind, at)) => {
                    let n = &self.tr.nums[at as usize..at as usize + 3];
                    if let Some(p) = self.panel.as_mut() {
                        match kind {
                            0 => p.key(n[0] as u8, n[1] as u8, n[2] != 0),
                            1 => p.pad(n[0] as u8, n[1] as i32),
                            _ => p.turn(n[0] as u8, n[1] as i32),
                        }
                        self.inputs += 1;
                    }
                    self.pos += 1;
                    continue;
                }
                Some(&Item::SrcStep(src, at)) => (src, at, false),
                Some(&Item::SrcService(src, at)) => (src, at, true),
                _ => return,
            };
            self.pos += 1;
            let done = self.tr.nums[at as usize];
            self.holds.clear();
            let (mut pits, mut dtims) = (self.pits.take(), self.dtims.take());
            let mut forced = [self.forced[0].take(), self.forced[1].take()];
            let f = if (2..4).contains(&src) { forced[src as usize - 2].as_mut() } else { None };
            let mut ran = true;
            if src == 4 {
                if let Some(mut a) = self.ssi.take() {
                    let mut h = VHost { s: self, m, c: Some(c) };
                    let result = if service {
                        a.service(&mut h, done);
                        None
                    } else {
                        Some(a.step(&mut h, done).unwrap_or(-1))
                    };
                    let next = h.next_write();
                    if a.unsupported {
                        self.err.get_or_insert("the audio channel is programmed in a way the Rust model does not handle".into());
                    }
                    self.ssi = Some(a);
                    self.pits = pits;
                    self.dtims = dtims;
                    self.forced = forced;
                    self.finish_call(src, done, next, result);
                    if self.err.is_some() {
                        return;
                    }
                    continue;
                }
            }
            if src == 5 {
                if let Some(mut b) = self.bank.take() {
                    let mut h = VHost { s: self, m, c: Some(c) };
                    let result = if service {
                        b.service(&mut h, done);
                        None
                    } else {
                        Some(-1)
                    };
                    let next = h.next_write();
                    self.bank = Some(b);
                    self.pits = pits;
                    self.dtims = dtims;
                    self.forced = forced;
                    self.finish_call(src, done, next, result);
                    if self.err.is_some() {
                        return;
                    }
                    continue;
                }
            }
            let mut h = VHost { s: self, m, c: Some(c) };
            let result = match (src, pits.as_mut(), dtims.as_mut(), f, service) {
                (0, Some(p), _, _, false) => Some(p.step(&mut h, done)),
                (0, Some(p), _, _, true) => {
                    p.service(&mut h, done);
                    None
                }
                (1, _, Some(d), _, false) => Some(d.step(&mut h, done)),
                (1, _, Some(d), _, true) => {
                    d.service(&mut h, done);
                    None
                }
                (_, _, _, Some(f), false) => Some(f.step(&mut h, done).unwrap_or(-1)),
                (_, _, _, Some(f), true) => {
                    f.service(&mut h, done);
                    None
                }
                _ => {
                    ran = false;
                    None
                }
            };
            let next = h.next_write();
            if self.holds.iter().any(|(_, b)| *b) && self.holds.iter().any(|(_, b)| !*b) {
                self.model[4] += 1;
            }
            self.pits = pits;
            self.dtims = dtims;
            self.forced = forced;
            if !ran {
                continue;
            }
            self.finish_call(src, done, next, result);
            if self.err.is_some() {
                return;
            }
        }
    }

    fn peek(&mut self) -> Option<Item> {
        if self.skip_tb {
            while let Some(Item::Tb(..)) = self.tr.items.get(self.pos) {
                self.pos += 1;
            }
        }
        self.tr.items.get(self.pos).copied()
    }

    /// The next item for an event group: several hooks on one access are
    /// recorded back to back, so only the first may sit behind blocks.
    fn event(&mut self, first: bool) -> Option<Item> {
        if first {
            self.peek()
        } else {
            self.tr.items.get(self.pos).copied()
        }
    }

    /// Consume one thing the host did after the item just consumed: a
    /// write is applied, a read is compared. -> whether there was one.
    fn apply_one(&mut self, m: &mut Mem) -> bool {
        match self.tr.items.get(self.pos) {
            Some(&Item::WMem { addr, off, len }) => {
                let data = &self.tr.blob[off as usize..(off + len) as usize];
                m.write_bytes(addr, data);
            }
            Some(&Item::HRead { addr, off, len }) => {
                let want = &self.tr.blob[off as usize..(off + len) as usize];
                let mut got = vec![0u8; len as usize];
                m.read_bytes(addr, &mut got);
                if got != want {
                    let at = got.iter().zip(want).position(|(a, b)| a != b).unwrap();
                    self.err.get_or_insert(format!(
                        "the host read {} memory at {:#010x}: this core has {:#04x}, recorded {:#04x}",
                        if m.is_device(addr) { "device" } else { "RAM" },
                        addr as usize + at,
                        got[at],
                        want[at]
                    ));
                }
                self.reads += 1;
                self.read_bytes += len as u64;
            }
            Some(&Item::WReg(r, v)) => self.regs.push((r, v)),
            Some(Item::Stop) => self.stop = true,
            // Samples only matter to the audio model's own check.
            Some(Item::Audio { .. }) => {}
            _ => return false,
        }
        self.pos += 1;
        true
    }

    /// Everything the host did inside a memory hook.
    fn effects(&mut self, m: &mut Mem) {
        while self.apply_one(m) {}
    }

    /// Everything the host did where the CPU is at an instruction boundary:
    /// register writes take effect at once, and an exception entry is
    /// checked against this core's own. -> whether the PC was written.
    fn effects_cpu(&mut self, c: &mut Cpu, m: &mut Mem) -> bool {
        let mut pc = false;
        loop {
            match check_entry(self, c, m) {
                Some(true) => self.entries += 1,
                Some(false) => {
                    self.err.get_or_insert(format!("exception entry differs at {:#010x}", c.pc));
                }
                None => {}
            }
            if !self.apply_one(m) {
                return pc;
            }
            pc |= apply_regs(self, c);
        }
    }
}

struct ReplayBus(Rc<RefCell<Stream>>);

impl Bus for ReplayBus {
    fn access(&mut self, m: &mut Mem, write: bool, addr: u32, size: u32, value: u32) {
        let s = &mut *self.0.borrow_mut();
        // One recorded event per hook that covered this access; an access
        // nothing hooks has none.
        let mut first = true;
        while let Some(Item::Mem { write: w, size: sz, addr: a, value: v }) = s.event(first) {
            if w != write || a != addr {
                break;
            }
            let first_event = first;
            first = false;
            if sz as u32 != size || (write && v != value) {
                s.err.get_or_insert(format!(
                    "device write {addr:#010x}: size {size} value {value:#x}, recorded size {sz} value {v:#x}"
                ));
            }
            s.pos += 1;
            // A DMA timer register: the Rust model answers, and must do what
            // the Python hook did.
            if let Some(mut d) = s.dtims.take() {
                if d.owns(write, addr) {
                    let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                    d.access(&mut h, write, addr);
                    if let Some(extra @ (Item::WMem { .. } | Item::WReg(..))) = h.next_write() {
                        s.err.get_or_insert(format!(
                            "DMA timer hook at {addr:#010x}: the recording also has {extra:x?}"
                        ));
                    }
                    s.model[2] += 1;
                }
                s.dtims = Some(d);
            }
            // A force register: each model that watches it, once per access.
            if first_event {
                if let Some(mut p) = s.panel.take() {
                    if p.owns(write, addr) {
                        s.answering = Some((write, addr));
                        let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                        p.access(&mut h, write, addr, size, value);
                        if let Some(extra @ (Item::WMem { .. } | Item::WReg(..))) = h.next_write() {
                            s.err.get_or_insert(format!(
                                "panel hook at {addr:#010x}: the recording also has {extra:x?}"
                            ));
                        }
                        s.answering = None;
                        s.model[2] += 1;
                    }
                    s.panel = Some(p);
                }
                if let Some(mut i) = s.i2c.take() {
                    if i.owns(write, addr) {
                        let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                        i.access(&mut h, write, addr, size, value);
                        if let Some(extra @ (Item::WMem { .. } | Item::WReg(..))) = h.next_write() {
                            s.err.get_or_insert(format!(
                                "I2C hook at {addr:#010x}: the recording also has {extra:x?}"
                            ));
                        }
                        s.model[2] += 1;
                    }
                    s.i2c = Some(i);
                }
                if let Some(mut b) = s.bank.take() {
                    if b.owns(write, addr) {
                        let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                        b.access(&mut h, write, addr, size, value);
                        if let Some(extra @ (Item::WMem { .. } | Item::WReg(..))) = h.next_write() {
                            s.err.get_or_insert(format!(
                                "eDMA hook at {addr:#010x}: the recording also has {extra:x?}"
                            ));
                        }
                        s.model[2] += 1;
                    }
                    s.bank = Some(b);
                }
                if let Some(mut a) = s.ssi.take() {
                    if a.owns(write, addr) {
                        let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                        a.write(&mut h, addr, size, value);
                        s.model[2] += 1;
                    }
                    s.ssi = Some(a);
                }
                for i in 0..2 {
                    if let Some(mut f) = s.forced[i].take() {
                        if f.owns(write, addr) {
                            let mut h = VHost { s: &mut *s, m: &mut *m, c: None };
                            f.write(&mut h, addr, size, value);
                            s.model[2] += 1;
                        }
                        s.forced[i] = Some(f);
                    }
                }
            }
            s.effects(m);
        }
    }
}

/// If the host writes about to be applied are an exception entry (a frame
/// below the stack pointer, then A7, SR and PC), make the same entry with
/// this core's own `ops::exception` on a copy of the CPU and compare.
/// -> Some(matches), None if the writes are something else.
fn check_entry(s: &Stream, c: &Cpu, m: &mut Mem) -> Option<bool> {
    let it = &s.tr.items;
    let (&Item::WMem { addr, off, len: 8 }, &Item::WReg(15, a7), &Item::WReg(16, sr), &Item::WReg(17, pc)) =
        (it.get(s.pos)?, it.get(s.pos + 1)?, it.get(s.pos + 2)?, it.get(s.pos + 3)?)
    else {
        return None;
    };
    if addr != c.r[15].wrapping_sub(8) || a7 != addr {
        return None;
    }
    let frame = &s.tr.blob[off as usize..off as usize + 8];
    let vec = (u16::from_be_bytes([frame[0], frame[1]]) as u32 & 0x0ffc) >> 2;
    let ret = u32::from_be_bytes([frame[4], frame[5], frame[6], frame[7]]);
    let old = c.full_sr() & 0xffff;
    let level = if (sr ^ old) & 0x0700 != 0 { Some((sr >> 8) & 7) } else { None };
    let mut t = *c;
    let mut saved = [0u8; 8];
    m.read_bytes(addr, &mut saved);
    let taken = cfcore::ops::exception(&mut t, m, vec, level, ret, VBR);
    let mut got = [0u8; 8];
    m.read_bytes(addr, &mut got);
    m.write_bytes(addr, &saved);
    Some(taken && got == frame && t.r[15] == a7 && t.full_sr() & 0xffff == sr & 0xffff && t.pc == pc)
}

/// Apply pending host register writes. -> whether the PC was written.
fn apply_regs(s: &mut Stream, c: &mut Cpu) -> bool {
    let mut pc = false;
    for (r, v) in s.regs.drain(..) {
        match r {
            0..=15 => c.r[r as usize] = v,
            16 => c.set_sr(v),
            _ => {
                c.pc = v;
                pc = true;
            }
        }
    }
    pc
}

fn compare(c: &Cpu, want: &State, pc: u32) -> Vec<String> {
    let mut bad = Vec::new();
    if c.pc != pc {
        bad.push(format!("pc {:#010x}, recorded {pc:#010x}", c.pc));
    }
    for i in 0..16 {
        if c.r[i] != want.regs[i] {
            let name = if i < 8 { 'd' } else { 'a' };
            bad.push(format!("{name}{} {:#010x}, recorded {:#010x}", i & 7, c.r[i], want.regs[i]));
        }
    }
    if c.full_sr() & 0xffff != want.regs[16] & 0xffff {
        bad.push(format!("sr {:#06x}, recorded {:#06x}", c.full_sr(), want.regs[16]));
    }
    for i in 0..4 {
        if c.macc[i] != want.acc[i] {
            bad.push(format!("acc{i} {:#018x}, recorded {:#018x}", c.macc[i], want.acc[i]));
        }
    }
    if c.macsr != want.macsr {
        bad.push(format!("macsr {:#x}, recorded {:#x}", c.macsr, want.macsr));
    }
    if c.mac_mask != want.mask {
        bad.push(format!("mask {:#x}, recorded {:#x}", c.mac_mask, want.mask));
    }
    bad
}

struct Machine {
    c: Cpu,
    m: Mem,
    s: Rc<RefCell<Stream>>,
    bp: HashSet<u32>,
    it: Interp,
    /// CFREPLAY_TRACE is set: print every interpreted instruction.
    trace: bool,
}

fn machine(s: &Rc<RefCell<Stream>>, skip_tb: bool) -> Machine {
    let mut m = Mem::new();
    let mut c = Cpu::default();
    {
        let st = &mut *s.borrow_mut();
        st.pos = 0;
        st.skip_tb = skip_tb;
        st.regs.clear();
        st.stop = false;
        st.err = None;
        st.reads = 0;
        st.read_bytes = 0;
        st.entries = 0;
        st.model = [0; 5];
        st.pits = None;
        st.dtims = None;
        st.forced = [None, None];
        st.ssi = None;
        st.bank = None;
        st.panel = None;
        st.idle = None;
        st.i2c = None;
        st.unblock = None;
        st.traps = 0;
        st.inputs = 0;
        st.answering = None;
        st.audio_bytes = 0;
        st.end_steps = 0;
        if std::env::var_os("CFREPLAY_NO_MODELS").is_none() {
            for src in &st.tr.sources {
                match src.kind {
                    0 => st.pits = Some(Pits(src.clock.clone())),
                    k @ (2 | 3) => st.forced[k as usize - 2] = Some(src.forced.clone()),
                    4 => st.ssi = Some(src.ssi.clone()),
                    5 => st.bank = Some(src.bank.clone()),
                    6 => st.panel = Some(src.panel.clone()),
                    7 => st.idle = Some(src.idle.clone()),
                    9 => st.i2c = Some(src.i2c.clone()),
                    8 => st.unblock = Some(src.unblock.clone()),
                    _ => st.dtims = Some(Dtims { clock: src.clock.clone(), arm: src.arm, now: src.now }),
                }
            }
        }
        for r in &st.tr.regions {
            for mb in 0..r.len.div_ceil(1 << 20) {
                let at = r.addr.wrapping_add(mb << 20);
                match r.alias {
                    Some(target) => m.alias(at, target.wrapping_add(mb << 20)),
                    None if r.device => m.map_device(at),
                    None => m.map(at),
                }
            }
            if r.alias.is_none() {
                m.write_bytes(r.addr, &r.data);
            }
        }
        c.r.copy_from_slice(&st.tr.init.regs[..16]);
        c.set_sr(st.tr.init.regs[16]);
        c.macc = st.tr.init.acc;
        c.macsr = st.tr.init.macsr;
        c.mac_mask = st.tr.init.mask;
        c.vbr = VBR;
    }
    m.bus = Some(Box::new(ReplayBus(s.clone())));
    let bp = s.borrow().tr.code_hooks.iter().copied().collect();
    let trace = std::env::var_os("CFREPLAY_TRACE").is_some();
    Machine { c, m, s: s.clone(), bp, it: Interp::new(), trace }
}

/// What ended the instruction or block being replayed.
enum After {
    Continue,
    /// A hook or exception redirected the PC, or stopped the engine.
    Redirected,
}

impl Machine {
    fn check(&mut self) -> Result<(), String> {
        if let Some(e) = self.s.borrow_mut().err.take() {
            return Err(e);
        }
        if self.m.fault != 0 {
            return Err(format!("access to unmapped {:#010x}", self.m.fault - 1));
        }
        Ok(())
    }

    /// Fire the recorded code hooks for the instruction about to run.
    fn code_hooks(&mut self) -> After {
        let pc = self.c.pc;
        let s = &mut *self.s.borrow_mut();
        let mut redirected = false;
        let mut first = true;
        while let Some(Item::Code(a)) = s.event(first) {
            if a != pc {
                break;
            }
            first = false;
            s.pos += 1;
            // The idle spin and the RTOS routines hooked for the stand-ins.
            if let Some(mut i) = s.idle.take() {
                if i.owns(pc) {
                    let skip = match s.tr.items.get(s.pos) {
                        Some(&Item::IdleSkip(at)) => {
                            s.pos += 1;
                            s.tr.nums[at as usize] as u64
                        }
                        _ => 0,
                    };
                    s.model_pc = false;
                    let mut h = VHost { s: &mut *s, m: &mut self.m, c: Some(&mut self.c) };
                    i.hit(&mut h, skip);
                    redirected |= s.model_pc;
                    s.model[2] += 1;
                }
                s.idle = Some(i);
            }
            if let Some(mut u) = s.unblock.take() {
                if u.owns(pc) {
                    let mut h = VHost { s: &mut *s, m: &mut self.m, c: Some(&mut self.c) };
                    u.hit(&mut h, pc);
                    s.model[2] += 1;
                }
                s.unblock = Some(u);
            }
            // The `rte` the audio model hooks: it may enter the render here.
            if let Some(mut a) = s.ssi.take() {
                if pc == a.force_rte {
                    s.model_pc = false;
                    s.holds.clear();
                    let mut h = VHost { s: &mut *s, m: &mut self.m, c: Some(&mut self.c) };
                    a.force_rte(&mut h);
                    redirected |= s.model_pc;
                    s.model[2] += 1;
                }
                s.ssi = Some(a);
            }
            redirected |= s.effects_cpu(&mut self.c, &mut self.m);
        }
        if redirected || s.stop {
            After::Redirected
        } else {
            After::Continue
        }
    }

    /// Run one instruction with the interpreter. -> (what happened, whether
    /// it counted as executed, whether it was a branch).
    fn step(&mut self) -> Result<(After, bool, bool), String> {
        let at = self.c.pc;
        if self.trace {
            eprintln!(
                "T {at:08x} d0={:08x} d1={:08x} a0={:08x} a7={:08x} sr={:04x} pos={}",
                self.c.r[0],
                self.c.r[1],
                self.c.r[8],
                self.c.r[15],
                self.c.full_sr(),
                self.s.borrow().pos
            );
        }
        let mut op = [0u8; 2];
        self.m.read_bytes(at, &mut op);
        let trap = op[0] == 0x4e && op[1] & 0xf0 == 0x40;
        let flow = self.it.step(&mut self.c, &mut self.m);
        self.check()?;
        let s = &mut *self.s.borrow_mut();
        if apply_regs(s, &mut self.c) {
            return Err(format!("a memory hook wrote the PC at {at:#010x}"));
        }
        if trap && self.c.halted == 0 {
            // This core took the trap itself: the recording has the hook
            // firing and Python making the same entry.
            if !matches!(s.peek(), Some(Item::Intr(_))) {
                return Err(format!("trap at {at:#010x} without a recorded exception"));
            }
            s.pos += 1;
            while let Some(Item::HRead { .. }) = s.tr.items.get(s.pos) {
                s.apply_one(&mut self.m);
            }
            let it = &s.tr.items;
            let same = match (it.get(s.pos), it.get(s.pos + 1), it.get(s.pos + 2), it.get(s.pos + 3)) {
                (
                    Some(&Item::WMem { addr, off, len: 8 }),
                    Some(&Item::WReg(15, a7)),
                    Some(&Item::WReg(16, sr)),
                    Some(&Item::WReg(17, pc)),
                ) => {
                    let mut got = [0u8; 8];
                    self.m.read_bytes(addr, &mut got);
                    got == s.tr.blob[off as usize..off as usize + 8]
                        && addr == a7
                        && self.c.r[15] == a7
                        && self.c.full_sr() & 0xffff == sr & 0xffff
                        && self.c.pc == pc
                }
                _ => false,
            };
            if !same {
                return Err(format!("trap at {at:#010x}: this core's entry differs from the recording"));
            }
            s.pos += 4;
            s.entries += 1;
            s.traps += 1;
            if let Some(e) = s.err.take() {
                return Err(e);
            }
            return Ok((After::Redirected, true, flow));
        }
        if self.c.halted != 0 {
            // An instruction this core hands to the exception hook.
            self.c.halted = 0;
            match s.peek() {
                Some(Item::Intr(_)) => {
                    s.pos += 1;
                    s.effects_cpu(&mut self.c, &mut self.m);
                    return Ok((After::Redirected, false, flow));
                }
                other => {
                    return Err(format!(
                        "unimplemented instruction at {at:#010x}, next recorded item {other:?}"
                    ))
                }
            }
        }
        if s.stop {
            return Ok((After::Redirected, true, flow));
        }
        Ok((After::Continue, true, flow))
    }

    fn end_step(&mut self, state: u32, pc: u32) -> Result<(), String> {
        let s = &mut *self.s.borrow_mut();
        let bad = compare(&self.c, &s.tr.states[state as usize], pc);
        if !bad.is_empty() {
            return Err(bad.join("; "));
        }
        s.stop = false;
        s.timers(&mut self.c, &mut self.m);
        match s.err.take() {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Compare all of memory with the recording's final dump.
    fn final_memory(&self) -> Result<u64, String> {
        let s = self.s.borrow();
        let mut bytes = 0u64;
        for (addr, want) in &s.tr.last {
            let mut got = vec![0u8; want.len()];
            self.m.read_bytes(*addr, &mut got);
            if &got != want {
                let n = got.iter().zip(want).filter(|(a, b)| a != b).count();
                let at = got.iter().zip(want).position(|(a, b)| a != b).unwrap();
                return Err(format!(
                    "final {} memory differs in {n} bytes of the region at {addr:#010x}, first at {:#010x}: {:#04x}, recorded {:#04x}",
                    if self.m.is_device(*addr) { "device" } else { "RAM" },
                    *addr as usize + at,
                    got[at],
                    want[at]
                ));
            }
            bytes += want.len() as u64;
        }
        Ok(bytes)
    }
}

/// What a pass checked besides the step states.
struct Checked {
    reads: u64,
    read_bytes: u64,
    entries: u64,
    final_bytes: u64,
    model: [u64; 5],
    audio_bytes: u64,
    inputs: u64,
    traps: u64,
}

fn checked(k: &Machine) -> Result<Checked, String> {
    let final_bytes = k.final_memory()?;
    let s = k.s.borrow();
    Ok(Checked {
        reads: s.reads,
        read_bytes: s.read_bytes,
        entries: s.entries,
        final_bytes,
        model: s.model,
        audio_bytes: s.audio_bytes,
        inputs: s.inputs,
        traps: s.traps,
    })
}

/// Follow the recorded blocks. -> instructions executed in each step.
fn pass1(s: &Rc<RefCell<Stream>>) -> Result<(Vec<i64>, Checked), String> {
    let mut k = machine(s, false);
    let mut counts = Vec::new();
    let mut n = 0i64;
    loop {
        let item = k.s.borrow_mut().peek();
        let fail = |k: &Machine, e: String| {
            format!("step {}, item {}, pc {:#010x}: {e}", counts.len(), k.s.borrow().pos, k.c.pc)
        };
        match item {
            None => break,
            Some(Item::StepStart(pc)) => {
                k.s.borrow_mut().pos += 1;
                k.c.pc = pc;
                n = 0;
            }
            Some(Item::StepEnd(state, pc)) => {
                k.s.borrow_mut().pos += 1;
                k.end_step(state, pc).map_err(|e| fail(&k, e))?;
                counts.push(n);
            }
            Some(Item::Tb(addr, size)) => {
                k.s.borrow_mut().pos += 1;
                if k.c.pc != addr {
                    return Err(fail(&k, format!("the reference entered a block at {addr:#010x}")));
                }
                let end = addr.wrapping_add(size);
                loop {
                    if k.bp.contains(&k.c.pc) {
                        if let After::Redirected = k.code_hooks() {
                            break;
                        }
                    }
                    let (after, counted, flow) = k.step().map_err(|e| fail(&k, e))?;
                    n += counted as i64;
                    if matches!(after, After::Redirected) || flow || k.c.pc == end {
                        break;
                    }
                    if k.c.pc < addr || k.c.pc > end {
                        return Err(fail(&k, format!("left the block {addr:#010x}+{size}")));
                    }
                }
                k.check().map_err(|e| fail(&k, e))?;
            }
            Some(other) => return Err(fail(&k, format!("unexpected recorded item {other:?}"))),
        }
    }
    let c = checked(&k)?;
    Ok((counts, c))
}

/// Run each step on its instruction budget. -> (seconds, instructions the
/// interpreter ran, what was checked).
fn pass2(s: &Rc<RefCell<Stream>>, counts: &[i64]) -> Result<(f64, u64, Checked), String> {
    let mut k = machine(s, true);
    let mut step = 0usize;
    let t0 = Instant::now();
    loop {
        let item = k.s.borrow_mut().peek();
        let pc = match item {
            None => break,
            Some(Item::StepStart(pc)) => pc,
            Some(other) => return Err(format!("step {step}: unexpected recorded item {other:?}")),
        };
        k.s.borrow_mut().pos += 1;
        k.c.pc = pc;
        let mut left = counts[step];
        let fail = |k: &Machine, e: String| format!("step {step}, pc {:#010x}: {e}", k.c.pc);
        let (state, end_pc) = loop {
            if left == 0 {
                if let Some(Item::StepEnd(state, end_pc)) = k.s.borrow_mut().peek() {
                    break (state, end_pc);
                }
            }
            if k.bp.contains(&k.c.pc) {
                if let After::Redirected = k.code_hooks() {
                    k.check().map_err(|e| fail(&k, e))?;
                    continue;
                }
            } else if left > 0 && aot::has(k.c.pc) {
                let before = left;
                aot::run(&mut k.c, &mut k.m, &mut left);
                if left != before {
                    k.check().map_err(|e| fail(&k, e))?;
                    if !k.s.borrow().regs.is_empty() || k.s.borrow().stop {
                        return Err(fail(&k, "a memory hook wrote a register or stopped".into()));
                    }
                    continue;
                }
            }
            if left <= 0 {
                let next = k.s.borrow_mut().peek();
                return Err(fail(&k, format!("budget used up, next recorded item {next:?}")));
            }
            let (_, counted, _) = k.step().map_err(|e| fail(&k, e))?;
            left -= counted as i64;
        };
        k.s.borrow_mut().pos += 1;
        k.end_step(state, end_pc).map_err(|e| fail(&k, e))?;
        step += 1;
    }
    let secs = t0.elapsed().as_secs_f64();
    let c = checked(&k)?;
    Ok((secs, k.it.executed, c))
}

fn main() {
    let mut path = None;
    let mut seeds = None;
    let mut reps = 3usize;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--seeds" => seeds = args.next(),
            "--reps" => reps = args.next().and_then(|v| v.parse().ok()).expect("--reps N"),
            _ => path = Some(a),
        }
    }
    let tr = Trace::read(&path.expect("TRACE")).unwrap_or_else(|e| panic!("{e}"));
    let emulated = tr.emulated_ns as f64 / 1e9;
    let raised = tr.raised;
    let mut kinds = [0u64; 5];
    let mut tbs: BTreeSet<u32> = BTreeSet::new();
    for item in &tr.items {
        match item {
            Item::Tb(a, _) => {
                kinds[0] += 1;
                tbs.insert(*a);
            }
            Item::Code(_) => kinds[1] += 1,
            Item::Mem { .. } => kinds[2] += 1,
            Item::Intr(_) => kinds[3] += 1,
            Item::StepEnd(..) => kinds[4] += 1,
            _ => {}
        }
    }
    eprintln!(
        "{:.3} emulated s: {} steps, {} blocks ({} distinct), {} code hooks, {} memory hooks, {} exceptions",
        emulated, kinds[4], kinds[0], tbs.len(), kinds[1], kinds[2], kinds[3]
    );
    if let Some(out) = seeds {
        let mut text = String::new();
        for a in &tbs {
            text.push_str(&format!("b {a:08x}\n"));
        }
        for a in &tr.code_hooks {
            text.push_str(&format!("x {a:08x}\n"));
        }
        std::fs::write(out, text).expect("write seeds");
    }
    let s = Rc::new(RefCell::new(Stream {
        tr,
        pos: 0,
        skip_tb: false,
        regs: Vec::new(),
        stop: false,
        err: None,
        reads: 0,
        read_bytes: 0,
        entries: 0,
        pits: None,
        dtims: None,
        forced: [None, None],
        ssi: None,
        bank: None,
        panel: None,
        idle: None,
        i2c: None,
        unblock: None,
        traps: 0,
        inputs: 0,
        answering: None,
        audio_bytes: 0,
        model_pc: false,
        end_steps: 0,
        holds: Vec::new(),
        model: [0; 5],
    }));
    let report = |name: &str, steps: usize, c: &Checked| {
        eprintln!(
            "{name}: {steps} step states match; {} host reads ({} bytes) match; {} of {} exception entries reproduced; final memory matches ({} MB)",
            c.reads,
            c.read_bytes,
            c.entries,
            raised,
            c.final_bytes >> 20
        );
        if c.model != [0; 5] {
            eprintln!(
                "    Rust models in place of the Python ones: {} step results, {} services ({} interrupts raised) and {} hooks agree; {} bytes of audio handed on match; {} panel inputs applied; {} traps taken natively; {} calls had mixed render answers",
                c.model[0], c.model[1], c.model[3], c.model[2], c.audio_bytes, c.inputs, c.traps, c.model[4]
            );
        }
    };
    let (counts, c1) = match pass1(&s) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("pass 1 (following the reference): MISMATCH at {e}");
            std::process::exit(1);
        }
    };
    let total: i64 = counts.iter().sum();
    {
        // The Python clock credits each skipped idle pass as one block of
        // 3.87 instructions at its 64M a second. What is left is the time
        // the instructions actually run took, which gives their real rate.
        let st = s.borrow();
        let skipped: i64 = st
            .tr
            .items
            .iter()
            .filter_map(|i| match i {
                Item::IdleSkip(at) => Some(st.tr.nums[*at as usize]),
                _ => None,
            })
            .sum();
        let idle = skipped as f64 * 3.87 / 64e6;
        if emulated > idle {
            eprintln!(
                "{:.1}% of the emulated time was idle; the rest ran {:.1}M real instructions a second",
                100.0 * idle / emulated,
                total as f64 / (emulated - idle) / 1e6
            );
        }
    }
    report("pass 1 (following the reference, interpreter)", counts.len(), &c1);
    let mut best = f64::MAX;
    let mut interp = 0;
    for rep in 0..reps {
        match pass2(&s, &counts) {
            Ok((secs, n, c2)) => {
                best = best.min(secs);
                interp = n;
                if rep == 0 {
                    report("pass 2 (instruction budgets)", counts.len(), &c2);
                }
            }
            Err(e) => {
                eprintln!("pass 2 (instruction budgets): MISMATCH at {e}");
                let st = s.borrow();
                let lo = st.pos.saturating_sub(14);
                for (i, item) in st.tr.items.iter().enumerate().skip(lo).take(22) {
                    eprintln!("  {}{i}: {item:x?}", if i == st.pos { "> " } else { "  " });
                }
                std::process::exit(1);
            }
        }
    }
    println!(
        "{{\"emulated_s\":{emulated:.6},\"steps\":{},\"insns\":{total},\"translated_blocks\":{},\
\"interp_insns\":{interp},\"seconds\":{best:.6},\"mips\":{:.1},\"cpu_s_per_emulated_s\":{:.4}}}",
        counts.len(),
        aot::BLOCKS,
        total as f64 / best / 1e6,
        if emulated > 0.0 { best / emulated } else { 0.0 }
    );
}
