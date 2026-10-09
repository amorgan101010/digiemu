//! The whole Model:Cycles in Rust: the core, the device models and the
//! loop that steps them, with no Python and no recording in it.
//!
//! The loop is digiemu's `longrun.spin`: ask every source how far it is to
//! its next event, run the CPU that many instructions, then let each source
//! deliver what is due. Its clock counts this core's real instructions, so
//! `ips` here is not the Python emulator's 64M (see the plan's note on the
//! clock unit): a machine started from a Python state has its deadlines
//! rescaled by `Machine::from_trace`.
use crate::board::{I2c, Panel};
use crate::cpu::{Bus, Cpu, Mem};
use crate::edma::Bank;
use crate::esdhc::Esdhc;
use crate::interp::Interp;
use crate::intfrc::Forced;
use crate::rtos::{Idle, Unblock};
use crate::ssi::{Render, Ssi};
use crate::timers::{Dtims, Host, Pits};
use crate::trace::{Item, Trace};
use crate::{aot, ops};
use std::cell::RefCell;
use std::rc::Rc;

/// The Python stepper's instructions per block: what one pass of the idle
/// spin cost on its clock.
const PY_PER_BLOCK: f64 = 3.87;
const VBR: u32 = 0x4000_0000;

#[derive(Default)]
pub struct Devices {
    pub pits: Pits,
    pub dtims: Dtims,
    /// INTC0's forced sources, then INTC1's.
    pub forced: Vec<Forced>,
    pub ssi: Ssi,
    pub bank: Bank,
    pub panel: Panel,
    pub i2c: I2c,
    /// The +Drive, if the machine was given a card.
    pub esdhc: Option<Esdhc>,
    pub idle: Idle,
    pub unblock: Unblock,
    /// The samples played so far, as the eDMA read them: 8 bytes a frame.
    pub audio: Vec<u8>,
    /// A model asked for the current step to end.
    pub end_step: bool,
    pub raised: u64,
}

/// The machine as a device model sees it.
struct NHost<'a> {
    c: Option<&'a mut Cpu>,
    m: &'a mut Mem,
    render: Option<&'a mut Render>,
    audio: &'a mut Vec<u8>,
    end: &'a mut bool,
    raised: &'a mut u64,
}

impl Host for NHost<'_> {
    fn read(&mut self, addr: u32, out: &mut [u8]) {
        self.m.read_bytes(addr, out);
    }

    fn write(&mut self, addr: u32, data: &[u8]) {
        self.m.write_bytes(addr, data);
    }

    fn sr(&mut self) -> u32 {
        self.c.as_ref().map_or(0x2700, |c| c.full_sr() & 0xffff)
    }

    fn raise(&mut self, vec: u32, level: Option<u32>) -> bool {
        let Some(c) = self.c.as_mut() else { return false };
        let pc = c.pc;
        let taken = ops::exception(c, self.m, vec, level, pc, VBR);
        *self.raised += taken as u64;
        taken
    }

    fn render_holds(&mut self, done: i64, level: Option<u32>) -> bool {
        self.render.as_mut().map_or(false, |r| r.holds(done, level))
    }

    fn a7(&mut self) -> u32 {
        self.c.as_ref().map_or(0, |c| c.r[15])
    }

    fn audio(&mut self, data: &[u8]) {
        self.audio.extend_from_slice(data);
    }

    fn end_step(&mut self) {
        *self.end = true;
    }

    fn mapped(&mut self, addr: u32) -> bool {
        self.m.is_mapped(addr)
    }
}

struct DevBus(Rc<RefCell<Devices>>);

impl Bus for DevBus {
    fn access(&mut self, m: &mut Mem, write: bool, addr: u32, size: u32, value: u32) {
        let d = &mut *self.0.borrow_mut();
        let Devices { dtims, forced, ssi, bank, panel, i2c, esdhc, audio, end_step, raised, .. } = d;
        let mut h = NHost { c: None, m, render: None, audio, end: end_step, raised };
        if panel.owns(write, addr) {
            panel.access(&mut h, write, addr, size, value);
        }
        if bank.owns(write, addr) {
            bank.access(&mut h, write, addr, size, value);
        }
        if ssi.owns(write, addr) {
            ssi.write(&mut h, addr, size, value);
        }
        for f in forced.iter_mut() {
            if f.owns(write, addr) {
                f.write(&mut h, addr, size, value);
            }
        }
        if dtims.owns(write, addr) {
            dtims.access(&mut h, write, addr);
        }
        if i2c.owns(write, addr) {
            i2c.access(&mut h, write, addr, size, value);
        }
        if let Some(e) = esdhc.as_mut() {
            if e.owns(write, addr) {
                e.access(&mut h, write, addr, size, value);
            }
        }
    }
}

