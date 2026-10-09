//! The eSDHC controller and the eMMC behind it: the +Drive.
//!
//! A port of digiemu's `emu/esdhc.py`, which has the reasons. In short: a
//! write to XFERTYP issues the command, which completes at once; bulk data
//! moves through eDMA channel 59, which the driver arms (SERQ) before it
//! issues the command; and the interrupt handler's work is done here in its
//! place, which is to clear the driver's status word and post its
//! semaphores. The controller's registers are guest memory, as they are in
//! the Python model.
//!
//! The card is the sectors that hold anything, by number: one that is not
//! there reads as zeros. It is part of the machine and is saved with it
//! (`state.rs`), so the firmware's view of the volume and the volume cannot
//! come apart. A starting state brings its card in the trace file
//! (`cfstart --card`); a machine without one has no controller model at
//! all, and the firmware's card commands never complete.
use crate::edma::Tcd;
use crate::timers::Host;
use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom, Write};
use std::sync::{Arc, Mutex};

const BASE: u32 = 0xFC0C_C000;
const CMDARG: u32 = 0x08;
const XFERTYP: u32 = 0x0C;
const CMDRSP0: u32 = 0x10;
const DATPORT: u32 = 0x20;
const PRSSTAT: u32 = 0x24;
const SYSCTL: u32 = 0x2C;
const IRQSTAT: u32 = 0x30;

const DTDSEL: u32 = 1 << 4;
const DPSEL: u32 = 1 << 21;
const INHIBITS: u32 = 0b111;
const BWEN: u32 = 1 << 10;
const BREN: u32 = 1 << 11;
const CC_TC: u32 = 0b11;
const BWR: u32 = 1 << 4;
const BRR: u32 = 1 << 5;
/// SYSCTL's INITA and the three software resets, which clear themselves.
const SELF_CLEARING: u32 = 0xF << 24;

const SERQ: u32 = 0xFC04_4018;
const TCD_BASE: u32 = 0xFC04_5000;
const DMA_CHAN: u32 = 59;
const TCD: u32 = TCD_BASE + DMA_CHAN * 0x20;
const CSR_DONE: u32 = 0x80;
/// More than the firmware moves in one command; a descriptor asking for
/// more is not run.
const MAX_BYTES: u64 = 16 << 20;

pub const SECTOR: usize = 512;

/// The part the firmware's own table lists, which is what lets it mount
/// the volume (`emu/esdhc.py`, PART).
const MANUFACTURER: u32 = 0x11;
const NAME: &[u8; 6] = b"004GE0";

const BASE_MAGIC: &[u8; 8] = b"CFCD1\0\0\0";

fn fnv(hash: u64, data: &[u8]) -> u64 {
    data.iter().fold(hash, |h, b| (h ^ *b as u64).wrapping_mul(0x0000_0100_0000_01b3))
}

/// A card's contents in a file of their own, never written: the runs of
/// sectors that hold anything, then their bytes. A card too big to keep in
/// memory and in every saved machine (one with samples on it) is one of
/// these under the sectors written since.
///
/// The file: `CFCD1`, the card's size in sectors, the number of runs and a
/// hash of the data (u32, u32, u64, little-endian), each run's first sector
/// and length (u32, u32), then the data.
pub struct Base {
    file: Mutex<std::fs::File>,
    /// What tells this file from another: a hash of everything before the
    /// data, which includes the data's own hash.
    pub id: u64,
    pub blocks: u32,
    /// First sector, length in sectors, offset of its bytes in the file.
    runs: Vec<(u32, u32, u64)>,
}

impl std::fmt::Debug for Base {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "Base({:#x}, {} runs)", self.id, self.runs.len())
    }
}

