//! A captured stretch of guest execution with the reference engine's result:
//! entry state, the pages it touches, the expected final state. The files
//! are firmware-derived and live outside the tree.
use crate::cpu::{Cpu, Mem};
use std::collections::HashMap;

pub const PAGE: usize = 4096;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct State {
    /// D0..D7, A0..A7, SR.
    pub regs: [u32; 17],
    pub acc: [u64; 4],
    pub macsr: u32,
    pub mask: u32,
}

pub struct Workload {
    pub entry: u32,
    pub stop: u32,
    pub init: State,
    pub pages: Vec<(u32, Vec<u8>)>,
    pub expect: State,
    pub dirty: HashMap<u32, Vec<u8>>,
    /// Every block address the reference engine entered.
    pub blocks: Vec<u32>,
    pub block_entries: u32,
}

struct Reader<'a> {
    data: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn bytes(&mut self, n: usize) -> Result<&[u8], String> {
        let end = self.at + n;
        let out = self.data.get(self.at..end).ok_or("workload file is truncated")?;
        self.at = end;
        Ok(out)
    }

    fn u32(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.bytes(4)?.try_into().unwrap()))
    }

    fn u64(&mut self) -> Result<u64, String> {
        Ok(u64::from_le_bytes(self.bytes(8)?.try_into().unwrap()))
    }

    fn state(&mut self) -> Result<State, String> {
        let mut s = State::default();
        for r in s.regs.iter_mut() {
            *r = self.u32()?;
        }
        for a in s.acc.iter_mut() {
            *a = self.u64()?;
        }
        s.macsr = self.u32()?;
        s.mask = self.u32()?;
        Ok(s)
    }

    fn pages(&mut self) -> Result<Vec<(u32, Vec<u8>)>, String> {
        let n = self.u32()?;
        let mut out = Vec::with_capacity(n as usize);
        for _ in 0..n {
            let addr = self.u32()?;
            out.push((addr, self.bytes(PAGE)?.to_vec()));
        }
        Ok(out)
    }
}

impl Workload {
    pub fn parse(data: &[u8]) -> Result<Workload, String> {
        let mut r = Reader { data, at: 0 };
        if r.bytes(8)? != b"CFWL1\0\0\0" {
            return Err("not a workload file".into());
        }
        let entry = r.u32()?;
        let stop = r.u32()?;
        let init = r.state()?;
        let pages = r.pages()?;
        let expect = r.state()?;
        let dirty = r.pages()?.into_iter().collect();
        let n = r.u32()?;
        let mut blocks = Vec::with_capacity(n as usize);
        for _ in 0..n {
            blocks.push(r.u32()?);
        }
        let block_entries = r.u32()?;
        Ok(Workload { entry, stop, init, pages, expect, dirty, blocks, block_entries })
    }

    pub fn read(path: &str) -> Result<Workload, String> {
        let data = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Workload::parse(&data).map_err(|e| format!("{path}: {e}"))
    }

    /// Put the entry state into `c` and `m`.
    pub fn load(&self, c: &mut Cpu, m: &mut Mem) {
        for (addr, page) in &self.pages {
            m.map(*addr);
            m.write_bytes(*addr, page);
        }
        m.fault = 0;
        *c = Cpu::default();
        c.r.copy_from_slice(&self.init.regs[..16]);
        c.set_sr(self.init.regs[16]);
        c.macc = self.init.acc;
        c.macsr = self.init.macsr;
        c.mac_mask = self.init.mask;
        c.pc = self.entry;
    }

    /// Compare a finished run with the reference. -> what differs.
    pub fn check(&self, c: &Cpu, m: &mut Mem) -> Vec<String> {
        let mut bad = Vec::new();
        if c.halted != 0 {
            bad.push(format!("unimplemented instruction at {:#010x}", c.halted - 1));
        }
        if m.fault != 0 {
            bad.push(format!("access to unmapped {:#010x}", m.fault - 1));
        }
        if c.pc != self.stop {
            bad.push(format!("pc {:#010x}, expected {:#010x}", c.pc, self.stop));
        }
        for i in 0..16 {
            if c.r[i] != self.expect.regs[i] {
                let name = if i < 8 { 'd' } else { 'a' };
                bad.push(format!(
                    "{name}{} {:#010x}, expected {:#010x}",
                    i & 7,
                    c.r[i],
                    self.expect.regs[i]
                ));
            }
        }
        if c.full_sr() & 0xffff != self.expect.regs[16] & 0xffff {
            bad.push(format!("sr {:#06x}, expected {:#06x}", c.full_sr(), self.expect.regs[16]));
        }
        for i in 0..4 {
            if c.macc[i] != self.expect.acc[i] {
                bad.push(format!(
                    "acc{i} {:#018x}, expected {:#018x}",
                    c.macc[i], self.expect.acc[i]
                ));
            }
        }
        if c.macsr != self.expect.macsr {
            bad.push(format!("macsr {:#x}, expected {:#x}", c.macsr, self.expect.macsr));
        }
        if c.mac_mask != self.expect.mask {
            bad.push(format!("mask {:#x}, expected {:#x}", c.mac_mask, self.expect.mask));
        }
        let mut got = vec![0u8; PAGE];
        for (addr, page) in &self.pages {
            let want = self.dirty.get(addr).unwrap_or(page);
            m.read_bytes(*addr, &mut got);
            if &got != want {
                let at = got.iter().zip(want.iter()).position(|(a, b)| a != b).unwrap();
                bad.push(format!(
                    "memory {:#010x}: {:#04x}, expected {:#04x}",
                    *addr as usize + at,
                    got[at],
                    want[at]
                ));
            }
        }
        bad
    }
}
