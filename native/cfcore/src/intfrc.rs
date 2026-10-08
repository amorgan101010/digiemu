//! Software-forced interrupts: sources asserted by a bit in an interrupt
//! controller's INTFRCH/INTFRCL registers. The firmware chains its
//! sequencer tick and its panel-scan processing this way.
//!
//! A port of digiemu's `emu/intfrc.py` (`ForcedInterrupts`), checked call
//! by call against a recording of it by `cfreplay`.
use crate::timers::{interrupt_level_unmasked, Host};

const INTFRCH: u32 = 0x10;
/// How often a source the CPU's interrupt mask refused is offered again.
const PENDING_STEP: i64 = 2048;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Forced {
    /// The interrupt controller's base address.
    pub base: u32,
    /// The vector of its source 0.
    pub first_vector: u32,
    /// The sources watched, asserted now, and already delivered for this
    /// assertion: one bit per source.
    pub sources: u64,
    pub asserted: u64,
    pub delivered: u64,
    pub fired: u64,
}

impl Forced {
    pub fn owns(&self, write: bool, addr: u32) -> bool {
        write && (self.base + INTFRCH..=self.base + INTFRCH + 7).contains(&addr)
    }

    /// The firmware is about to write a force register. The hook runs
    /// before the store lands, so the value is merged in by hand.
    pub fn write(&mut self, h: &mut dyn Host, addr: u32, size: u32, value: u32) {
        let mut regs = [0u8; 8];
        h.read(self.base + INTFRCH, &mut regs);
        let off = (addr - (self.base + INTFRCH)) as usize;
        let bytes = value.to_be_bytes();
        for i in 0..size as usize {
            if off + i < 8 {
                regs[off + i] = bytes[4 - size as usize + i];
            }
        }
        let now = u64::from_be_bytes(regs) & self.sources;
        // A source cleared (the handler's acknowledge) or newly set is
        // armed again.
        self.delivered &= now & self.asserted;
        self.asserted = now;
    }

    /// Instructions to run before the next `service`, None for no limit.
    pub fn step(&mut self, h: &mut dyn Host, done: i64) -> Option<i64> {
        let waiting = self.asserted & !self.delivered;
        if waiting == 0 {
            return None;
        }
        // Python walks a set here; with one source waiting the order cannot
        // matter, and the replay checks the level of every question asked.
        for src in 0..64 {
            if waiting & (1 << src) != 0 {
                let level = interrupt_level_unmasked(h, self.first_vector + src);
                if !h.render_holds(done, level) {
                    return Some(PENDING_STEP);
                }
            }
        }
        None
    }

    /// Deliver what the CPU's interrupt mask allows now.
    pub fn service(&mut self, h: &mut dyn Host, _done: i64) {
        let waiting = self.asserted & !self.delivered;
        if waiting == 0 {
            return;
        }
        let mut ready = Vec::new();
        for src in 0..64u32 {
            if waiting & (1 << src) != 0 {
                match interrupt_level_unmasked(h, self.first_vector + src) {
                    // Level 0 never interrupts: retire the assertion.
                    None => self.delivered |= 1 << src,
                    Some(level) => ready.push((level, src)),
                }
            }
        }
        // Highest level first; within a level, the highest source number.
        ready.sort_unstable_by(|a, b| b.cmp(a));
        for (level, src) in ready {
            if (h.sr() >> 8) & 7 >= level {
                continue;
            }
            if h.raise(self.first_vector + src, Some(level)) {
                self.delivered |= 1 << src;
                self.fired += 1;
            }
        }
    }
}