impl Base {
    /// Write the card file for `image`, a raw image of the start of a card
    /// of `blocks` sectors. -> its id.
    pub fn pack(image: &[u8], blocks: u32, out: &str) -> Result<u64, String> {
        let mut runs: Vec<(u32, u32)> = Vec::new();
        let mut hash = 0xcbf2_9ce4_8422_2325u64;
        for (n, sector) in image.chunks(SECTOR).enumerate() {
            if sector.iter().all(|b| *b == 0) {
                continue;
            }
            hash = fnv(hash, sector);
            if sector.len() < SECTOR {
                hash = fnv(hash, &vec![0u8; SECTOR - sector.len()]);
            }
            match runs.last_mut() {
                Some((start, len)) if *start + *len == n as u32 => *len += 1,
                _ => runs.push((n as u32, 1)),
            }
        }
        let mut head = BASE_MAGIC.to_vec();
        head.extend_from_slice(&blocks.to_le_bytes());
        head.extend_from_slice(&(runs.len() as u32).to_le_bytes());
        head.extend_from_slice(&hash.to_le_bytes());
        for (start, len) in &runs {
            head.extend_from_slice(&start.to_le_bytes());
            head.extend_from_slice(&len.to_le_bytes());
        }
        let err = |e: std::io::Error| format!("{out}: {e}");
        let mut f = std::io::BufWriter::new(std::fs::File::create(out).map_err(err)?);
        f.write_all(&head).map_err(err)?;
        for (start, len) in &runs {
            let at = *start as usize * SECTOR;
            let end = (at + *len as usize * SECTOR).min(image.len());
            f.write_all(&image[at..end]).map_err(err)?;
            f.write_all(&vec![0u8; at + *len as usize * SECTOR - end]).map_err(err)?;
        }
        f.flush().map_err(err)?;
        Ok(fnv(0xcbf2_9ce4_8422_2325, &head))
    }

    pub fn open(path: &str) -> Result<Arc<Base>, String> {
        let err = |e: std::io::Error| format!("{path}: {e}");
        let mut file = std::fs::File::open(path).map_err(err)?;
        let size = file.metadata().map_err(err)?.len();
        let mut head = vec![0u8; 24];
        file.read_exact(&mut head).map_err(err)?;
        if &head[..8] != BASE_MAGIC {
            return Err(format!("{path}: not a card file"));
        }
        let word = |b: &[u8], at: usize| u32::from_le_bytes(b[at..at + 4].try_into().unwrap());
        let (blocks, count) = (word(&head, 8), word(&head, 12) as u64);
        if 24 + count * 8 > size {
            return Err(format!("{path}: the card file is cut short"));
        }
        let mut table = vec![0u8; count as usize * 8];
        file.read_exact(&mut table).map_err(err)?;
        let mut runs = Vec::with_capacity(count as usize);
        let mut at = 24 + count * 8;
        let mut next = 0u64;
        for row in table.chunks(8) {
            let (start, len) = (word(row, 0), word(row, 4));
            if (start as u64) < next || len == 0 {
                return Err(format!("{path}: the card file is damaged"));
            }
            runs.push((start, len, at));
            at += len as u64 * SECTOR as u64;
            next = start as u64 + len as u64;
        }
        if at != size {
            return Err(format!("{path}: the card file is cut short"));
        }
        head.extend_from_slice(&table);
        Ok(Arc::new(Base { file: Mutex::new(file), id: fnv(0xcbf2_9ce4_8422_2325, &head), blocks, runs }))
    }

    /// How many sectors the file holds.
    pub fn sectors(&self) -> u64 {
        self.runs.iter().map(|r| r.1 as u64).sum()
    }

    /// Whether the file has anything for `sector`.
    fn has(&self, sector: u32) -> bool {
        let i = self.runs.partition_point(|r| r.0 <= sector);
        i > 0 && sector - self.runs[i - 1].0 < self.runs[i - 1].1
    }

