//! A ColdFire V4e/EMAC core with no runtime code generation: an interpreter,
//! and blocks translated ahead of time from one firmware image (`cfgen`).
//! iOS does not let an app make executable memory, so this is how the
//! emulated CPU can run there; digiemu's desktop engine stays the reference.
pub mod board;
pub mod cpu;
pub mod edma;
pub mod ffi;
pub mod insn;
pub mod interp;
pub mod machine;
pub mod intfrc;
pub mod ops;
pub mod rtos;
pub mod ssi;
pub mod timers;
pub mod trace;
pub mod workload;

pub use cpu::{Cpu, Mem};

/// The translated blocks: `has(pc)` and `run(cpu, mem, budget)`. `run`
/// executes whole blocks while each fits in the instruction budget.
/// Empty unless the build was given CFCORE_AOT (see build.rs).
pub mod aot {
    #![allow(unused_imports, clippy::all)]
    use crate::cpu::{Cpu, Mem};
    use crate::insn::{Alu::*, Ea::*, Sz::*};
    use crate::ops;
    include!(concat!(env!("OUT_DIR"), "/aot_gen.rs"));
}

/// Run exactly `insns` instructions, or up to an unimplemented instruction
/// or a memory fault: translated blocks where there are any and they fit,
/// the interpreter otherwise.
pub fn run_mixed(c: &mut Cpu, m: &mut Mem, it: &mut interp::Interp, insns: u64) {
    let mut left = insns as i64;
    while left > 0 && c.halted == 0 && m.fault == 0 {
        if aot::has(c.pc) {
            let before = left;
            aot::run(c, m, &mut left);
            if left != before {
                continue;
            }
        }
        it.step(c, m);
        left -= 1;
    }
}
