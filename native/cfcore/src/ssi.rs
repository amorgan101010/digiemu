//! The audio output path: SSI0 asking eDMA channel 50 for samples at the
//! sample rate, the channel's half and major interrupts (vector 170), and
//! the render interrupt the firmware forces from that handler (vector 191).
//!
//! A port of digiemu's `emu/ssi.py` (`Ssi0Dma`) for the Models' profile,
//! which has a transmit channel and no receive channel. `cfreplay` checks it
//! call by call against a recording of the Python model, the samples it
//! hands on included.
use crate::timers::{interrupt_level, interrupt_level_unmasked, Host};

const SERQ: u32 = 0xFC04_4018;
const CINT: u32 = 0xFC04_401C;
const TCD_BASE: u32 = 0xFC04_5000;
const INTFRCH1: u32 = 0xFC04_C010;
const FORCE_VECTOR: u32 = 191;
/// A render window older than this is not trusted to end the step itself.
const RENDER_MAX: i64 = 200_000;

const SADDR: u32 = 0x00;
const ATTR: u32 = 0x04;
const SOFF: u32 = 0x06;
const NBYTES: u32 = 0x08;
const SLAST: u32 = 0x0C;
const DADDR: u32 = 0x10;
const CITER: u32 = 0x14;
const DOFF: u32 = 0x16;
const DLAST: u32 = 0x18;
const BITER: u32 = 0x1C;
const CSR: u32 = 0x1E;

/// The audio render in progress, which the timers ask about: while it runs
/// at a raised interrupt level, a waiting tick is left to its `rte`.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Render {
    /// The level the render holds, None when none is running.
    pub ipl: Option<u32>,
    /// The instruction count it started at.
    pub since: i64,
    /// A tick is waiting for it to end.
    pub waiting: bool,
}

impl Render {
    /// `emu.pit.render_holds`, without its side effect.
    pub fn would_hold(&self, done: i64, level: Option<u32>) -> bool {
        match (self.ipl, level) {
            (Some(ipl), Some(level)) => level <= ipl && done - self.since <= RENDER_MAX,
            _ => false,
        }
    }

