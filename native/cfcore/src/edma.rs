//! Software-started eDMA channels: the memory-to-memory moves the audio
//! render hands to the DMA controller and then waits on.
//!
//! A port of digiemu's `emu/edma_sw.py` (`SoftwareBank` and its channels,
//! the Python path, which `emu/native.py`'s C version also follows).
//! `cfreplay` checks every byte it moves against a recording of the Python
//! model. It is not a general eDMA: a channel whose START bit the firmware
//! sets has its major loop (and any scatter-gather chain) run at once as
//! copies, and DONE is shown the next time the firmware reads the CSR.
use crate::timers::Host;

const TCD_BASE: u32 = 0xFC04_5000;
const SSRT: u32 = 0xFC04_401E;
const CSR: u32 = 0x1E;
const CSR_START: u32 = 0x0001;
const CSR_DONE: u32 = 0x0080;
const CSR_ACTIVE: u32 = 0x0040;
const CSR_ESG: u32 = 0x0010;
const MAX_LINKS: usize = 64;
const MAX_BYTES: u64 = 1 << 20;
const MAP_PAGE: i64 = 0x10_0000;

/// A transfer control descriptor, in its register order.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Tcd {
    pub(crate) saddr: u32,
    pub(crate) attr: u32,
    pub(crate) soff: i32,
    pub(crate) nbytes: u32,
    pub(crate) slast: u32,
    pub(crate) daddr: u32,
    pub(crate) citer: u32,
    pub(crate) doff: i32,
    pub(crate) dlast: u32,
    pub(crate) biter: u32,
    pub(crate) csr: u32,
}

impl Tcd {
    pub(crate) fn parse(raw: &[u8; 0x20]) -> Tcd {
        let be16 = |o: usize| u16::from_be_bytes([raw[o], raw[o + 1]]) as u32;
        let be32 = |o: usize| u32::from_be_bytes([raw[o], raw[o + 1], raw[o + 2], raw[o + 3]]);
        Tcd {
            saddr: be32(0x00),
            attr: be16(0x04),
            soff: be16(0x06) as u16 as i16 as i32,
            nbytes: be32(0x08),
            slast: be32(0x0C),
            daddr: be32(0x10),
            citer: be16(0x14),
            doff: be16(0x16) as u16 as i16 as i32,
            dlast: be32(0x18),
            biter: be16(0x1C),
            csr: be16(0x1E),
        }
    }

    pub(crate) fn pack(&self) -> [u8; 0x20] {
        let mut raw = [0u8; 0x20];
        raw[0x00..0x04].copy_from_slice(&self.saddr.to_be_bytes());
        raw[0x04..0x06].copy_from_slice(&(self.attr as u16).to_be_bytes());
        raw[0x06..0x08].copy_from_slice(&(self.soff as i16).to_be_bytes());
        raw[0x08..0x0C].copy_from_slice(&self.nbytes.to_be_bytes());
        raw[0x0C..0x10].copy_from_slice(&self.slast.to_be_bytes());
        raw[0x10..0x14].copy_from_slice(&self.daddr.to_be_bytes());
        raw[0x14..0x16].copy_from_slice(&(self.citer as u16).to_be_bytes());
        raw[0x16..0x18].copy_from_slice(&(self.doff as i16).to_be_bytes());
        raw[0x18..0x1C].copy_from_slice(&self.dlast.to_be_bytes());
        raw[0x1C..0x1E].copy_from_slice(&(self.biter as u16).to_be_bytes());
        raw[0x1E..0x20].copy_from_slice(&(self.csr as u16).to_be_bytes());
        raw
    }
}

/// `addr + delta` with the low `modulo` bits wrapping, as ATTR's SMOD and
/// DMOD make a ring buffer.
fn modulo_add(addr: u32, delta: i64, modulo: u32) -> u32 {
    if modulo == 0 {
        return (addr as i64 + delta) as u32;
    }
    let mask = ((1u64 << modulo) - 1) as u32;
    (addr & !mask) | (((addr as i64 + delta) as u32) & mask)
}

