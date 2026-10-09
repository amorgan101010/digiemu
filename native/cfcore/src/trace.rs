//! A recorded run of the live emulator: the machine at the start, then
//! everything the reference engine did and everything done to it from
//! outside, in order. The engine's side is the blocks it entered and the
//! state at the end of each step; the outside is every hook that fired,
//! every write the host made to guest memory and registers, and every read
//! it made of guest memory. Replaying the outside against this core must
//! give the same states and the same memory. Firmware-derived: the files
//! live outside the tree. `tools/record.py` writes them.
use crate::workload::State;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Item {
    /// The engine is started at this PC.
    StepStart(u32),
    /// The reference engine entered a block: address, size in bytes.
    Tb(u32, u32),
    /// A code hook fired before the instruction at this address.
    Code(u32),
    /// A memory hook fired before this access.
    Mem { write: bool, size: u8, addr: u32, value: u32 },
    /// The exception hook fired with this number.
    Intr(u32),
    /// The host wrote guest memory: address, offset and length in `blob`.
    WMem { addr: u32, off: u32, len: u32 },
    /// The host wrote a register: 0..15 D0..A7, 16 SR, 17 PC.
    WReg(u8, u32),
    /// The host asked the engine to stop.
    Stop,
    /// The engine returned: index into `states`, and the PC.
    StepEnd(u32, u32),
    /// The host read guest memory and got these bytes (in `blob`).
    HRead { addr: u32, off: u32, len: u32 },
    /// A timer model's `step` was called: source, index of `done` in `nums`.
    SrcStep(u8, u32),
    /// ... and returned: source, index of the result in `nums`.
    SrcStepEnd(u8, u32),
    /// A timer model's `service` was called: source, index of `done`.
    SrcService(u8, u32),
    /// ... and returned.
    SrcServiceEnd(u8),
    /// `render_holds` was asked about this level (0xff for none) and gave
    /// this answer.
    RenderHolds(bool, u8),
    /// The audio model handed on these samples (in `blob`).
    Audio { off: u32, len: u32 },
    /// The idle hook skipped the rest of the step: the passes credited
    /// (index in `nums`).
    IdleSkip(u32),
    /// The window gave the panel input: 0 key (column, bit, down), 1 pad
    /// (index, velocity), 2 turn (encoder, steps); three numbers in `nums`.
    Input(u8, u32),
}

/// A timer model's state when the recording began (`timers::Clock` and the
/// DMA timers' extras). `kind` 0 is the PITs, 1 the DMA timers, 2 and up
/// 3 the forced-interrupt models, which have `forced` instead, 4 the audio
/// model, which has `ssi`.
#[derive(Clone, Debug, Default)]
pub struct Source {
    pub kind: u8,
    pub clock: crate::timers::Clock,
    pub arm: u8,
    pub now: i64,
    pub forced: crate::intfrc::Forced,
    pub ssi: crate::ssi::Ssi,
    /// Kind 5, the software eDMA channels.
    pub bank: crate::edma::Bank,
    /// Kind 6, the front panel.
    pub panel: crate::board::Panel,
    /// Kind 7, the idle-loop hook, and kind 8, the semaphore stand-in.
    pub idle: crate::rtos::Idle,
    pub unblock: crate::rtos::Unblock,
    /// Kind 9, the I2C bus and the codec on it.
    pub i2c: crate::board::I2c,
}

pub struct Region {
    pub addr: u32,
    pub len: u32,
    pub device: bool,
    /// Another view of the RAM at this address; then there is no data.
    pub alias: Option<u32>,
    pub data: Vec<u8>,
}

pub struct Trace {
    pub init: State,
    pub regions: Vec<Region>,
    /// Addresses with a code hook.
    pub code_hooks: Vec<u32>,
    pub sources: Vec<Source>,
    /// The 64-bit numbers some items point at.
    pub nums: Vec<i64>,
    pub items: Vec<Item>,
    pub blob: Vec<u8>,
    pub states: Vec<State>,
    pub emulated_ns: u64,
    /// Exception entries the host made during the recording.
    pub raised: u64,
    /// Every region's memory when the recording ended.
    pub last: Vec<(u32, Vec<u8>)>,
    /// How many bytes of the file the starting state takes: everything
    /// before the items.
    pub head: usize,
    /// The +Drive the machine starts with, if the file brings one.
    pub card: Option<crate::esdhc::Esdhc>,
}

