//! The interpreter: decode once per address, then run the handlers.
use crate::cpu::{Cpu, Mem};
use crate::insn::{decode, exec, Insn};

const SLOTS: usize = 1 << 16;

pub struct Interp {
    /// Direct-mapped decode cache: (address, instruction, length).
    cache: Vec<(u32, Insn, u32)>,
    pub executed: u64,
}

impl Default for Interp {
    fn default() -> Self {
        Self::new()
    }
}

impl Interp {
    pub fn new() -> Self {
        // No instruction is at an odd address, so 1 marks an empty slot.
        Interp { cache: vec![(1, Insn::Nop(), 0); SLOTS], executed: 0 }
    }

    /// Run one instruction. -> whether it chose the next PC itself.
    #[inline]
    pub fn step(&mut self, c: &mut Cpu, m: &mut Mem) -> bool {
        let pc = c.pc;
        let slot = ((pc >> 1) as usize) & (SLOTS - 1);
        let (at, insn, len) = self.cache[slot];
        let (insn, len) = if at == pc {
            (insn, len)
        } else {
            let (insn, len) = decode(&mut |a| m.r16(a) as u16, pc);
            self.cache[slot] = (pc, insn, len);
            (insn, len)
        };
        exec(c, m, insn);
        if !insn.is_flow() {
            c.pc = pc.wrapping_add(len);
        }
        self.executed += 1;
        insn.is_flow()
    }

    /// Run until `stop`, an unimplemented instruction, a fault or `max`
    /// instructions.
    pub fn run(&mut self, c: &mut Cpu, m: &mut Mem, stop: u32, max: u64) {
        let end = self.executed + max;
        while c.pc != stop && c.halted == 0 && m.fault == 0 && self.executed < end {
            self.step(c, m);
        }
    }
}