/// The minor-loop count in a CITER/BITER word: with ELINK (bit 15) set it
/// is only bits 8:0.
fn iteration_count(raw: u32) -> u32 {
    if raw & 0x8000 != 0 {
        raw & 0x01FF
    } else {
        raw & 0x7FFF
    }
}

/// The megabytes a channel's major loop will touch.
fn span(base: u32, off: i32, citer: u32, nbytes: u32, modulo: u32) -> impl Iterator<Item = u32> {
    let (base, off, citer, nbytes) = (base as i64, off as i64, citer as i64, nbytes as i64);
    let step = off.abs();
    let mut span = step * citer * (nbytes / step.max(1)).max(1);
    span = span.max(citer * nbytes).max(1);
    let mut lo = base.min(base + if off < 0 { off * citer * nbytes } else { 0 });
    if modulo != 0 {
        let ring = 1i64 << modulo;
        lo = base & !(ring - 1) & 0xFFFF_FFFF;
        span = span.min(ring);
        if (base & (ring - 1)) + span > ring {
            span = ring;
        } else {
            lo = base;
        }
    }
    let first = lo & !(MAP_PAGE - 1);
    let last = (lo + span - 1) & !(MAP_PAGE - 1);
    (0..=(last - first) / MAP_PAGE).map(move |i| (first + i * MAP_PAGE) as u32)
}

fn read_ring(h: &mut dyn Host, mut addr: u32, n: u32, modulo: u32) -> Vec<u8> {
    let mut out = vec![0u8; n as usize];
    if modulo == 0 {
        h.read(addr, &mut out);
        return out;
    }
    let ring = 1u64 << modulo;
    let mut pos = 0usize;
    while pos < out.len() {
        let c = ((out.len() - pos) as u64).min(ring - (addr as u64 & (ring - 1))) as usize;
        h.read(addr, &mut out[pos..pos + c]);
        addr = modulo_add(addr, c as i64, modulo);
        pos += c;
    }
    out
}