struct Reader<'a> {
    data: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn bytes(&mut self, n: usize) -> Result<&[u8], String> {
        let end = self.at + n;
        let out = self.data.get(self.at..end).ok_or("trace file is truncated")?;
        self.at = end;
        Ok(out)
    }

    fn u8(&mut self) -> Result<u8, String> {
        Ok(self.bytes(1)?[0])
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
}

impl Trace {
    pub fn parse(data: &[u8]) -> Result<Trace, String> {
        let mut r = Reader { data, at: 0 };
        if r.bytes(8)? != b"CFTR2\0\0\0" {
            return Err("not a trace file".into());
        }
        let init = r.state()?;
        let mut regions = Vec::new();
        for _ in 0..r.u32()? {
            let addr = r.u32()?;
            let len = r.u32()?;
            let flags = r.u32()?;
            if flags & 2 != 0 {
                let alias = Some(r.u32()?);
                regions.push(Region { addr, len, device: false, alias, data: Vec::new() });
            } else {
                let data = r.bytes(len as usize)?.to_vec();
                regions.push(Region { addr, len, device: flags & 1 != 0, alias: None, data });
            }
        }
        let mut code_hooks = Vec::new();
        for _ in 0..r.u32()? {
            code_hooks.push(r.u32()?);
        }
        let mut sources = Vec::new();
        for _ in 0..r.u32()? {
            let mut s = Source { kind: r.u8()?, ..Default::default() };
            if s.kind == 9 {
                let b = r.bytes(9)?.to_vec();
                let i = &mut s.i2c;
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
                sources.push(s);
                continue;
            }
            if s.kind == 7 {
                s.idle.spins = r.u64()?;
                s.idle.every = r.u64()?;
                for _ in 0..r.u32()? {
                    s.idle.addrs.push(r.u32()?);
                }
                sources.push(s);
                continue;
            }
            if s.kind == 8 {
                for _ in 0..r.u32()? {
                    s.unblock.pend.push(r.u32()?);
                }
                s.unblock.post = r.u32()?;
                for _ in 0..r.u32()? {
                    s.unblock.skip.insert(r.u32()?);
                }
                for _ in 0..r.u32()? {
                    s.unblock.skip_callers.insert(r.u32()?);
                }
                sources.push(s);
                continue;
            }
            if s.kind == 6 {
                let p = &mut s.panel;
                p.select = r.u8()?;
                p.frames = r.u64()? as i64;
                p.keys.copy_from_slice(r.bytes(4)?);
                p.phase.copy_from_slice(r.bytes(16)?);
                for pad in p.pads.iter_mut() {
                    let b = r.bytes(2)?;
                    *pad = u16::from_le_bytes([b[0], b[1]]);
                }
                p.led_rows.copy_from_slice(r.bytes(8)?);
                sources.push(s);
                continue;
            }
            if s.kind == 5 {
                s.bank.claimed = r.u64()?;
                for ch in 0..64 {
                    let flags = r.u8()?;
                    let value = r.bytes(2)?;
                    let value = u16::from_le_bytes([value[0], value[1]]);
                    s.bank.deferred[ch] = flags & 1 != 0;
                    s.bank.done[ch] = (flags & 2 != 0).then_some(value);
                }
                for _ in 0..r.u8()? {
                    s.bank.pending.push(r.u8()?);
                }
                sources.push(s);
                continue;
            }
            if s.kind == 4 {
                let a = &mut s.ssi;
                a.tx_chan = r.u32()?;
                a.tx_vector = r.u32()?;
                a.force_rte = r.u32()?;
                a.request_hz = r.u64()? as i64;
                a.ips = r.u64()? as i64;
                a.now = r.u64()? as i64;
                let has = r.u8()? != 0;
                let (num, den) = (r.u64()? as i64, r.u64()? as i64);
                if has {
                    if den <= 0 || a.request_hz % den != 0 {
                        return Err("the audio model's next request is not on its clock".into());
                    }
                    a.next = Some(num as i128 * (a.request_hz / den) as i128);
                }
                a.batch = r.u8()? != 0;
                let has = r.u8()? != 0;
                let due = r.u64()? as i64;
                a.due = has.then_some(due);
                a.enabled = r.u8()? != 0;
                a.int50_asserted = r.u8()? != 0;
                a.int50_delivered = r.u8()? != 0;
                a.force_asserted = r.u8()? != 0;
                a.force_delivered = r.u8()? != 0;
                let ipl = r.u8()?;
                a.render.ipl = (ipl != 0xff).then_some(ipl as u32);
                a.render.since = r.u64()? as i64;
                a.render.waiting = r.u8()? != 0;
                sources.push(s);
                continue;
            }
            if s.kind >= 2 {
                s.forced.base = r.u32()?;
                s.forced.first_vector = r.u32()?;
                s.forced.sources = r.u64()?;
                s.forced.asserted = r.u64()?;
                s.forced.delivered = r.u64()?;
                sources.push(s);
                continue;
            }
            for _ in 0..r.u8()? {
                s.clock.channels.push(r.u8()?);
            }
            s.clock.ips = f64::from_bits(r.u64()?);
            for ch in 0..4 {
                let has = r.u8()? != 0;
                let next = f64::from_bits(r.u64()?);
                s.clock.next[ch] = has.then_some(next);
            }
            s.clock.pending = r.u8()?;
            s.arm = r.u8()?;
            s.clock.held = r.u8()? != 0;
            s.now = r.u64()? as i64;
            sources.push(s);
        }
        let mut t = Trace {
            init,
            regions,
            code_hooks,
            sources,
            nums: Vec::new(),
            items: Vec::new(),
            blob: Vec::new(),
            states: Vec::new(),
            emulated_ns: 0,
            raised: 0,
            last: Vec::new(),
            head: r.at,
            card: None,
        };
        while r.at < data.len() {
            let item = match r.u8()? {
                1 => Item::StepStart(r.u32()?),
                2 => Item::Tb(r.u32()?, r.u32()?),
                3 => Item::Code(r.u32()?),
                4 => {
                    let write = r.u8()? != 0;
                    let size = r.u8()?;
                    Item::Mem { write, size, addr: r.u32()?, value: r.u32()? }
                }
                5 => Item::Intr(r.u32()?),
                tag @ (6 | 12) => {
                    let addr = r.u32()?;
                    let len = r.u32()?;
                    let off = t.blob.len() as u32;
                    t.blob.extend_from_slice(r.bytes(len as usize)?);
                    if tag == 6 {
                        Item::WMem { addr, off, len }
                    } else {
                        Item::HRead { addr, off, len }
                    }
                }
                7 => Item::WReg(r.u8()?, r.u32()?),
                8 => Item::Stop,
                9 => {
                    t.states.push(r.state()?);
                    Item::StepEnd(t.states.len() as u32 - 1, r.u32()?)
                }
                tag @ (20 | 21 | 22) => {
                    let src = r.u8()?;
                    t.nums.push(r.u64()? as i64);
                    let at = t.nums.len() as u32 - 1;
                    match tag {
                        20 => Item::SrcStep(src, at),
                        21 => Item::SrcStepEnd(src, at),
                        _ => Item::SrcService(src, at),
                    }
                }
                23 => Item::SrcServiceEnd(r.u8()?),
                24 => Item::RenderHolds(r.u8()? != 0, r.u8()?),
                27 => {
                    t.nums.push(r.u64()? as i64);
                    Item::IdleSkip(t.nums.len() as u32 - 1)
                }
                26 => {
                    let kind = r.u8()?;
                    let at = t.nums.len() as u32;
                    for _ in 0..3 {
                        t.nums.push(r.u32()? as i32 as i64);
                    }
                    Item::Input(kind, at)
                }
                25 => {
                    let len = r.u32()?;
                    let off = t.blob.len() as u32;
                    t.blob.extend_from_slice(r.bytes(len as usize)?);
                    Item::Audio { off, len }
                }
                11 => {
                    t.emulated_ns = r.u64()?;
                    t.raised = r.u64()?;
                    continue;
                }
                13 => {
                    for _ in 0..r.u32()? {
                        let addr = r.u32()?;
                        let len = r.u32()? as usize;
                        t.last.push((addr, r.bytes(len)?.to_vec()));
                    }
                    continue;
                }
                30 => {
                    let flag = r.u32()?;
                    let mut card = crate::esdhc::Card { blocks: r.u32()?, ..Default::default() };
                    for _ in 0..r.u32()? {
                        let n = r.u32()?;
                        card.write(n, r.bytes(crate::esdhc::SECTOR)?);
                    }
                    t.card = Some(crate::esdhc::Esdhc::for_driver(flag, card));
                    continue;
                }
                tag => return Err(format!("unknown item {tag} at byte {}", r.at - 1)),
            };
            t.items.push(item);
        }
        Ok(t)
    }

