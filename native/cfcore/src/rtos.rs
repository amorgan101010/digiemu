//! Two things digiemu does to the firmware's RTOS from outside, at code
//! addresses it hooks (`emu/longrun.py`, `build`):
//!
//! - **Idle.** The idle task spins on a one-instruction loop. Reaching it
//!   ends the engine's step, the rest of the step is credited as spun, and
//!   every `idle_yield` passes the reschedule vector (32) is raised, which
//!   is what moves the RTOS on when nothing else interrupts.
//! - **Unblock.** A task about to wait on a semaphore nothing in the
//!   emulator will ever post has it posted for it. Semaphores that do have a
//!   poster are left alone: a fixed list, plus any seen posted through the
//!   RTOS's set-post routine.
//!
//! `cfreplay` checks both against a recording of the Python hooks.
use crate::timers::Host;
use std::collections::HashSet;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Idle {
    /// The addresses of the idle spins.
    pub addrs: Vec<u32>,
    /// Passes through an idle spin so far, skipped ones included.
    pub spins: u64,
    /// Passes between reschedules.
    pub every: u64,
}

impl Idle {
    pub fn owns(&self, pc: u32) -> bool {
        self.addrs.contains(&pc)
    }

    /// The CPU reached an idle spin with `skip` passes of the current step
    /// left, which the caller skips.
    pub fn hit(&mut self, h: &mut dyn Host, skip: u64) {
        let before = self.spins / self.every.max(1);
        self.spins += 1 + skip;
        if self.spins / self.every.max(1) != before {
            h.raise(32, None);
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Unblock {
    /// The entries of the RTOS's semaphore-wait routines.
    pub pend: Vec<u32>,
    /// The entry of its set-post routine, 0 if not hooked.
    pub post: u32,
    /// Semaphores never to post from here.
    pub skip: HashSet<u32>,
    /// Callers whose waits are left alone.
    pub skip_callers: HashSet<u32>,
    pub satisfied: u64,
}

fn be32(h: &mut dyn Host, addr: u32) -> u32 {
    let mut b = [0u8; 4];
    h.read(addr, &mut b);
    u32::from_be_bytes(b)
}

impl Unblock {
    pub fn owns(&self, pc: u32) -> bool {
        self.pend.contains(&pc) || (self.post != 0 && self.post == pc)
    }

    /// The CPU is about to run the first instruction of a routine hooked
    /// here: the return address and the semaphore are on the stack.
    pub fn hit(&mut self, h: &mut dyn Host, pc: u32) {
        let sp = h.a7();
        if self.post != 0 && pc == self.post {
            // Real code posts this one, so it must never be faked.
            let sem = be32(h, sp.wrapping_add(4));
            self.skip.insert(sem);
            if !self.pend.contains(&pc) {
                return;
            }
        }
        let ret = be32(h, sp);
        let sem = be32(h, sp.wrapping_add(4));
        if sem == 0 || self.skip.contains(&sem) || self.skip_callers.contains(&ret) {
            return;
        }
        if !h.mapped(sem) {
            return;
        }
        if be32(h, sem) as i32 <= 0 {
            h.write(sem, &1u32.to_be_bytes());
            self.satisfied += 1;
        }
    }
}
