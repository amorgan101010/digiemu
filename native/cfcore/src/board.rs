//! What the Model:Cycles' and Model:Samples' board adds: the front panel
//! the main CPU scans itself (keys, encoders and LED rows multiplexed at
//! 0x8C000000, the pads on the ADC), and the PIT1 flag its delay loop polls.
//!
//! A port of digiemu's `emu/modelboard.py` (`ModelPanel`, `_pit1_delay`)
//! and of the ready line `emu/dsp.py`'s `Fifo` keeps on the same address.
//! `cfreplay` checks every value it puts in front of the firmware against
//! a recording of the Python models.
use crate::timers::Host;
use std::collections::VecDeque;

const PANEL_DATA: u32 = 0x8C00_0002;
const ADC_RESULT: u32 = 0xFC09_4012;
const PCSR1: u32 = 0xFC08_4000;

const KEY_COLUMNS: usize = 4;
const ENCODERS: usize = 16;
const PADS: usize = 6;
const LED_ROWS: usize = 7;

/// Scan frames an input is held for before the next change to it.
const KEY_FRAMES: i64 = 4;
const PAD_FRAMES: i64 = 40;
const ENCODER_FRAMES: i64 = 1;
/// Keeps a fast drag responsive to a reversal.
const MAX_PENDING_TURNS: i32 = 128;

const PAD_ARM: u32 = 0x2148;
const PAD_PER_VELOCITY: u32 = 152;
const PAD_MAX: u32 = 0x7FF8;

/// The two encoder bits at each quarter of a clockwise step.
const CW: [u8; 4] = [0b00, 0b10, 0b11, 0b01];

/// The ADC sample that reads as `velocity` (1..127) on a pad.
pub fn pad_sample(velocity: i32) -> u16 {
    let v = velocity.clamp(1, 127) as u32;
    (PAD_ARM + v * PAD_PER_VELOCITY).min(PAD_MAX) as u16
}

#[derive(Clone, Debug, PartialEq)]
pub struct Panel {
    /// The column the scan has selected.
    pub select: u8,
    /// Scan frames so far: a read of column 0 starts one.
    pub frames: i64,
    /// What the key matrix reads now, a set bit pressed.
    pub keys: [u8; KEY_COLUMNS],
    /// Each encoder's position in `CW`.
    pub phase: [u8; ENCODERS],
    /// Each pad's ADC sample.
    pub pads: [u16; PADS],
    /// The LED rows as the firmware last drove them, active low.
    pub led_rows: [u8; 8],
    pub led_version: u64,
    /// Changes asked for and not yet applied, in the order they came.
    key_want: Vec<((u8, u8), VecDeque<bool>)>,
    key_since: [[i64; 8]; KEY_COLUMNS],
    pad_want: Vec<(u8, VecDeque<u16>)>,
    pad_since: [i64; PADS],
    turns: [i32; ENCODERS],
    turn_since: [i64; ENCODERS],
}

impl Default for Panel {
    fn default() -> Self {
        Panel {
            select: 0,
            frames: 0,
            keys: [0; KEY_COLUMNS],
            phase: [0; ENCODERS],
            pads: [0; PADS],
            led_rows: [0xFF; 8],
            led_version: 0,
            key_want: Vec::new(),
            key_since: [[-KEY_FRAMES; 8]; KEY_COLUMNS],
            pad_want: Vec::new(),
            pad_since: [-PAD_FRAMES; PADS],
            turns: [0; ENCODERS],
            turn_since: [0; ENCODERS],
        }
    }
}

impl Panel {
    /// Press or release the key at `column`, `bit` of the matrix.
    pub fn key(&mut self, column: u8, bit: u8, down: bool) {
        if column as usize >= KEY_COLUMNS || bit >= 8 {
            return;
        }
        match self.key_want.iter_mut().find(|(pos, _)| *pos == (column, bit)) {
            Some((_, q)) => q.push_back(down),
            None => self.key_want.push(((column, bit), VecDeque::from([down]))),
        }
    }

    /// Press pad `index` (its ADC channel) at `velocity`; 0 lifts it.
    pub fn pad(&mut self, index: u8, velocity: i32) {
        if index as usize >= PADS {
            return;
        }
        let sample = if velocity != 0 { pad_sample(velocity) } else { 0 };
        match self.pad_want.iter_mut().find(|(i, _)| *i == index) {
            Some((_, q)) => q.push_back(sample),
            None => self.pad_want.push((index, VecDeque::from([sample]))),
        }
    }