fn write_ring(h: &mut dyn Host, mut addr: u32, data: &[u8], modulo: u32) {
    if modulo == 0 {
        h.write(addr, data);
        return;
    }
    let ring = 1u64 << modulo;
    let mut pos = 0usize;
    while pos < data.len() {
        let c = ((data.len() - pos) as u64).min(ring - (addr as u64 & (ring - 1))) as usize;
        h.write(addr, &data[pos..pos + c]);
        addr = modulo_add(addr, c as i64, modulo);
        pos += c;
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Bank {
    /// Channels that belong to another model (the SSI's), one bit each.
    pub claimed: u64,
    /// A finished transfer's CSR, waiting for the firmware to look.
    pub done: [Option<u16>; 64],
    /// A transfer that could not run inside the firmware's write.
    pub deferred: [bool; 64],
    /// The channels with either, in the order they came up.
    pub pending: Vec<u8>,
    pub transfers: u64,
    pub bytes_moved: u64,
    pub refused: u64,
}

impl Default for Bank {
    fn default() -> Self {
        Bank {
            claimed: 0,
            done: [None; 64],
            deferred: [false; 64],
            pending: Vec::new(),
            transfers: 0,
            bytes_moved: 0,
            refused: 0,
        }
    }
}

impl Bank {
    fn tcd(ch: usize) -> u32 {
        TCD_BASE + ch as u32 * 0x20
    }

    fn has(&self, ch: usize) -> bool {
        ch < 64 && self.claimed & (1 << ch) == 0
    }

    pub fn owns(&self, write: bool, addr: u32) -> bool {
        (TCD_BASE..TCD_BASE + 64 * 0x20).contains(&addr) || (write && addr == SSRT)
    }

    /// The channel whose CSR an access at `addr` touches, if it is ours.
    fn channel_for_csr(&self, addr: u32, size: u32) -> Option<usize> {
        let offset = (addr - TCD_BASE) % 0x20;
        if offset + size <= CSR || offset > CSR + 1 {
            return None;
        }
        let ch = ((addr - TCD_BASE) / 0x20) as usize;
        self.has(ch).then_some(ch)
    }

    /// The firmware is about to read or write an eDMA register.
    pub fn access(&mut self, h: &mut dyn Host, write: bool, addr: u32, size: u32, value: u32) {
        if addr == SSRT {
            return self.ssrt(h, value);
        }
        let Some(ch) = self.channel_for_csr(addr, size) else { return };
        if !write {
            // The firmware is polling: let it see DONE now.
            self.apply(h, ch);
            return;
        }
        // The hook runs before the store lands: merge it in by hand.
        let mut current = [0u8; 2];
        h.read(Self::tcd(ch) + CSR, &mut current);
        let off = (addr - (Self::tcd(ch) + CSR)) as usize;
        let bytes = value.to_be_bytes();
        for i in 0..size as usize {
            if off + i < 2 {
                current[off + i] = bytes[4 - size as usize + i];
            }
        }
        let csr = u16::from_be_bytes(current) as u32;
        if csr & CSR_START != 0 {
            h.write(Self::tcd(ch) + CSR, &current);
            if self.mapped(h, ch) {
                self.transfer(h, ch);
            } else {
                self.deferred[ch] = true;
            }
        }
        if (self.done[ch].is_some() || self.deferred[ch]) && !self.pending.contains(&(ch as u8)) {
            self.pending.push(ch as u8);
        }
    }

    /// SSRT: start the channel written, as a CSR START would. The store is
    /// not to the CSR, so the completion can be written straight away.
    fn ssrt(&mut self, h: &mut dyn Host, value: u32) {
        let v = value & 0xFF;
        let ch = (v & 0x3F) as usize;
        if v & 0x40 != 0 || !self.has(ch) {
            return;
        }
        let mut current = [0u8; 2];
        h.read(Self::tcd(ch) + CSR, &mut current);
        let csr = u16::from_be_bytes(current) | CSR_START as u16;
        h.write(Self::tcd(ch) + CSR, &csr.to_be_bytes());
        if !self.mapped(h, ch) {
            self.deferred[ch] = true;
            if !self.pending.contains(&(ch as u8)) {
                self.pending.push(ch as u8);
            }
            return;
        }
        self.transfer(h, ch);
        self.apply(h, ch);
    }

    /// At a step boundary: run what was deferred, show what is done.
    pub fn service(&mut self, h: &mut dyn Host, _done: i64) {
        for ch in std::mem::take(&mut self.pending) {
            let ch = ch as usize;
            if std::mem::take(&mut self.deferred[ch]) {
                self.transfer(h, ch);
            }
            self.apply(h, ch);
        }
    }

    /// Write a queued completion, now that the firmware's store has landed.
    fn apply(&mut self, h: &mut dyn Host, ch: usize) {
        if let Some(csr) = self.done[ch].take() {
            h.write(Self::tcd(ch) + CSR, &csr.to_be_bytes());
        }
    }

    /// Does the programmed transfer, chain included, touch only memory that
    /// is there? One that does not is left for the step boundary.
    fn mapped(&self, h: &mut dyn Host, ch: usize) -> bool {
        let mut raw = [0u8; 0x20];
        h.read(Self::tcd(ch), &mut raw);
        let mut seen = Vec::new();
        for _ in 0..MAX_LINKS {
            let t = Tcd::parse(&raw);
            let citer = iteration_count(t.citer);
            let mut pages = span(t.saddr, t.soff, citer, t.nbytes, t.attr >> 11)
                .chain(span(t.daddr, t.doff, citer, t.nbytes, (t.attr >> 3) & 0x1F));
            if !pages.all(|p| h.mapped(p)) {
                return false;
            }
            if t.csr & CSR_ESG == 0 {
                return true;
            }
            if seen.contains(&t.dlast) || t.dlast % 0x20 != 0 || !h.mapped(t.dlast & !(MAP_PAGE as u32 - 1)) {
                return false;
            }
            seen.push(t.dlast);
            h.read(t.dlast, &mut raw);
        }
        false
    }

    /// Run the major loop and any chain, then queue the completion.
    fn transfer(&mut self, h: &mut dyn Host, ch: usize) {
        let mut raw = [0u8; 0x20];
        h.read(Self::tcd(ch), &mut raw);
        let mut tcd = Tcd::parse(&raw);
        let mut moved = 0u64;
        let mut seen = Vec::new();
        let mut links = 0;
        loop {
            match self.run(h, ch, &mut tcd) {
                Some(n) => moved += n,
                None => {
                    self.refused += 1;
                    return;
                }
            }
            if tcd.csr & CSR_ESG == 0 {
                break;
            }
            links += 1;
            let next = tcd.dlast;
            if links >= MAX_LINKS || seen.contains(&next) || next % 0x20 != 0 {
                self.refused += 1;
                return;
            }
            seen.push(next);
            h.read(next, &mut raw);
            h.write(Self::tcd(ch), &raw);
            tcd = Tcd::parse(&raw);
        }
        self.transfers += 1;
        self.bytes_moved += moved;
        self.done[ch] = Some(((tcd.csr & !(CSR_START | CSR_ACTIVE)) | CSR_DONE) as u16);
    }

    /// One major loop of `tcd`, which is updated and written back whole.
    /// -> bytes moved, None if the descriptor makes no sense.
    fn run(&mut self, h: &mut dyn Host, ch: usize, tcd: &mut Tcd) -> Option<u64> {
        let citer = iteration_count(tcd.citer);
        if citer == 0 || tcd.nbytes == 0 || citer as u64 * tcd.nbytes as u64 > MAX_BYTES {
            return None;
        }
        let ssize = 1u32 << ((tcd.attr >> 8) & 7);
        let dsize = 1u32 << (tcd.attr & 7);
        let (smod, dmod) = (tcd.attr >> 11, (tcd.attr >> 3) & 0x1F);
        if tcd.nbytes % ssize != 0 || tcd.nbytes % dsize != 0 {
            return None;
        }
        let total = citer * tcd.nbytes;
        let (source, dest);
        if tcd.soff == ssize as i32 && tcd.doff == dsize as i32 {
            let data = read_ring(h, tcd.saddr, total, smod);
            write_ring(h, tcd.daddr, &data, dmod);
            source = modulo_add(tcd.saddr, total as i64, smod);
            dest = modulo_add(tcd.daddr, total as i64, dmod);
        } else {
            let (mut s, mut d) = (tcd.saddr, tcd.daddr);
            let mut buf = vec![0u8; tcd.nbytes as usize];
            for _ in 0..citer {
                for k in 0..(tcd.nbytes / ssize) as usize {
                    h.read(s, &mut buf[k * ssize as usize..(k + 1) * ssize as usize]);
                    s = modulo_add(s, tcd.soff as i64, smod);
                }
                for k in 0..(tcd.nbytes / dsize) as usize {
                    h.write(d, &buf[k * dsize as usize..(k + 1) * dsize as usize]);
                    d = modulo_add(d, tcd.doff as i64, dmod);
                }
            }
            source = s;
            dest = d;
        }
        tcd.saddr = source.wrapping_add(tcd.slast);
        tcd.daddr = if tcd.csr & CSR_ESG == 0 { dest.wrapping_add(tcd.dlast) } else { dest };
        tcd.citer = tcd.biter;
        h.write(Self::tcd(ch), &tcd.pack());
        Some(total as u64)
    }
}