    /// Lay what the file has for the sectors from `sector` on over `out`.
    fn read(&self, sector: u32, out: &mut [u8]) {
        if out.is_empty() {
            return;
        }
        let end = sector as u64 + out.len().div_ceil(SECTOR) as u64;
        let first = self.runs.partition_point(|r| r.0 as u64 + r.1 as u64 <= sector as u64);
        let mut file = self.file.lock().unwrap_or_else(|e| e.into_inner());
        for &(start, len, at) in &self.runs[first..] {
            if start as u64 >= end {
                break;
            }
            let from = start.max(sector);
            let to = (start as u64 + len as u64).min(end);
            let o = (from - sector) as usize * SECTOR;
            let n = (((to - from as u64) as usize) * SECTOR).min(out.len() - o);
            let ok = file
                .seek(SeekFrom::Start(at + (from - start) as u64 * SECTOR as u64))
                .and_then(|_| file.read_exact(&mut out[o..o + n]));
            if ok.is_err() {
                out[o..o + n].fill(0);
            }
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Card {
    /// The card's size in sectors.
    pub blocks: u32,
    pub rca: u16,
    pub selected: bool,
    /// CMD35's and CMD36's sectors, waiting for CMD38.
    pub erase_from: Option<u32>,
    pub erase_to: Option<u32>,
    /// Without a card file: the sectors that are not all zeros. With one:
    /// the sectors written since, zeros included.
    pub sectors: BTreeMap<u32, Box<[u8; SECTOR]>>,
    /// The id of the card file that belongs under them, 0 for none, and
    /// the file once it has been given (`Machine::attach_card`).
    pub base_id: u64,
    pub base: Option<Arc<Base>>,
    /// Sectors of the card file that have been erased: first and last,
    /// in order, none touching.
    pub erased: Vec<(u32, u32)>,
}

impl PartialEq for Card {
    fn eq(&self, other: &Card) -> bool {
        (self.blocks, self.rca, self.selected, self.erase_from, self.erase_to, self.base_id)
            == (other.blocks, other.rca, other.selected, other.erase_from, other.erase_to, other.base_id)
            && self.sectors == other.sectors
            && self.erased == other.erased
    }
}

impl Card {
    /// A card of `blocks` sectors holding `image`, a raw image of its start.
    pub fn from_image(blocks: u32, image: &[u8]) -> Card {
        let mut card = Card { blocks, ..Default::default() };
        card.write(0, image);
        card
    }

    /// -> the response words RSP0..RSP3.
    fn command(&mut self, index: u32, arg: u32) -> [u32; 4] {
        // Transfer state, ready for data.
        let r1 = [0x0000_0900, 0, 0, 0];
        match index {
            0 => {
                self.selected = false;
                [0; 4]
            }
            // Powered up, sector addressed.
            1 => [0xC0FF_8080, 0, 0, 0],
            2 | 10 => {
                let n = NAME;
                let be = |a: u8, b: u8, c: u8, d: u8| u32::from_be_bytes([a, b, c, d]);
                [0, be(n[4], n[5], 0, 0), be(n[0], n[1], n[2], n[3]), MANUFACTURER << 16]
            }
            9 => [0; 4],
            3 => {
                self.rca = (arg >> 16) as u16;
                r1
            }
            7 => {
                self.selected = (arg >> 16) as u16 == self.rca;
                r1
            }
            35 => {
                self.erase_from = Some(arg);
                r1
            }
            36 => {
                self.erase_to = Some(arg);
                r1
            }
            38 => {
                // The range includes its last sector.
                if let (Some(from), Some(to)) = (self.erase_from.take(), self.erase_to.take()) {
                    if from <= to {
                        self.erase(from, to);
                    }
                }
                self.erase_from = None;
                self.erase_to = None;
                r1
            }
            _ => r1,
        }
    }

    /// The 512 bytes of EXT_CSD: only what the firmware reads.
    fn ext_csd(&self) -> Vec<u8> {
        let mut b = vec![0u8; 512];
        b[0x98] = 1; // in SLC mode
        b[0xD4..0xD8].copy_from_slice(&self.blocks.to_be_bytes());
        b[0xAF] = 1;
        b[0xB7] = 1;
        b[0xB9] = 1;
        b[0x9C..0x9F].copy_from_slice(&[0x00, 0x01, 0xD8]);
        b[0xDE] = 0x01;
        b[0xE3] = 0x08;
        b
    }

    /// Erase the sectors `from` to `to`, both included.
    fn erase(&mut self, from: u32, to: u32) {
        let gone: Vec<u32> = self.sectors.range(from..=to).map(|(n, _)| *n).collect();
        for n in gone {
            self.sectors.remove(&n);
        }
        if self.base_id == 0 {
            return;
        }
        let (mut from, mut to) = (from, to);
        let mut kept = Vec::with_capacity(self.erased.len() + 1);
        for &(a, b) in &self.erased {
            if b as u64 + 1 < from as u64 || to as u64 + 1 < a as u64 {
                kept.push((a, b));
            } else {
                from = from.min(a);
                to = to.max(b);
            }
        }
        kept.push((from, to));
        kept.sort();
        self.erased = kept;
    }

    /// `len` bytes from `sector` on.
    pub fn read(&self, sector: u32, len: usize) -> Vec<u8> {
        let mut out = vec![0u8; len];
        if len == 0 {
            return out;
        }
        let last = sector.saturating_add(((len - 1) / SECTOR) as u32);
        if let Some(base) = &self.base {
            base.read(sector, &mut out);
            for &(a, b) in &self.erased {
                let (a, b) = (a.max(sector), b.min(last));
                if a <= b {
                    let at = (a - sector) as usize * SECTOR;
                    let end = ((b - sector) as usize * SECTOR + SECTOR).min(len);
                    out[at..end].fill(0);
                }
            }
        }
        for (n, data) in self.sectors.range(sector..=last) {
            let at = (*n - sector) as usize * SECTOR;
            let c = SECTOR.min(len - at);
            out[at..at + c].copy_from_slice(&data[..c]);
        }
        out
    }

    /// Put `data` on the card from `sector` on.
    pub fn write(&mut self, sector: u32, data: &[u8]) {
        for (k, part) in data.chunks(SECTOR).enumerate() {
            let Some(n) = sector.checked_add(k as u32) else { return };
            // Zeros need keeping only over something the card file has.
            let covered = self.base.as_ref().is_some_and(|b| b.has(n)) && !self.erased.iter().any(|&(a, b)| a <= n && n <= b);
            if part.len() == SECTOR && part.iter().all(|b| *b == 0) && !covered {
                self.sectors.remove(&n);
                continue;
            }
            if part.len() < SECTOR && !self.sectors.contains_key(&n) {
                let was = self.read(n, SECTOR);
                self.sectors.insert(n, Box::new(was.try_into().unwrap()));
            }
            let slot = self.sectors.entry(n).or_insert_with(|| Box::new([0; SECTOR]));
            slot[..part.len()].copy_from_slice(part);
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Esdhc {
    /// The driver's status word, which its interrupt handler clears.
    pub status: u32,
    /// The semaphores that handler and channel 59's post: a command done,
    /// a data transfer done, the DMA done. 0 where there is none.
    pub cmd_sem: u32,
    pub data_sem: u32,
    pub dma_sem: u32,
    /// The last word the firmware wrote to DATPORT, for the bus test.
    pub pattern: u32,
    /// Channel 59 has been armed for the next command.
    pub armed: bool,
    pub commands: u64,
    pub bytes_moved: u64,
    pub card: Card,
}

fn be32(h: &mut dyn Host, addr: u32) -> u32 {
    let mut b = [0u8; 4];
    h.read(addr, &mut b);
    u32::from_be_bytes(b)
}

fn put32(h: &mut dyn Host, addr: u32, value: u32) {
    h.write(addr, &value.to_be_bytes());
}

/// Is every megabyte of `len` bytes at `addr` there?
fn there(h: &mut dyn Host, addr: u32, len: u64) -> bool {
    let Some(last) = (addr as u64).checked_add(len.max(1) - 1).filter(|l| *l <= u32::MAX as u64) else {
        return false;
    };
    // A megabyte of SDRAM nothing has touched yet becomes RAM when written.
    (addr >> 20..=(last as u32) >> 20).all(|mb| h.mapped(mb << 20) || crate::cpu::SDRAM_WINDOW.contains(&(mb << 20)))
}

impl Esdhc {
    /// The model for a driver whose "storage is up" flag is at `flag`: the
    /// words it uses are at fixed places after it (`emu/symbols.py`,
    /// sd_flag).
    pub fn for_driver(flag: u32, card: Card) -> Esdhc {
        Esdhc {
            status: flag + 0x30,
            cmd_sem: flag + 0x4C,
            data_sem: flag + 0x44,
            dma_sem: flag + 0x3C,
            card,
            ..Default::default()
        }
    }

    pub fn owns(&self, write: bool, addr: u32) -> bool {
        let reg = |r: u32| (BASE + r..BASE + r + 4).contains(&addr);
        if write {
            reg(XFERTYP) || reg(DATPORT) || addr == SERQ
        } else {
            reg(SYSCTL)
        }
    }

    /// The firmware is about to read or write one of the registers above.
    pub fn access(&mut self, h: &mut dyn Host, write: bool, addr: u32, size: u32, value: u32) {
        if !write {
            // The bits the firmware set and now polls have cleared.
            let cur = be32(h, BASE + SYSCTL);
            if cur & SELF_CLEARING != 0 {
                put32(h, BASE + SYSCTL, cur & !SELF_CLEARING);
            }
        } else if addr == SERQ {
            // Bit 6 means every channel.
            if value & 0x40 == 0 && value & 0x3F == DMA_CHAN {
                self.armed = true;
            }
        } else if (BASE + DATPORT..BASE + DATPORT + 4).contains(&addr) {
            self.pattern = value;
        } else {
            // The store has not landed: the command is in `value`.
            let xfer = if size == 4 { value } else { be32(h, BASE + XFERTYP) };
            self.issue(h, xfer);
        }
    }

    fn post(&self, h: &mut dyn Host, sem: u32) {
        if sem != 0 && h.mapped(sem) && be32(h, sem) as i32 <= 0 {
            put32(h, sem, 1);
        }
    }

    fn set(&self, h: &mut dyn Host, reg: u32, bits: u32) {
        let cur = be32(h, BASE + reg);
        put32(h, BASE + reg, cur | bits);
    }

    fn issue(&mut self, h: &mut dyn Host, xfer: u32) {
        let index = (xfer >> 24) & 0x3F;
        let arg = be32(h, BASE + CMDARG);
        self.commands += 1;
        let response = self.card.command(index, arg);
        for (k, word) in response.iter().enumerate() {
            put32(h, BASE + CMDRSP0 + 4 * k as u32, *word);
        }
        let prsstat = be32(h, BASE + PRSSTAT);
        put32(h, BASE + PRSSTAT, prsstat & !INHIBITS);
        self.set(h, IRQSTAT, CC_TC);
        if xfer & DPSEL != 0 {
            if xfer & DTDSEL != 0 {
                // Card to host.
                self.set(h, PRSSTAT, BREN);
                self.set(h, IRQSTAT, BRR);
                // CMD14 answers the bus test with the pattern inverted.
                put32(h, BASE + DATPORT, if index == 14 { !self.pattern } else { 0 });
                let want = if self.armed { self.dma_size(h) } else { 0 };
                let payload = match index {
                    8 => Some(self.card.ext_csd()),
                    18 => Some(self.card.read(arg, want as usize)),
                    _ => None,
                };
                if let (Some(payload), true) = (payload, self.armed) {
                    self.dma_out(h, &payload);
                    self.armed = false;
                    self.post(h, self.dma_sem);
                }
            } else {
                self.set(h, PRSSTAT, BWEN);
                self.set(h, IRQSTAT, BWR);
                if self.armed {
                    let payload = self.dma_in(h);
                    if index == 25 {
                        self.card.write(arg, &payload);
                    }
                    self.armed = false;
                    self.post(h, self.dma_sem);
                }
            }
            self.post(h, self.data_sem);
        }
        if self.status != 0 && h.mapped(self.status) {
            put32(h, self.status, 0);
        }
        self.post(h, self.cmd_sem);
    }

    fn tcd(&self, h: &mut dyn Host) -> Tcd {
        let mut raw = [0u8; 0x20];
        h.read(TCD, &mut raw);
        Tcd::parse(&raw)
    }

    /// The bytes the armed channel's major loop moves, 0 if that is not a
    /// sensible number.
    fn dma_size(&self, h: &mut dyn Host) -> u64 {
        let t = self.tcd(h);
        let total = (t.citer & 0x7FFF) as u64 * t.nbytes as u64;
        if total > MAX_BYTES {
            0
        } else {
            total
        }
    }

    /// Give `payload` to the firmware through the channel's descriptor: its
    /// source is DATPORT read over and over and its destination runs on, so
    /// this is one copy and the descriptor's bookkeeping.
    fn dma_out(&mut self, h: &mut dyn Host, payload: &[u8]) {
        let mut t = self.tcd(h);
        let total = self.dma_size(h);
        if total == 0 || !there(h, t.daddr, total) {
            return;
        }
        let mut chunk = payload[..payload.len().min(total as usize)].to_vec();
        chunk.resize(total as usize, 0);
        h.write(t.daddr, &chunk);
        t.daddr = t.daddr.wrapping_add(total as u32);
        t.citer = t.biter & 0x7FFF;
        t.csr |= CSR_DONE;
        h.write(TCD, &t.pack());
        self.bytes_moved += total;
    }

    /// Take what the channel's descriptor names out of the firmware's
    /// buffer, as `emu/esdhc.py` does: one minor loop's bytes at a time,
    /// the source moving on by SOFF between them.
    fn dma_in(&mut self, h: &mut dyn Host) -> Vec<u8> {
        let mut t = self.tcd(h);
        let (citer, nbytes) = (t.citer & 0x7FFF, t.nbytes);
        let mut payload = Vec::new();
        if citer as u64 * nbytes as u64 > MAX_BYTES {
            return payload;
        }
        let mut src = t.saddr;
        for _ in 0..citer {
            let mut part = vec![0u8; nbytes as usize];
            if there(h, src, nbytes as u64) {
                h.read(src, &mut part);
            }
            payload.extend_from_slice(&part);
            src = src.wrapping_add(t.soff as u32);
        }
        t.saddr = src.wrapping_add(t.slast);
        t.citer = t.biter & 0x7FFF;
        t.csr |= CSR_DONE;
        h.write(TCD, &t.pack());
        self.bytes_moved += payload.len() as u64;
        payload
    }
}