    /// Turn `encoder` by `steps` firmware steps; negative is anticlockwise.
    pub fn turn(&mut self, encoder: u8, steps: i32) {
        let Some(pending) = self.turns.get_mut(encoder as usize) else { return };
        let delta = 2 * steps;
        if (*pending as i64) * (delta as i64) < 0 {
            *pending = 0; // discard what is left of the old drag
        }
        let limit = 2 * MAX_PENDING_TURNS;
        *pending = (*pending + delta).clamp(-limit, limit);
    }

    /// The LED ids lit (row * 8 + bit).
    pub fn lit(&self) -> Vec<u8> {
        let mut out = Vec::new();
        for row in 0..LED_ROWS {
            for bit in 0..8 {
                if self.led_rows[row] >> bit & 1 == 0 {
                    out.push((row * 8 + bit) as u8);
                }
            }
        }
        out
    }

    /// A frame starts: apply whatever input has waited long enough.
    fn frame(&mut self) {
        self.frames += 1;
        let now = self.frames;
        let mut i = 0;
        while i < self.key_want.len() {
            let (column, bit) = self.key_want[i].0;
            let since = &mut self.key_since[column as usize][bit as usize];
            if now - *since < KEY_FRAMES {
                i += 1;
                continue;
            }
            *since = now;
            let want = self.key_want[i].1.pop_front().unwrap_or(false);
            let mask = 1u8 << bit;
            if want {
                self.keys[column as usize] |= mask;
            } else {
                self.keys[column as usize] &= !mask;
            }
            if self.key_want[i].1.is_empty() {
                self.key_want.remove(i);
            } else {
                i += 1;
            }
        }
        let mut i = 0;
        while i < self.pad_want.len() {
            let index = self.pad_want[i].0 as usize;
            if now - self.pad_since[index] < PAD_FRAMES {
                i += 1;
                continue;
            }
            self.pad_since[index] = now;
            self.pads[index] = self.pad_want[i].1.pop_front().unwrap_or(0);
            if self.pad_want[i].1.is_empty() {
                self.pad_want.remove(i);
            } else {
                i += 1;
            }
        }
        for e in 0..ENCODERS {
            if self.turns[e] == 0 || now - self.turn_since[e] < ENCODER_FRAMES {
                continue;
            }
            let step = self.turns[e].signum();
            self.phase[e] = ((self.phase[e] as i32 + step).rem_euclid(4)) as u8;
            self.turns[e] -= step;
            self.turn_since[e] = now;
        }
    }

    /// The byte column `n` reads.
    fn column(&self, n: usize) -> u8 {
        if n < KEY_COLUMNS {
            return self.keys[n];
        }
        let base = (n - KEY_COLUMNS) * 4;
        (0..4).fold(0, |byte, k| byte | CW[self.phase[base + k] as usize] << (2 * k))
    }

    /// A 16-bit write to the data register: a column select (bit 0 set) or
    /// an LED row (bit 0 clear, the row's bits in the high byte).
    fn data_write(&mut self, value: u32) {
        let row = ((value >> 5) & 7) as usize;
        if value & 1 != 0 {
            self.select = row as u8;
        } else if row < LED_ROWS {
            let bits = ((value >> 8) & 0xFF) as u8;
            if self.led_rows[row] != bits {
                self.led_rows[row] = bits;
                self.led_version += 1;
            }
        }
    }

    pub fn owns(&self, write: bool, addr: u32) -> bool {
        (PANEL_DATA..=PANEL_DATA + 1).contains(&addr)
            || (!write && ((ADC_RESULT..=ADC_RESULT + 1).contains(&addr) || (PCSR1..=PCSR1 + 1).contains(&addr)))
    }

    /// The firmware is about to read or write a board register.
    pub fn access(&mut self, h: &mut dyn Host, write: bool, addr: u32, size: u32, value: u32) {
        if write {
            if addr == PANEL_DATA {
                let v = if size >= 2 { value >> (8 * (size - 2)) } else { (value & 0xFF) << 8 };
                self.data_write(v & 0xFFFF);
            }
        } else if addr == PANEL_DATA || addr == PANEL_DATA + 1 {
            // The port's ready line first, then the scanned column over it.
            h.write(PANEL_DATA, &[0x00, 0x01]);
            if addr == PANEL_DATA {
                if self.select == 0 {
                    self.frame();
                }
                let column = self.column(self.select as usize);
                h.write(PANEL_DATA, &[column]);
            }
        } else if (ADC_RESULT..=ADC_RESULT + 1).contains(&addr) {
            let sample = self.pads.get(self.select as usize).copied().unwrap_or(0);
            h.write(ADC_RESULT, &sample.to_be_bytes());
        } else {
            // PIT1 is not modelled: its flag reads as set, so the firmware's
            // busy-wait delay is instant.
            let mut cur = [0u8; 2];
            h.read(PCSR1, &mut cur);
            h.write(PCSR1, &(u16::from_be_bytes(cur) | 0x0004).to_be_bytes());
        }
    }
}

