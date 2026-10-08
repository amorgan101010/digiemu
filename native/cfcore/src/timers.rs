//! The MCF5441x's PIT and DMA timers on the emulator's instruction clock.
//!
//! A port of digiemu's `emu/pit.py` (`Pits`) and `emu/dtim.py` (`Dtims`),
//! which say why each rule is what it is. The arithmetic is kept in the same
//! order and in f64, as Python does it, so the deadlines are the same bits:
//! `cfreplay` checks every `step` result and every write `service` makes
//! against a recording of the Python models.
//!
//! A timer does not touch the machine directly. It goes through `Host`, so
//! the same code runs against the real CPU and memory or against a
//! recording that checks it.

/// What a timer model needs from the machine it is part of.
pub trait Host {
    /// Read device or RAM bytes as the host sees them (no hook runs).
    fn read(&mut self, addr: u32, out: &mut [u8]);
    /// Write bytes as the host does (no hook runs).
    fn write(&mut self, addr: u32, data: &[u8]);
    /// The CPU's status register.
    fn sr(&mut self) -> u32;
    /// Enter exception `vec`, at interrupt `level` (None leaves the
    /// interrupt mask alone). -> false if the vector is empty and nothing
    /// was done.
    fn raise(&mut self, vec: u32, level: Option<u32>) -> bool;
    /// Can a tick waiting at `level` be left to the end of the audio render
    /// (`emu.pit.render_holds`)? The render belongs to the SSI model.
    fn render_holds(&mut self, done: i64, level: Option<u32>) -> bool;
    /// The CPU's stack pointer.
    fn a7(&mut self) -> u32;
    /// Samples leaving on the audio output, as the eDMA read them.
    fn audio(&mut self, data: &[u8]);
    /// End the engine's current step at the next block.
    fn end_step(&mut self);
    /// Is there memory at `addr`?
    fn mapped(&mut self, addr: u32) -> bool;
}

const F_BUS: f64 = 132_000_000.0;
/// How far to run when every timer is off.
const IDLE_STEP: i64 = 1_000_000;
/// How often a tick the CPU's interrupt mask refused is offered again.
const PENDING_STEP: i64 = 2048;
/// How soon to look again after the firmware wrote a DMA timer's registers.
const ARM_STEP: i64 = 256;

/// (base, first vector) of the three interrupt controllers.
const INTC: [(u32, u32); 3] = [(0xFC04_8000, 64), (0xFC04_C000, 128), (0xFC05_0000, 192)];
const ICR_BASE: u32 = 0x40;
const IMR_BASE: u32 = 0x08;

fn be16(b: &[u8]) -> u32 {
    u16::from_be_bytes([b[0], b[1]]) as u32
}

fn be32(b: &[u8]) -> u32 {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]])
}

/// A vector's programmed interrupt level, None if its source is disabled.
/// A forced source bypasses the mask registers, so this does not read them.
pub fn interrupt_level_unmasked(h: &mut dyn Host, vec: u32) -> Option<u32> {
    let (base, first) = INTC.iter().copied().find(|(_, first)| (*first..first + 64).contains(&vec))?;
    let mut icr = [0u8; 1];
    h.read(base + ICR_BASE + (vec - first), &mut icr);
    match (icr[0] & 7) as u32 {
        0 => None,
        level => Some(level),
    }
}

/// A vector's programmed interrupt level, None if its source is disabled
/// or masked at the interrupt controller.
pub fn interrupt_level(h: &mut dyn Host, vec: u32) -> Option<u32> {
    let (base, first) = INTC.iter().copied().find(|(_, first)| (*first..first + 64).contains(&vec))?;
    let src = vec - first;
    let icr = interrupt_level_unmasked(h, vec)?;
    let mut imr = [0u8; 8];
    h.read(base + IMR_BASE, &mut imr);
    let (imrh, imrl) = (be32(&imr[..4]), be32(&imr[4..]));
    let masked = if src < 32 { (imrl >> src) & 1 } else { (imrh >> (src - 32)) & 1 };
    if masked != 0 {
        None
    } else {
        Some(icr)
    }
}

/// The state both timer families share.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Clock {
    /// The channels delivered, in tie-break order.
    pub channels: Vec<u8>,
    /// Instructions per second of emulated time.
    pub ips: f64,
    /// Each channel's next deadline, an absolute instruction count.
    pub next: [Option<f64>; 4],
    /// Channels with a tick the interrupt mask has refused so far (bits).
    pub pending: u8,
    pub held: bool,
    pub fired: [u64; 4],
    pub missed: [u64; 4],
}

impl Clock {
    /// Could a `service` now only re-check whether channels are on?
    fn nothing_due(&self, done: i64) -> bool {
        if self.pending != 0 {
            return false;
        }
        self.channels.iter().all(|&ch| match self.next[ch as usize] {
            Some(n) => (done as f64) < n,
            None => false,
        })
    }

