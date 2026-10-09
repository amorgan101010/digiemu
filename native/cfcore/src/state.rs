//! The whole machine written to bytes and read back: `Machine::save` and
//! `Machine::restore`. This is what lets an app be closed and opened again
//! where it was. The clock stays as it is (no rescaling: that is only for a
//! state the Python emulator made, in `Machine::from_trace`).
//!
//! What is written is everything a later step depends on: the CPU, every
//! megabyte of guest memory and what kind it is, and every device model.
//! The decode cache and the list of hooked addresses are rebuilt. A model
//! is taken apart field by field with no `..`, so a field added to one
//! without being saved here does not compile.
//!
//! A saved machine holds guest memory, so it is firmware-derived: the files
//! live outside the tree.
use crate::board::{I2c, Panel};
use crate::cpu::{ChunkKind, Cpu, Mem, CHUNK, CHUNKS};
use crate::edma::Bank;
use crate::esdhc::{Card, Esdhc, SECTOR};
use crate::intfrc::Forced;
use crate::machine::{Devices, Input, Machine};
use crate::rtos::{Idle, Unblock};
use crate::ssi::{Render, Ssi};
use crate::timers::{Clock, Dtims, Pits};
use std::collections::VecDeque;

const MAGIC: &[u8; 8] = b"CFSV2\0\0\0";
const END: &[u8; 4] = b"END.";
const ZERO: u8 = 0x80;

/// Whether `data` starts as a saved machine does.
pub fn is_saved(data: &[u8]) -> bool {
    data.starts_with(MAGIC)
}

#[derive(Default)]
struct W(Vec<u8>);

impl W {
    fn bytes(&mut self, b: &[u8]) {
        self.0.extend_from_slice(b);
    }
    fn u8(&mut self, v: u8) {
        self.0.push(v);
    }
    fn flag(&mut self, v: bool) {
        self.0.push(v as u8);
    }
    fn u16(&mut self, v: u16) {
        self.bytes(&v.to_le_bytes());
    }
    fn u32(&mut self, v: u32) {
        self.bytes(&v.to_le_bytes());
    }
    fn u64(&mut self, v: u64) {
        self.bytes(&v.to_le_bytes());
    }
    fn i64(&mut self, v: i64) {
        self.bytes(&v.to_le_bytes());
    }
    fn f64(&mut self, v: f64) {
        self.u64(v.to_bits());
    }
    fn u32s(&mut self, v: &[u32]) {
        self.u32(v.len() as u32);
        for x in v {
            self.u32(*x);
        }
    }
}

struct R<'a> {
    data: &'a [u8],
    at: usize,
}

type Res<T> = Result<T, String>;