    /// Can a tick at `level` wait for the render's end? Saying yes asks the
    /// render's `rte` to end the step.
    pub fn holds(&mut self, done: i64, level: Option<u32>) -> bool {
        let yes = self.would_hold(done, level);
        self.waiting |= yes;
        yes
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Ssi {
    pub tx_chan: u32,
    pub tx_vector: u32,
    /// The address of the `rte` of the handler that forces the render.
    pub force_rte: u32,
    /// Sample requests per second, and instructions per second.
    pub request_hz: i64,
    pub ips: i64,
    pub now: i64,
    /// The next request's instruction count, times `request_hz` (the period
    /// is `ips / request_hz`, which is not a whole number).
    pub next: Option<i128>,
    /// Run the requests up to the next interrupt in one go.
    pub batch: bool,
    /// Requests until the next half or major boundary, once worked out.
    pub due: Option<i64>,
    pub enabled: bool,
    pub int50_asserted: bool,
    pub int50_delivered: bool,
    pub force_asserted: bool,
    pub force_delivered: bool,
    pub render: Render,
    pub requests: u64,
    pub major_loops: u64,
    pub half_loops: u64,
    /// The channel was programmed in a way this model does not handle.
    pub unsupported: bool,
}

fn ceil_div(n: i128, d: i128) -> i128 {
    n.div_euclid(d) + (n.rem_euclid(d) != 0) as i128
}

impl Ssi {
    fn tcd(&self) -> u32 {
        TCD_BASE + self.tx_chan * 0x20
    }

    fn u16(&self, h: &mut dyn Host, off: u32) -> u32 {
        let mut b = [0u8; 2];
        h.read(self.tcd() + off, &mut b);
        u16::from_be_bytes(b) as u32
    }

    fn u32(&self, h: &mut dyn Host, off: u32) -> u32 {
        let mut b = [0u8; 4];
        h.read(self.tcd() + off, &mut b);
        u32::from_be_bytes(b)
    }

    fn w16(&self, h: &mut dyn Host, off: u32, v: u32) {
        h.write(self.tcd() + off, &(v as u16).to_be_bytes());
    }

    fn w32(&self, h: &mut dyn Host, off: u32, v: u32) {
        h.write(self.tcd() + off, &v.to_be_bytes());
    }

    fn hz(&self) -> i128 {
        self.request_hz as i128
    }

    fn period_ceil(&self) -> i64 {
        ceil_div(self.ips as i128, self.hz()).max(1) as i64
    }

    /// Requests until the transmit channel's next half or major boundary.
    fn frames_to_event(&self, h: &mut dyn Host) -> i64 {
        if !self.batch || !self.enabled {
            return 1;
        }
        let citer = self.u16(h, CITER) & 0x7FFF;
        let biter = self.u16(h, BITER) & 0x7FFF;
        if citer == 0 {
            return 1;
        }
        let half = biter / 2;
        if self.u16(h, CSR) & 0x0004 != 0 && citer > half {
            (citer - half) as i64
        } else {
            citer as i64
        }
    }

    /// The instruction count the next event is due at, rounded up.
    fn deadline_ceil(&mut self, h: &mut dyn Host) -> i64 {
        let due = match self.due {
            Some(due) => due,
            None => {
                let due = self.frames_to_event(h);
                self.due = Some(due);
                due
            }
        };
        let at = self.next.unwrap_or(0) + self.ips as i128 * (due as i128 - 1);
        ceil_div(at, self.hz()) as i64
    }

    /// Instructions to run before the next `service`, None for no limit.
    pub fn step(&mut self, h: &mut dyn Host, done: i64) -> Option<i64> {
        self.now = done;
        if !self.enabled {
            return None;
        }
        if self.next.is_none() {
            self.next = Some(done as i128 * self.hz() + self.ips as i128);
        }
        let mut step = (self.deadline_ceil(h) - done).max(1);
        if self.batch
            && ((self.int50_asserted && !self.int50_delivered)
                || (self.force_asserted && !self.force_delivered))
        {
            step = step.min(self.period_ceil());
        }
        Some(step)
    }

    pub fn service(&mut self, h: &mut dyn Host, done: i64) {
        self.now = done;
        if self.next.is_some() && done >= self.deadline_ceil(h) {
            let n = self.due.unwrap_or(1);
            self.requests += n as u64;
            self.run_minors(h, n);
            let mut next = self.next.unwrap_or(0) + self.ips as i128 * n as i128;
            if next <= done as i128 * self.hz() {
                next = done as i128 * self.hz() + self.ips as i128;
            }
            self.next = Some(next);
            self.due = None;
        }
        self.deliver_vector170(h);
        self.deliver_vector191(h);
    }

    /// `n` minor loops, as one copy when the channel's shape allows it.
    fn run_minors(&mut self, h: &mut dyn Host, n: i64) {
        if n == 1 || !self.enabled {
            for _ in 0..n {
                self.run_minor(h);
            }
            return;
        }
        let mut raw = [0u8; 0x20];
        h.read(self.tcd(), &mut raw);
        let be16 = |o: u32| u16::from_be_bytes([raw[o as usize], raw[o as usize + 1]]) as u32;
        let be32 = |o: u32| u32::from_be_bytes(raw[o as usize..o as usize + 4].try_into().unwrap());
        let (mut source, attr, soff, nbytes) = (be32(SADDR), be16(ATTR), be16(SOFF) as u16 as i16, be32(NBYTES));
        let (dest, citer_raw, doff) = (be32(DADDR), be16(CITER), be16(DOFF) as u16 as i16);
        let (biter_raw, csr) = (be16(BITER), be16(CSR));
        let mut citer = citer_raw & 0x7FFF;
        let simple = citer_raw & 0x8000 == 0
            && biter_raw & 0x8000 == 0
            && attr & 7 == 2
            && (attr >> 8) & 7 == 2
            && nbytes != 0
            && nbytes % 4 == 0
            && n as u32 <= citer
            && soff == 4
            && doff == 0;
        if !simple {
            for _ in 0..n {
                self.run_minor(h);
            }
            return;
        }
        let total = nbytes * n as u32;
        let mut captured = vec![0u8; total as usize];
        h.read(source, &mut captured);
        source = source.wrapping_add(total);
        citer -= n as u32;
        raw[SADDR as usize..SADDR as usize + 4].copy_from_slice(&source.to_be_bytes());
        raw[DADDR as usize..DADDR as usize + 4].copy_from_slice(&dest.to_be_bytes());
        raw[CITER as usize..CITER as usize + 2].copy_from_slice(&(citer as u16).to_be_bytes());
        h.write(self.tcd(), &raw);
        if !captured.is_empty() {
            h.audio(&captured);
        }
        if citer != 0 {
            let half = (biter_raw & 0x7FFF) / 2;
            if csr & 0x0004 != 0 && citer + n as u32 > half && half >= citer {
                self.half_loops += 1;
                self.int50_asserted = true;
                self.int50_delivered = false;
            }
            return;
        }
        self.complete_major(h, source, dest, biter_raw);
    }

    fn run_minor(&mut self, h: &mut dyn Host) {
        if !self.enabled {
            return;
        }
        let citer_raw = self.u16(h, CITER);
        let biter_raw = self.u16(h, BITER);
        if citer_raw & 0x8000 != 0 || biter_raw & 0x8000 != 0 {
            self.unsupported = true;
            return;
        }
        let mut citer = citer_raw & 0x7FFF;
        if citer == 0 {
            return;
        }
        let attr = self.u16(h, ATTR);
        let nbytes = self.u32(h, NBYTES);
        if attr & 7 != 2 || (attr >> 8) & 7 != 2 || nbytes == 0 || nbytes % 4 != 0 {
            self.unsupported = true;
            return;
        }
        let mut source = self.u32(h, SADDR);
        let mut dest = self.u32(h, DADDR);
        let soff = self.u16(h, SOFF) as u16 as i16 as i32 as u32;
        let doff = self.u16(h, DOFF) as u16 as i16 as i32 as u32;
        let mut captured = Vec::with_capacity(nbytes as usize);
        for _ in 0..nbytes / 4 {
            let mut beat = [0u8; 4];
            h.read(source, &mut beat);
            captured.extend_from_slice(&beat);
            source = source.wrapping_add(soff);
            dest = dest.wrapping_add(doff);
        }
        self.w32(h, SADDR, source);
        self.w32(h, DADDR, dest);
        citer -= 1;
        self.w16(h, CITER, citer);
        if !captured.is_empty() {
            h.audio(&captured);
        }
        if citer != 0 {
            let csr = self.u16(h, CSR);
            if csr & 0x0004 != 0 && citer == (biter_raw & 0x7FFF) / 2 {
                self.half_loops += 1;
                self.int50_asserted = true;
                self.int50_delivered = false;
            }
            return;
        }
        self.complete_major(h, source, dest, biter_raw);
    }

    /// Major loop done: SLAST, then DLAST or scatter-gather, then the
    /// interrupt.
    fn complete_major(&mut self, h: &mut dyn Host, source: u32, dest: u32, biter_raw: u32) {
        let csr = self.u16(h, CSR);
        let source = source.wrapping_add(self.u32(h, SLAST));
        self.w32(h, SADDR, source);
        self.major_loops += 1;
        if csr & 0x0010 != 0 {
            let pointer = self.u32(h, DLAST);
            if pointer & 0x1F != 0 {
                self.unsupported = true;
                return;
            }
            let mut descriptor = [0u8; 0x20];
            h.read(pointer, &mut descriptor);
            h.write(self.tcd(), &descriptor);
        } else {
            let dest = dest.wrapping_add(self.u32(h, DLAST));
            self.w32(h, DADDR, dest);
            self.w16(h, CITER, biter_raw);
        }
        if csr & 0x0002 != 0 {
            self.int50_asserted = true;
            self.int50_delivered = false;
        }
    }

    fn deliver_vector170(&mut self, h: &mut dyn Host) {
        if !self.int50_asserted || self.int50_delivered {
            return;
        }
        let Some(level) = interrupt_level(h, self.tx_vector) else { return };
        if (h.sr() >> 8) & 7 >= level {
            return;
        }
        if h.raise(self.tx_vector, Some(level)) {
            self.int50_delivered = true;
            // The handler forces the render at a level it holds off itself.
            if let Some(render) = interrupt_level_unmasked(h, FORCE_VECTOR) {
                if render <= level {
                    self.render_started(render);
                }
            }
        }
    }

    /// Offer the forced render again if the interrupt mask blocked it.
    fn deliver_vector191(&mut self, h: &mut dyn Host) {
        if !self.force_asserted || self.force_delivered {
            return;
        }
        let Some(level) = interrupt_level_unmasked(h, FORCE_VECTOR) else { return };
        if (h.sr() >> 8) & 7 >= level {
            return;
        }
        if h.raise(FORCE_VECTOR, Some(level)) {
            self.force_delivered = true;
        }
    }

    fn render_started(&mut self, level: u32) {
        if self.render.ipl.is_none() {
            self.render.since = self.now;
            self.render.waiting = false;
        }
        self.render.ipl = Some(level);
    }

    fn render_returned(&mut self, h: &mut dyn Host) {
        if self.render.ipl.take().is_some() && self.render.waiting {
            self.render.waiting = false;
            h.end_step();
        }
    }

    pub fn owns(&self, write: bool, addr: u32) -> bool {
        write && (addr == SERQ || addr == CINT || (INTFRCH1..=INTFRCH1 + 3).contains(&addr))
    }

    /// The firmware is about to write one of the registers this model
    /// watches: the eDMA's request-enable and interrupt-clear bytes, and the
    /// force register the render's source is in.
    pub fn write(&mut self, h: &mut dyn Host, addr: u32, size: u32, value: u32) {
        if addr == SERQ {
            if size != 1 || value & 0x80 != 0 {
                return;
            }
            if value & 0x40 != 0 || value & 0x3F == self.tx_chan {
                self.enabled = true;
            }
            if self.enabled && self.next.is_none() {
                self.next = Some(self.now as i128 * self.hz() + self.ips as i128);
            }
        } else if addr == CINT {
            if size == 1 && (value & 0x40 != 0 || value & 0x3F == self.tx_chan) {
                self.int50_asserted = false;
                self.int50_delivered = false;
            }
        } else {
            // The hook runs before the store lands: merge it in by hand.
            let mut current = [0u8; 4];
            h.read(INTFRCH1, &mut current);
            let off = (addr - INTFRCH1) as usize;
            let bytes = value.to_be_bytes();
            for i in 0..size as usize {
                if off + i < 4 {
                    current[off + i] = bytes[4 - size as usize + i];
                }
            }
            let asserted = u32::from_be_bytes(current) & 0x8000_0000 != 0;
            if asserted && !self.force_asserted {
                self.force_delivered = false;
            }
            self.force_asserted = asserted;
            if !asserted {
                self.force_delivered = false;
            }
        }
    }

    /// The CPU is about to run the `rte` of the handler that forces the
    /// render: enter the render from here if it is asserted and the frame
    /// being returned to allows its level, or note that a render has ended.
    pub fn force_rte(&mut self, h: &mut dyn Host) {
        if !self.force_asserted || self.force_delivered {
            self.render_returned(h);
            return;
        }
        let Some(level) = interrupt_level_unmasked(h, FORCE_VECTOR) else { return };
        let mut saved = [0u8; 2];
        let sp = h.a7();
        h.read(sp.wrapping_add(2), &mut saved);
        let saved_sr = u16::from_be_bytes(saved) as u32;
        if (saved_sr >> 8) & 7 < level && h.raise(FORCE_VECTOR, Some(level)) {
            self.force_delivered = true;
            self.render_started(level);
        }
    }
}