    /// The step to the deadline `d`, shortened while a tick waits.
    fn step_to(&self, h: &mut dyn Host, done: i64, d: Option<f64>, vectors: &[u32; 4], arm: bool) -> i64 {
        let mut n = match d {
            None => IDLE_STEP,
            Some(d) => ((d - done as f64).ceil() as i64).max(1),
        };
        if arm {
            n = n.min(ARM_STEP);
        }
        if self.pending != 0 {
            let mut all = true;
            for ch in 0..4 {
                if self.pending & (1 << ch) != 0 {
                    let level = interrupt_level(h, vectors[ch]);
                    if !h.render_holds(done, level) {
                        all = false;
                        break;
                    }
                }
            }
            if !all {
                n = n.min(PENDING_STEP);
            }
        }
        n.max(1)
    }

    /// Offer channel `ch`'s waiting tick to the CPU.
    fn deliver(&mut self, h: &mut dyn Host, ch: u8, vec: u32) {
        let bit = 1u8 << ch;
        let Some(level) = interrupt_level(h, vec) else {
            self.pending &= !bit;
            self.missed[ch as usize] += 1;
            return;
        };
        if (h.sr() >> 8) & 7 >= level {
            return;
        }
        self.pending &= !bit;
        if h.raise(vec, Some(level)) {
            self.fired[ch as usize] += 1;
        }
    }
}

/// The four programmable interrupt timers.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Pits(pub Clock);

const PIT_BASES: [u32; 4] = [0xFC08_0000, 0xFC08_4000, 0xFC08_8000, 0xFC08_C000];
const PIT_VECTORS: [u32; 4] = [205, 206, 207, 208];

impl Pits {
    /// Instructions between interrupts, None while the timer is off.
    fn period(&self, h: &mut dyn Host, ch: u8) -> Option<f64> {
        let mut raw = [0u8; 4];
        h.read(PIT_BASES[ch as usize], &mut raw);
        let (pcsr, pmr) = (be16(&raw[..2]), be16(&raw[2..]));
        if pcsr & 0x01 == 0 || pcsr & 0x08 == 0 {
            return None;
        }
        let prescale = 1u64 << (((pcsr >> 8) & 0xF) + 1);
        Some((prescale * (pmr as u64 + 1)) as f64 / F_BUS * self.0.ips)
    }

    fn deadline(&mut self, h: &mut dyn Host, done: i64) -> Option<f64> {
        if self.0.held {
            return None;
        }
        let mut best: Option<f64> = None;
        for i in 0..self.0.channels.len() {
            let ch = self.0.channels[i];
            let Some(p) = self.period(h, ch) else {
                self.0.next[ch as usize] = None;
                continue;
            };
            let next = *self.0.next[ch as usize].get_or_insert(done as f64 + p);
            if best.map_or(true, |b| next < b) {
                best = Some(next);
            }
        }
        best
    }

    /// Instructions to run before the next `service` is due.
    pub fn step(&mut self, h: &mut dyn Host, done: i64) -> i64 {
        let d = self.deadline(h, done);
        self.0.step_to(h, done, d, &PIT_VECTORS, false)
    }

    /// Take what is due at instruction count `done`.
    pub fn service(&mut self, h: &mut dyn Host, done: i64) {
        if self.0.held || self.0.nothing_due(done) {
            return;
        }
        for i in 0..self.0.channels.len() {
            let ch = self.0.channels[i];
            let (c, bit) = (ch as usize, 1u8 << ch);
            let Some(p) = self.period(h, ch) else {
                self.0.next[c] = None;
                self.0.pending &= !bit;
                continue;
            };
            let Some(next) = self.0.next[c] else {
                self.0.next[c] = Some(done as f64 + p);
                continue;
            };
            if done as f64 >= next {
                let mut n = next + p;
                if n <= done as f64 {
                    n = done as f64 + p;
                }
                self.0.next[c] = Some(n);
                if self.0.pending & bit != 0 {
                    self.0.missed[c] += 1;
                }
                self.0.pending |= bit;
            }
            if self.0.pending & bit != 0 {
                self.0.deliver(h, ch, PIT_VECTORS[c]);
            }
        }
    }
}

/// The four DMA timers.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Dtims {
    pub clock: Clock,
    /// Channels whose registers the firmware wrote since the last step.
    pub arm: u8,
    /// The instruction count at the last step or service.
    pub now: i64,
}

const DTIM_BASES: [u32; 4] = [0xFC07_0000, 0xFC07_4000, 0xFC07_8000, 0xFC07_C000];
const DTIM_VECTORS: [u32; 4] = [96, 97, 98, 99];
const DTER: u32 = 0x03;
const DTCN: u32 = 0x0C;
const RST: u32 = 0x01;
const FRR: u32 = 0x08;
const ORRI: u32 = 0x10;
const DMAEN: u32 = 0x80;