impl R<'_> {
    fn bytes(&mut self, n: usize) -> Res<&[u8]> {
        let end = self.at.checked_add(n).ok_or("saved machine is damaged")?;
        let out = self.data.get(self.at..end).ok_or("saved machine is cut short")?;
        self.at = end;
        Ok(out)
    }
    fn u8(&mut self) -> Res<u8> {
        Ok(self.bytes(1)?[0])
    }
    fn flag(&mut self) -> Res<bool> {
        Ok(self.u8()? != 0)
    }
    fn u16(&mut self) -> Res<u16> {
        Ok(u16::from_le_bytes(self.bytes(2)?.try_into().unwrap()))
    }
    fn u32(&mut self) -> Res<u32> {
        Ok(u32::from_le_bytes(self.bytes(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Res<u64> {
        Ok(u64::from_le_bytes(self.bytes(8)?.try_into().unwrap()))
    }
    fn i64(&mut self) -> Res<i64> {
        Ok(self.u64()? as i64)
    }
    fn f64(&mut self) -> Res<f64> {
        Ok(f64::from_bits(self.u64()?))
    }
    /// A count that the rest of the file could hold, `each` bytes apiece.
    fn count(&mut self, each: usize) -> Res<usize> {
        let n = self.u32()? as usize;
        if n.saturating_mul(each.max(1)) > self.data.len() - self.at {
            return Err("saved machine is damaged".into());
        }
        Ok(n)
    }
    fn u32s(&mut self) -> Res<Vec<u32>> {
        (0..self.count(4)?).map(|_| self.u32()).collect()
    }
}

fn put_cpu(w: &mut W, c: &Cpu) {
    let Cpu { r, pc, cc_n, cc_z, cc_v, cc_c, cc_x, sr, macc, macsr, mac_mask, halted, vbr } = c;
    for v in r.iter().chain([pc, cc_n, cc_z, cc_v, cc_c, cc_x, sr, macsr, mac_mask, halted, vbr]) {
        w.u32(*v);
    }
    for a in macc {
        w.u64(*a);
    }
}

fn get_cpu(r: &mut R) -> Res<Cpu> {
    let mut c = Cpu::default();
    {
        let Cpu { r: regs, pc, cc_n, cc_z, cc_v, cc_c, cc_x, sr, macc, macsr, mac_mask, halted, vbr } = &mut c;
        for v in regs.iter_mut().chain([pc, cc_n, cc_z, cc_v, cc_c, cc_x, sr, macsr, mac_mask, halted, vbr]) {
            *v = r.u32()?;
        }
        for a in macc {
            *a = r.u64()?;
        }
    }
    Ok(c)
}

/// Each megabyte there is: its number, its kind, then the megabyte it is
/// another view of or its bytes (none when they are all zero).
fn put_mem(w: &mut W, m: &Mem) {
    for i in 0..CHUNKS {
        let kind = match m.chunk_kind(i) {
            ChunkKind::None => continue,
            ChunkKind::Alias(of) => {
                w.u16(i as u16);
                w.u8(3);
                w.u16(of as u16);
                continue;
            }
            ChunkKind::Ram => 1,
            ChunkKind::Device => 2,
        };
        w.u16(i as u16);
        w.u8(kind);
        // Straight into the output, and taken back if it is all zero.
        let at = w.0.len();
        w.0.resize(at + CHUNK, 0);
        m.read_bytes((i * CHUNK) as u32, &mut w.0[at..]);
        if w.0[at..].iter().all(|b| *b == 0) {
            w.0.truncate(at);
            w.0[at - 1] |= ZERO;
        }
    }
    w.u16(0xffff);
}

fn get_mem(r: &mut R) -> Res<Mem> {
    let mut m = Mem::new();
    loop {
        let i = r.u16()? as usize;
        if i == 0xffff {
            return Ok(m);
        }
        let kind = r.u8()?;
        let addr = (i * CHUNK) as u32;
        if i >= CHUNKS || m.is_mapped(addr) {
            return Err("saved machine is damaged".into());
        }
        match kind & !ZERO {
            3 => {
                let of = r.u16()? as usize;
                if of >= i || m.chunk_kind(of) != ChunkKind::Ram {
                    return Err("saved machine is damaged".into());
                }
                m.alias(addr, (of * CHUNK) as u32);
                continue;
            }
            1 => m.map(addr),
            2 => m.map_device(addr),
            _ => return Err("saved machine is damaged".into()),
        }
        if kind & ZERO == 0 {
            m.write_bytes(addr, r.bytes(CHUNK)?);
        }
    }
}

fn put_clock(w: &mut W, c: &Clock) {
    let Clock { channels, ips, next, pending, held, fired, missed } = c;
    w.u8(channels.len() as u8);
    w.bytes(channels);
    w.f64(*ips);
    for n in next {
        w.flag(n.is_some());
        w.f64(n.unwrap_or(0.0));
    }
    w.u8(*pending);
    w.flag(*held);
    for v in fired.iter().chain(missed) {
        w.u64(*v);
    }
}

fn get_clock(r: &mut R) -> Res<Clock> {
    let mut c = Clock::default();
    let n = r.u8()? as usize;
    c.channels = r.bytes(n)?.to_vec();
    if c.channels.iter().any(|ch| *ch > 3) {
        return Err("saved machine is damaged".into());
    }
    c.ips = r.f64()?;
    for n in c.next.iter_mut() {
        let has = r.flag()?;
        let v = r.f64()?;
        *n = has.then_some(v);
    }
    c.pending = r.u8()?;
    c.held = r.flag()?;
    for v in c.fired.iter_mut().chain(c.missed.iter_mut()) {
        *v = r.u64()?;
    }
    Ok(c)
}

fn put_forced(w: &mut W, f: &Forced) {
    let Forced { base, first_vector, sources, asserted, delivered, fired } = f;
    w.u32(*base);
    w.u32(*first_vector);
    for v in [sources, asserted, delivered, fired] {
        w.u64(*v);
    }
}

fn get_forced(r: &mut R) -> Res<Forced> {
    Ok(Forced {
        base: r.u32()?,
        first_vector: r.u32()?,
        sources: r.u64()?,
        asserted: r.u64()?,
        delivered: r.u64()?,
        fired: r.u64()?,
    })
}

fn put_ssi(w: &mut W, s: &Ssi) {
    let Ssi {
        tx_chan,
        tx_vector,
        force_rte,
        request_hz,
        ips,
        now,
        next,
        batch,
        due,
        enabled,
        int50_asserted,
        int50_delivered,
        force_asserted,
        force_delivered,
        render,
        requests,
        major_loops,
        half_loops,
        unsupported,
    } = s;
    let Render { ipl, since, waiting } = render;
    for v in [tx_chan, tx_vector, force_rte] {
        w.u32(*v);
    }
    for v in [request_hz, ips, now] {
        w.i64(*v);
    }
    w.flag(next.is_some());
    w.bytes(&next.unwrap_or(0).to_le_bytes());
    w.flag(due.is_some());
    w.i64(due.unwrap_or(0));
    for v in [batch, enabled, int50_asserted, int50_delivered, force_asserted, force_delivered, unsupported, waiting] {
        w.flag(*v);
    }
    w.flag(ipl.is_some());
    w.u32(ipl.unwrap_or(0));
    w.i64(*since);
    for v in [requests, major_loops, half_loops] {
        w.u64(*v);
    }
}

fn get_ssi(r: &mut R) -> Res<Ssi> {
    let mut s = Ssi { tx_chan: r.u32()?, tx_vector: r.u32()?, force_rte: r.u32()?, ..Default::default() };
    s.request_hz = r.i64()?;
    s.ips = r.i64()?;
    s.now = r.i64()?;
    let has = r.flag()?;
    let next = i128::from_le_bytes(r.bytes(16)?.try_into().unwrap());
    s.next = has.then_some(next);
    let has = r.flag()?;
    let due = r.i64()?;
    s.due = has.then_some(due);
    s.batch = r.flag()?;
    s.enabled = r.flag()?;
    s.int50_asserted = r.flag()?;
    s.int50_delivered = r.flag()?;
    s.force_asserted = r.flag()?;
    s.force_delivered = r.flag()?;
    s.unsupported = r.flag()?;
    s.render.waiting = r.flag()?;
    let has = r.flag()?;
    let ipl = r.u32()?;
    s.render.ipl = has.then_some(ipl);
    s.render.since = r.i64()?;
    s.requests = r.u64()?;
    s.major_loops = r.u64()?;
    s.half_loops = r.u64()?;
    if s.request_hz <= 0 || s.ips <= 0 {
        return Err("saved machine is damaged".into());
    }
    Ok(s)
}

fn put_bank(w: &mut W, b: &Bank) {
    let Bank { claimed, done, deferred, pending, transfers, bytes_moved, refused } = b;
    w.u64(*claimed);
    for ch in 0..64 {
        w.flag(done[ch].is_some());
        w.u16(done[ch].unwrap_or(0));
        w.flag(deferred[ch]);
    }
    w.u32(pending.len() as u32);
    w.bytes(pending);
    for v in [transfers, bytes_moved, refused] {
        w.u64(*v);
    }
}

fn get_bank(r: &mut R) -> Res<Bank> {
    let mut b = Bank { claimed: r.u64()?, ..Default::default() };
    for ch in 0..64 {
        let has = r.flag()?;
        let v = r.u16()?;
        b.done[ch] = has.then_some(v);
        b.deferred[ch] = r.flag()?;
    }
    let n = r.count(1)?;
    b.pending = r.bytes(n)?.to_vec();
    if b.pending.iter().any(|ch| *ch > 63) {
        return Err("saved machine is damaged".into());
    }
    b.transfers = r.u64()?;
    b.bytes_moved = r.u64()?;
    b.refused = r.u64()?;
    Ok(b)
}

fn put_panel(w: &mut W, p: &Panel) {
    let Panel {
        select,
        frames,
        keys,
        phase,
        pads,
        led_rows,
        led_version,
        key_want,
        key_since,
        pad_want,
        pad_since,
        turns,
        turn_since,
    } = p;
    w.u8(*select);
    w.i64(*frames);
    w.bytes(keys);
    w.bytes(phase);
    for v in pads {
        w.u16(*v);
    }
    w.bytes(led_rows);
    w.u64(*led_version);
    w.u32(key_want.len() as u32);
    for ((column, bit), want) in key_want {
        w.u8(*column);
        w.u8(*bit);
        w.u32(want.len() as u32);
        for v in want {
            w.flag(*v);
        }
    }
    for v in key_since.iter().flatten() {
        w.i64(*v);
    }
    w.u32(pad_want.len() as u32);
    for (pad, want) in pad_want {
        w.u8(*pad);
        w.u32(want.len() as u32);
        for v in want {
            w.u16(*v);
        }
    }
    for v in pad_since {
        w.i64(*v);
    }
    for v in turns {
        w.u32(*v as u32);
    }
    for v in turn_since {
        w.i64(*v);
    }
}

fn get_panel(r: &mut R) -> Res<Panel> {
    let mut p = Panel { select: r.u8()?, frames: r.i64()?, ..Default::default() };
    let n = p.keys.len();
    p.keys.copy_from_slice(r.bytes(n)?);
    let n = p.phase.len();
    p.phase.copy_from_slice(r.bytes(n)?);
    for v in p.pads.iter_mut() {
        *v = r.u16()?;
    }
    p.led_rows.copy_from_slice(r.bytes(8)?);
    p.led_version = r.u64()?;
    for _ in 0..r.count(6)? {
        let key = (r.u8()?, r.u8()?);
        let mut want = VecDeque::new();
        for _ in 0..r.count(1)? {
            want.push_back(r.flag()?);
        }
        if key.0 as usize >= p.keys.len() || key.1 > 7 {
            return Err("saved machine is damaged".into());
        }
        p.key_want.push((key, want));
    }
    for v in p.key_since.iter_mut().flatten() {
        *v = r.i64()?;
    }
    for _ in 0..r.count(5)? {
        let pad = r.u8()?;
        let mut want = VecDeque::new();
        for _ in 0..r.count(2)? {
            want.push_back(r.u16()?);
        }
        if pad as usize >= p.pads.len() {
            return Err("saved machine is damaged".into());
        }
        p.pad_want.push((pad, want));
    }
    for v in p.pad_since.iter_mut() {
        *v = r.i64()?;
    }
    for v in p.turns.iter_mut() {
        *v = r.u32()? as i32;
    }
    for v in p.turn_since.iter_mut() {
        *v = r.i64()?;
    }
    Ok(p)
}

fn put_i2c(w: &mut W, i: &I2c) {
    let I2c { cr, sr, busy, expect_address, target, reading, rx, regs, pointer, fresh, transfers } = i;
    w.bytes(&[*cr, *sr, *busy as u8, *expect_address as u8, *target as u8, *reading as u8, *rx, *pointer, *fresh as u8]);
    w.bytes(regs);
    w.u64(*transfers);
}

fn get_i2c(r: &mut R) -> Res<I2c> {
    let mut i = I2c::default();
    let b = r.bytes(9)?;
    i.cr = b[0];
    i.sr = b[1];
    i.busy = b[2] != 0;
    i.expect_address = b[3] != 0;
    i.target = b[4] != 0;
    i.reading = b[5] != 0;
    i.rx = b[6];
    i.pointer = b[7];
    i.fresh = b[8] != 0;
    i.regs.copy_from_slice(r.bytes(256)?);
    i.transfers = r.u64()?;
    Ok(i)
}

fn sorted(set: &std::collections::HashSet<u32>) -> Vec<u32> {
    let mut v: Vec<u32> = set.iter().copied().collect();
    v.sort_unstable();
    v
}

fn put_esdhc(w: &mut W, e: &Esdhc) {
    let Esdhc { status, cmd_sem, data_sem, dma_sem, pattern, armed, commands, bytes_moved, card } = e;
    for v in [status, cmd_sem, data_sem, dma_sem, pattern] {
        w.u32(*v);
    }
    w.flag(*armed);
    w.u64(*commands);
    w.u64(*bytes_moved);
    let Card { blocks, rca, selected, erase_from, erase_to, sectors } = card;
    w.u32(*blocks);
    w.u16(*rca);
    w.flag(*selected);
    for v in [erase_from, erase_to] {
        w.flag(v.is_some());
        w.u32(v.unwrap_or(0));
    }
    w.u32(sectors.len() as u32);
    for (n, data) in sectors {
        w.u32(*n);
        w.bytes(&data[..]);
    }
}

fn get_esdhc(r: &mut R) -> Res<Esdhc> {
    let mut e = Esdhc {
        status: r.u32()?,
        cmd_sem: r.u32()?,
        data_sem: r.u32()?,
        dma_sem: r.u32()?,
        pattern: r.u32()?,
        armed: r.flag()?,
        commands: r.u64()?,
        bytes_moved: r.u64()?,
        card: Card::default(),
    };
    let c = &mut e.card;
    c.blocks = r.u32()?;
    c.rca = r.u16()?;
    c.selected = r.flag()?;
    for v in [&mut c.erase_from, &mut c.erase_to] {
        let has = r.flag()?;
        let n = r.u32()?;
        *v = has.then_some(n);
    }
    for _ in 0..r.count(4 + SECTOR)? {
        let n = r.u32()?;
        let mut data = Box::new([0u8; SECTOR]);
        data.copy_from_slice(r.bytes(SECTOR)?);
        c.sectors.insert(n, data);
    }
    Ok(e)
}

fn put_devices(w: &mut W, d: &Devices) {
    let Devices { pits, dtims, forced, ssi, bank, panel, i2c, esdhc, idle, unblock, audio, end_step, raised } = d;
    put_clock(w, &pits.0);
    let Dtims { clock, arm, now } = dtims;
    put_clock(w, clock);
    w.u8(*arm);
    w.i64(*now);
    w.u32(forced.len() as u32);
    for f in forced {
        put_forced(w, f);
    }
    put_ssi(w, ssi);
    put_bank(w, bank);
    put_panel(w, panel);
    put_i2c(w, i2c);
    w.flag(esdhc.is_some());
    if let Some(e) = esdhc {
        put_esdhc(w, e);
    }
    let Idle { addrs, spins, every } = idle;
    w.u32s(addrs);
    w.u64(*spins);
    w.u64(*every);
    let Unblock { pend, post, skip, skip_callers, satisfied } = unblock;
    w.u32s(pend);
    w.u32(*post);
    w.u32s(&sorted(skip));
    w.u32s(&sorted(skip_callers));
    w.u64(*satisfied);
    w.u32(audio.len() as u32);
    w.bytes(audio);
    w.flag(*end_step);
    w.u64(*raised);
}

fn get_devices(r: &mut R) -> Res<Devices> {
    let mut d = Devices { pits: Pits(get_clock(r)?), ..Default::default() };
    d.dtims = Dtims { clock: get_clock(r)?, arm: r.u8()?, now: r.i64()? };
    for _ in 0..r.count(40)? {
        d.forced.push(get_forced(r)?);
    }
    d.ssi = get_ssi(r)?;
    d.bank = get_bank(r)?;
    d.panel = get_panel(r)?;
    d.i2c = get_i2c(r)?;
    d.esdhc = if r.flag()? { Some(get_esdhc(r)?) } else { None };
    d.idle = Idle { addrs: r.u32s()?, spins: r.u64()?, every: r.u64()? };
    d.unblock.pend = r.u32s()?;
    d.unblock.post = r.u32()?;
    d.unblock.skip = r.u32s()?.into_iter().collect();
    d.unblock.skip_callers = r.u32s()?.into_iter().collect();
    d.unblock.satisfied = r.u64()?;
    let n = r.count(1)?;
    d.audio = r.bytes(n)?.to_vec();
    d.end_step = r.flag()?;
    d.raised = r.u64()?;
    Ok(d)
}

impl Machine {
    /// The machine as bytes, to `restore` later. It must be between steps,
    /// which it is whenever `run` has returned, and not stopped.
    pub fn save(&self) -> Result<Vec<u8>, String> {
        if self.c.halted != 0 || self.m.fault != 0 {
            return Err("the machine has stopped: not saving it".into());
        }
        let card = self.dev.borrow().esdhc.as_ref().map_or(0, |e| e.card.sectors.len() * (4 + SECTOR));
        let mut w = W(Vec::with_capacity(self.m.mapped_chunks() * CHUNK + card + (1 << 20)));
        w.bytes(MAGIC);
        w.i64(self.ips);
        w.i64(self.now);
        w.u64(self.steps);
        w.i64(self.idle_skipped);
        put_cpu(&mut w, &self.c);
        put_mem(&mut w, &self.m);
        put_devices(&mut w, &self.dev.borrow());
        w.u32(self.inputs.len() as u32);
        for i in &self.inputs {
            w.i64(i.at);
            w.u8(i.kind);
            for a in i.args {
                w.u32(a as u32);
            }
        }
        w.bytes(END);
        Ok(w.0)
    }

    /// The machine `save` wrote, on the clock it was saved with.
    pub fn restore(data: &[u8]) -> Result<Machine, String> {
        let mut r = R { data, at: 0 };
        if r.bytes(8)? != MAGIC {
            return Err("not a saved machine".into());
        }
        let ips = r.i64()?;
        let now = r.i64()?;
        let steps = r.u64()?;
        let idle_skipped = r.i64()?;
        if ips <= 0 || now < 0 {
            return Err("saved machine is damaged".into());
        }
        let c = get_cpu(&mut r)?;
        let m = get_mem(&mut r)?;
        let d = get_devices(&mut r)?;
        let mut inputs = Vec::new();
        for _ in 0..r.count(21)? {
            let (at, kind) = (r.i64()?, r.u8()?);
            inputs.push(Input { at, kind, args: [r.u32()? as i32, r.u32()? as i32, r.u32()? as i32] });
        }
        if r.bytes(4)? != END || r.at != data.len() {
            return Err("saved machine is damaged".into());
        }
        if c.halted != 0 {
            return Err("saved machine had stopped".into());
        }
        crate::machine::blocks_fit(&m)?;
        let mut k = Machine::assemble(c, m, d, ips, inputs);
        k.now = now;
        k.steps = steps;
        k.idle_skipped = idle_skipped;
        Ok(k)
    }
}