    /// A recording with only what a run from its start needs: the starting
    /// state as `data` has it, the first step's PC, the panel input with
    /// its times, and the audio that was played (to compare with). The
    /// result is a trace file too.
    pub fn start_only(&self, data: &[u8]) -> Vec<u8> {
        let mut out = data[..self.head].to_vec();
        let mut at = None;
        for (n, item) in self.items.iter().enumerate() {
            match *item {
                Item::StepStart(pc) if n == 0 => {
                    out.push(1);
                    out.extend_from_slice(&pc.to_le_bytes());
                }
                Item::SrcService(_, i) | Item::SrcStep(_, i) => at = Some(self.nums[i as usize]),
                Item::Input(kind, i) => {
                    if let Some(at) = at {
                        out.extend_from_slice(&[20, 0]);
                        out.extend_from_slice(&(at as u64).to_le_bytes());
                    }
                    out.extend_from_slice(&[26, kind]);
                    for v in &self.nums[i as usize..i as usize + 3] {
                        out.extend_from_slice(&(*v as i32).to_le_bytes());
                    }
                }
                Item::Audio { off, len } => {
                    out.push(25);
                    out.extend_from_slice(&len.to_le_bytes());
                    out.extend_from_slice(&self.blob[off as usize..(off + len) as usize]);
                }
                _ => {}
            }
        }
        if let Some(e) = &self.card {
            out.extend_from_slice(&card_item(e.status - 0x30, &e.card));
        }
        out.push(11);
        out.extend_from_slice(&self.emulated_ns.to_le_bytes());
        out.extend_from_slice(&self.raised.to_le_bytes());
        out
    }

    /// The big-endian word the starting memory has at `addr`.
    pub fn word_at(&self, addr: u32) -> Option<u32> {
        self.regions.iter().find_map(|r| {
            let off = addr.checked_sub(r.addr)? as usize;
            let b = r.data.get(off..off + 4)?;
            Some(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
        })
    }

    pub fn read(path: &str) -> Result<Trace, String> {
        let data = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Trace::parse(&data).map_err(|e| format!("{path}: {e}"))
    }
}

/// The item that gives a machine its +Drive: the address of the card
/// driver's "storage is up" flag, the card's size in sectors, then each
/// sector that holds anything.
pub fn card_item(flag: u32, card: &crate::esdhc::Card) -> Vec<u8> {
    let mut out = vec![30];
    out.extend_from_slice(&flag.to_le_bytes());
    out.extend_from_slice(&card.blocks.to_le_bytes());
    out.extend_from_slice(&(card.sectors.len() as u32).to_le_bytes());
    for (n, data) in &card.sectors {
        out.extend_from_slice(&n.to_le_bytes());
        out.extend_from_slice(&data[..]);
    }
    out
}