const I2C0: u32 = 0xFC05_8000;
const I2CR: u32 = 0x08;
const I2SR: u32 = 0x0C;
const I2DR: u32 = 0x10;
const IEN: u8 = 0x80;
const MSTA: u8 = 0x20;
const MTX: u8 = 0x10;
const RSTA: u8 = 0x04;
const ICF: u8 = 0x80;
const IBB: u8 = 0x20;
const IAL: u8 = 0x10;
const IIF: u8 = 0x02;
const RXAK: u8 = 0x01;
/// The audio codec's address on the bus.
pub const CODEC_ADDRESS: u8 = 0x1A;

/// I2C0 as a bus master with one register-file device on it, the audio
/// codec. A port of digiemu's `emu/i2c.py`: the controller's flags, not its
/// timing. Every byte moves at once, and the firmware polls.
#[derive(Clone, Debug, PartialEq)]
pub struct I2c {
    pub cr: u8,
    pub sr: u8,
    /// Between START and STOP.
    pub busy: bool,
    pub expect_address: bool,
    /// The codec answered the address byte.
    pub target: bool,
    pub reading: bool,
    /// The byte the next data-register read returns.
    pub rx: u8,
    /// The codec: its registers, its register pointer, and whether the
    /// next byte written sets the pointer.
    pub regs: [u8; 256],
    pub pointer: u8,
    pub fresh: bool,
    pub transfers: u64,
}

impl Default for I2c {
    fn default() -> Self {
        I2c {
            cr: 0,
            sr: 0,
            busy: false,
            expect_address: false,
            target: false,
            reading: false,
            rx: 0xFF,
            regs: [0; 256],
            pointer: 0,
            fresh: true,
            transfers: 0,
        }
    }
}

impl I2c {
    pub fn owns(&self, write: bool, addr: u32) -> bool {
        let lo = if write { I2C0 + I2CR } else { I2C0 + I2SR };
        (lo..=I2C0 + I2DR).contains(&addr)
    }

    fn start(&mut self) {
        self.busy = true;
        self.expect_address = true;
        self.reading = false;
        self.target = false;
    }

    fn write_cr(&mut self, value: u8, old: u8) {
        self.cr = value;
        if value & IEN == 0 {
            self.busy = false;
        } else if (value & MSTA != 0 && old & MSTA == 0) || (value & MSTA != 0 && value & RSTA != 0) {
            self.start();
        } else if old & MSTA != 0 && value & MSTA == 0 {
            self.busy = false;
            self.target = false;
        }
    }

    fn write_dr(&mut self, byte: u8) {
        if !self.busy || self.cr & MTX == 0 {
            return;
        }
        if self.expect_address {
            self.expect_address = false;
            self.target = byte >> 1 == CODEC_ADDRESS;
            self.reading = byte & 1 != 0;
            if self.target {
                self.fresh = true;
            }
        } else if self.target {
            if self.fresh {
                self.pointer = byte;
                self.fresh = false;
            } else {
                self.regs[self.pointer as usize] = byte;
                self.pointer = self.pointer.wrapping_add(1);
            }
        }
        self.sr = (self.sr & !RXAK) | ICF | IIF | if self.target { 0 } else { RXAK };
        self.transfers += 1;
    }

    fn read_dr(&mut self) -> u8 {
        let byte = self.rx;
        if self.busy && self.cr & MTX == 0 && self.reading {
            self.rx = if self.target {
                let b = self.regs[self.pointer as usize];
                self.pointer = self.pointer.wrapping_add(1);
                b
            } else {
                0xFF
            };
            self.sr |= ICF | IIF;
            self.transfers += 1;
        }
        byte
    }

    /// The firmware is about to read or write an I2C0 register.
    pub fn access(&mut self, h: &mut dyn Host, write: bool, addr: u32, size: u32, value: u32) {
        let off = addr - I2C0;
        if !write {
            if off <= I2SR && I2SR < off + size {
                let status = (self.sr & !IBB) | if self.busy { IBB } else { 0 };
                h.write(I2C0 + I2SR, &[status]);
            }
            if off <= I2DR && I2DR < off + size {
                let byte = self.read_dr();
                h.write(I2C0 + I2DR, &[byte]);
            }
            return;
        }
        for k in 0..size {
            let byte = (value >> (8 * (size - 1 - k))) as u8;
            match off + k {
                I2CR => {
                    let mut old = [0u8; 1];
                    h.read(I2C0 + I2CR, &mut old);
                    self.write_cr(byte, old[0]);
                }
                I2SR => self.sr &= byte | !(IIF | IAL),
                I2DR => self.write_dr(byte),
                _ => {}
            }
        }
    }
}