/// Write a saved machine to `path`: by way of `path.tmp`, so that a save
/// cut short leaves the one before it, and with the one before it kept as
/// `path.prev`. One at a time.
pub fn write_saved(path: &str, data: &[u8]) -> Result<(), String> {
    use std::io::Write;
    static WRITING: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _one = WRITING.lock().unwrap_or_else(|e| e.into_inner());
    let tmp = format!("{path}.tmp");
    let mut f = std::fs::File::create(&tmp).map_err(|e| format!("{tmp}: {e}"))?;
    f.write_all(data).and_then(|_| f.sync_all()).map_err(|e| format!("{tmp}: {e}"))?;
    drop(f);
    if std::fs::metadata(path).is_ok() {
        let prev = format!("{path}.prev");
        std::fs::rename(path, &prev).map_err(|e| format!("{prev}: {e}"))?;
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("{path}: {e}"))
}

/// Whether the code in `m` is the code this build's translated blocks were
/// made from. Blocks are found by address alone, so over another firmware
/// they would run in place of whatever is there.
pub(crate) fn blocks_fit(m: &Mem) -> Result<(), String> {
    if aot::BLOCKS == 0 {
        return Ok(());
    }
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    let mut code = Vec::new();
    for &(addr, len) in aot::SPANS.iter() {
        if !m.is_mapped(addr) || !m.is_mapped(addr + len - 1) {
            return Err("this machine has no code where this build's translated blocks are".into());
        }
        code.resize(len as usize, 0);
        m.read_bytes(addr, &mut code);
        for b in &code {
            hash = (hash ^ *b as u64).wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    if hash != aot::CODE_HASH {
        return Err("this machine runs another firmware than this build's translated blocks are for".into());
    }
    Ok(())
}

/// Panel input due at an instruction count: 0 key, 1 pad, 2 turn.
#[derive(Clone, Copy, Debug)]
pub struct Input {
    pub at: i64,
    pub kind: u8,
    pub args: [i32; 3],
}

pub struct Machine {
    pub c: Cpu,
    pub m: Mem,
    pub dev: Rc<RefCell<Devices>>,
    pub it: Interp,
    /// Instructions run so far, which is the clock.
    pub now: i64,
    pub ips: i64,
    pub steps: u64,
    /// Instructions the idle skip credited without running.
    pub idle_skipped: i64,
    pub inputs: Vec<Input>,
    hooked: Vec<u32>,
}

impl Machine {
    /// The machine a recording starts from, on this core's clock of `ips`
    /// instructions a second. The recording's panel input comes along, at
    /// the same emulated times.
    pub fn from_trace(tr: &Trace, ips: i64) -> Result<Machine, String> {
        let mut m = Mem::new();
        for r in &tr.regions {
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
        let mut c = Cpu::default();
        c.r.copy_from_slice(&tr.init.regs[..16]);
        c.set_sr(tr.init.regs[16]);
        c.macc = tr.init.acc;
        c.macsr = tr.init.macsr;
        c.mac_mask = tr.init.mask;
        c.vbr = VBR;
        c.pc = match tr.items.first() {
            Some(&Item::StepStart(pc)) => pc,
            _ => return Err("the recording does not begin with a step".into()),
        };

        let mut d = Devices { forced: vec![Forced::default(), Forced::default()], ..Default::default() };
        let mut seen = 0u32;
        for s in &tr.sources {
            seen |= 1 << s.kind;
            match s.kind {
                0 => d.pits = Pits(s.clock.clone()),
                1 => d.dtims = Dtims { clock: s.clock.clone(), arm: s.arm, now: s.now },
                2 | 3 => d.forced[s.kind as usize - 2] = s.forced.clone(),
                4 => d.ssi = s.ssi.clone(),
                5 => d.bank = s.bank.clone(),
                6 => d.panel = s.panel.clone(),
                7 => d.idle = s.idle.clone(),
                8 => d.unblock = s.unblock.clone(),
                9 => d.i2c = s.i2c.clone(),
                _ => {}
            }
        }
        d.esdhc = tr.card.clone();
        if seen & 0x3ff != 0x3ff {
            return Err(format!("the recording lacks some model's state (have {seen:#b})"));
        }
        // From the Python clock, which stood at `then`, to this one at 0.
        let then = d.dtims.now;
        let py_ips = d.pits.0.ips;
        let scale = ips as f64 / py_ips;
        for clock in [&mut d.pits.0, &mut d.dtims.clock] {
            clock.ips = ips as f64;
            for next in clock.next.iter_mut().flatten() {
                *next = (*next - then as f64) * scale;
            }
        }
        d.dtims.now = 0;
        let hz = d.ssi.request_hz as i128;
        if let Some(next) = d.ssi.next.as_mut() {
            *next = ((*next - then as i128 * hz) as f64 * scale) as i128;
        }
        d.ssi.render.since = ((d.ssi.render.since - then) as f64 * scale) as i64;
        d.ssi.ips = ips;
        d.ssi.now = 0;
        d.idle.every = (d.idle.every as f64 * PY_PER_BLOCK * scale) as u64;

        let mut inputs = Vec::new();
        let mut at = then;
        for item in &tr.items {
            match *item {
                Item::SrcService(_, i) | Item::SrcStep(_, i) => at = tr.nums[i as usize],
                Item::Input(kind, i) => {
                    let n = &tr.nums[i as usize..i as usize + 3];
                    inputs.push(Input {
                        at: ((at - then) as f64 * scale) as i64,
                        kind,
                        args: [n[0] as i32, n[1] as i32, n[2] as i32],
                    });
                }
                _ => {}
            }
        }

        blocks_fit(&m)?;
        Ok(Machine::assemble(c, m, d, ips, inputs))
    }

    /// The machine in the file at `path`: a saved one (`save`), which must
    /// have been saved on this clock, or a recording's starting state.
    pub fn open(path: &str, ips: i64) -> Result<Machine, String> {
        let data = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        if crate::state::is_saved(&data) {
            let k = Machine::restore(&data).map_err(|e| format!("{path}: {e}"))?;
            if k.ips != ips {
                return Err(format!("{path}: saved at {} instructions a second, not {ips}", k.ips));
            }
            return Ok(k);
        }
        let tr = Trace::parse(&data).map_err(|e| format!("{path}: {e}"))?;
        drop(data);
        Machine::from_trace(&tr, ips)
    }

    /// Write the machine to `path`.
    pub fn save_to(&self, path: &str) -> Result<(), String> {
        write_saved(path, &self.save()?)
    }

    /// A machine of these parts at instruction count 0, with the devices
    /// behind the memory's bus.
    pub(crate) fn assemble(c: Cpu, mut m: Mem, d: Devices, ips: i64, inputs: Vec<Input>) -> Machine {
        let mut hooked = d.idle.addrs.clone();
        hooked.extend(&d.unblock.pend);
        hooked.push(d.unblock.post);
        hooked.push(d.ssi.force_rte);
        let dev = Rc::new(RefCell::new(d));
        m.bus = Some(Box::new(DevBus(dev.clone())));
        Machine { c, m, dev, it: Interp::new(), now: 0, ips, steps: 0, idle_skipped: 0, inputs, hooked }
    }

    /// How far it is to the next event of any source.
    fn next_step(&mut self) -> i64 {
        let now = self.now;
        let d = &mut *self.dev.borrow_mut();
        let Devices { pits, dtims, forced, ssi, audio, end_step, raised, .. } = d;
        let mut step = i64::MAX;
        {
            let mut h = NHost { c: Some(&mut self.c), m: &mut self.m, render: None, audio, end: end_step, raised };
            if let Some(n) = ssi.step(&mut h, now) {
                step = step.min(n);
            }
        }
        let mut h = NHost {
            c: Some(&mut self.c),
            m: &mut self.m,
            render: Some(&mut ssi.render),
            audio,
            end: end_step,
            raised,
        };
        step = step.min(pits.step(&mut h, now)).min(dtims.step(&mut h, now));
        for f in forced.iter_mut() {
            if let Some(n) = f.step(&mut h, now) {
                step = step.min(n);
            }
        }
        step
    }

    /// Let each source deliver what is due, in the Python loop's order.
    fn service(&mut self) {
        let now = self.now;
        let d = &mut *self.dev.borrow_mut();
        let Devices { pits, dtims, forced, ssi, bank, audio, end_step, raised, .. } = d;
        {
            let mut h = NHost { c: Some(&mut self.c), m: &mut self.m, render: None, audio, end: end_step, raised };
            ssi.service(&mut h, now);
            bank.service(&mut h, now);
        }
        let mut h = NHost {
            c: Some(&mut self.c),
            m: &mut self.m,
            render: Some(&mut ssi.render),
            audio,
            end: end_step,
            raised,
        };
        for f in forced.iter_mut() {
            f.service(&mut h, now);
        }
        pits.service(&mut h, now);
        dtims.service(&mut h, now);
    }

    /// The code hooks at the instruction about to run. -> whether the idle
    /// spin was reached, which ends the step with its rest credited.
    fn hooks(&mut self, left: i64) -> bool {
        let pc = self.c.pc;
        let d = &mut *self.dev.borrow_mut();
        let Devices { idle, unblock, ssi, audio, end_step, raised, .. } = d;
        let mut h = NHost { c: Some(&mut self.c), m: &mut self.m, render: None, audio, end: end_step, raised };
        if idle.owns(pc) {
            idle.hit(&mut h, left.max(0) as u64);
            return true;
        }
        if unblock.owns(pc) {
            unblock.hit(&mut h, pc);
        }
        if pc == ssi.force_rte {
            ssi.force_rte(&mut h);
        }
        false
    }

    /// Run until the clock reaches `until`. -> why it stopped early, if it
    /// did.
    pub fn run(&mut self, until: i64) -> Result<(), String> {
        while self.now < until {
            while let Some(input) = self.inputs.first().copied() {
                if input.at > self.now {
                    break;
                }
                self.inputs.remove(0);
                let panel = &mut self.dev.borrow_mut().panel;
                let a = input.args;
                match input.kind {
                    0 => panel.key(a[0] as u8, a[1] as u8, a[2] != 0),
                    1 => panel.pad(a[0] as u8, a[1]),
                    _ => panel.turn(a[0] as u8, a[1]),
                }
            }
            let step = self.next_step().max(1);
            let mut left = step;
            self.steps += 1;
            while left > 0 {
                let pc = self.c.pc;
                if self.hooked.contains(&pc) {
                    if self.hooks(left) {
                        self.idle_skipped += left;
                        left = 0;
                        break;
                    }
                    if self.c.pc == pc {
                        self.it.step(&mut self.c, &mut self.m);
                        left -= 1;
                    }
                    if std::mem::take(&mut self.dev.borrow_mut().end_step) {
                        break;
                    }
                    continue;
                }
                if aot::has(pc) {
                    let before = left;
                    aot::run(&mut self.c, &mut self.m, &mut left);
                    if left != before {
                        continue;
                    }
                }
                self.it.step(&mut self.c, &mut self.m);
                left -= 1;
                if self.c.halted != 0 {
                    return Err(format!("unimplemented instruction at {:#010x}", self.c.halted - 1));
                }
                if self.m.fault != 0 {
                    return Err(format!(
                        "access to unmapped {:#010x} near pc {:#010x}",
                        self.m.fault - 1,
                        self.c.pc
                    ));
                }
            }
            if self.c.halted != 0 {
                return Err(format!("unimplemented instruction at {:#010x}", self.c.halted - 1));
            }
            if self.m.fault != 0 {
                return Err(format!("access to unmapped {:#010x} near pc {:#010x}", self.m.fault - 1, self.c.pc));
            }
            self.now += step - left;
            self.service();
        }
        Ok(())
    }
}