impl Dtims {
    fn period(&self, h: &mut dyn Host, ch: u8) -> Option<f64> {
        let mut raw = [0u8; 8];
        h.read(DTIM_BASES[ch as usize], &mut raw);
        let (dtmr, dtxmr, dtrr) = (be16(&raw[..2]), raw[2] as u32, be32(&raw[4..]));
        if dtmr & RST == 0 || dtmr & ORRI == 0 || dtxmr & DMAEN != 0 {
            return None;
        }
        let div = match (dtmr >> 1) & 3 {
            1 => 1u64,
            2 => 16,
            _ => return None,
        };
        let ticks = (dtrr as u64 + 1) * (((dtmr >> 8) & 0xFF) as u64 + 1) * div;
        Some(ticks as f64 / F_BUS * self.clock.ips)
    }

    fn deadline(&mut self, h: &mut dyn Host, done: i64) -> Option<f64> {
        if self.clock.held {
            return None;
        }
        let mut best: Option<f64> = None;
        for i in 0..self.clock.channels.len() {
            let ch = self.clock.channels[i];
            let p = self.period(h, ch);
            self.arm &= !(1 << ch);
            let Some(p) = p else {
                self.clock.next[ch as usize] = None;
                continue;
            };
            let next = *self.clock.next[ch as usize].get_or_insert(done as f64 + p);
            if best.map_or(true, |b| next < b) {
                best = Some(next);
            }
        }
        best
    }

    pub fn step(&mut self, h: &mut dyn Host, done: i64) -> i64 {
        self.now = done;
        let d = self.deadline(h, done);
        let arm = self.arm != 0;
        self.clock.step_to(h, done, d, &DTIM_VECTORS, arm)
    }

    pub fn service(&mut self, h: &mut dyn Host, done: i64) {
        self.now = done;
        if self.clock.held || self.clock.nothing_due(done) {
            return;
        }
        for i in 0..self.clock.channels.len() {
            let ch = self.clock.channels[i];
            let (c, bit) = (ch as usize, 1u8 << ch);
            let Some(p) = self.period(h, ch) else {
                self.clock.next[c] = None;
                self.clock.pending &= !bit;
                continue;
            };
            let Some(next) = self.clock.next[c] else {
                self.clock.next[c] = Some(done as f64 + p);
                continue;
            };
            if done as f64 >= next {
                let mut n = next + p;
                if n <= done as f64 {
                    n = done as f64 + p;
                }
                self.clock.next[c] = Some(n);
                // The reference reached: the event register's REF bit.
                let mut dter = [0u8; 1];
                h.read(DTIM_BASES[c] + DTER, &mut dter);
                h.write(DTIM_BASES[c] + DTER, &[dter[0] | 0x02]);
                if self.clock.pending & bit != 0 {
                    self.clock.missed[c] += 1;
                }
                self.clock.pending |= bit;
            }
            if self.clock.pending & bit != 0 {
                self.clock.deliver(h, ch, DTIM_VECTORS[c]);
            }
        }
    }

    /// Does a guest access at `addr` belong to this model?
    pub fn owns(&self, write: bool, addr: u32) -> bool {
        DTIM_BASES.iter().enumerate().any(|(ch, &b)| {
            if write {
                self.clock.channels.contains(&(ch as u8)) && (b..=b + 0x0F).contains(&addr)
            } else {
                (b + DTCN..=b + DTCN + 3).contains(&addr)
            }
        })
    }

    /// The firmware is about to read or write a DMA timer register.
    pub fn access(&mut self, h: &mut dyn Host, write: bool, addr: u32) {
        let ch = ((addr - DTIM_BASES[0]) >> 14) as usize & 3;
        if write {
            self.arm |= 1 << ch;
        } else {
            self.serve_count(h, ch);
        }
    }

    /// Put DTCNn's value in place just before the firmware reads it.
    fn serve_count(&mut self, h: &mut dyn Host, ch: usize) {
        let mut raw = [0u8; 8];
        h.read(DTIM_BASES[ch], &mut raw);
        let (dtmr, dtrr) = (be16(&raw[..2]), be32(&raw[4..]));
        let div = if dtmr & RST != 0 {
            match (dtmr >> 1) & 3 {
                1 => ((dtmr >> 8) & 0xFF) as u64 + 1,
                2 => (((dtmr >> 8) & 0xFF) as u64 + 1) * 16,
                _ => return,
            }
        } else if ch == 0 && dtmr == 0 {
            1 // as the boot ROM leaves it
        } else {
            return;
        };
        let mut count = (self.now as f64 / self.clock.ips * F_BUS / div as f64) as i64;
        if dtmr & RST != 0 && dtmr & FRR != 0 {
            count %= dtrr as i64 + 1;
        }
        h.write(DTIM_BASES[ch] + DTCN, &(count as u32).to_be_bytes());
    }
}
