//! The C entry points an app calls to time the whole machine on a device:
//! open a starting-state file (`cfstart`'s), then run it a chunk at a time.
//! Each chunk reports what it ran and a hash of the audio it played, so a
//! device's run can be compared with the Mac's for the same chunks
//! (`cfchunks` prints them). The audio is dropped after each chunk: a long
//! run must not keep it.
//!
//!     void *cfcore_open(const char *path, int64_t ips);
//!     int cfcore_run(void *bench, double seconds, struct cf_chunk *out);
//!     void cfcore_close(void *bench);
//!     const char *cfcore_error(void);
//!     uint64_t cfcore_blocks(void);
//!
//! and to play it: the audio a slice at a time, the panel's input and LEDs,
//! and the screen. One thread makes all of these calls.
//!
//!     void cfcore_live(void *bench);
//!     long cfcore_advance(void *bench, long frames, float *pcm, long cap);
//!     void cfcore_key(void *bench, int column, int bit, int down);
//!     void cfcore_pad(void *bench, int index, int velocity);
//!     void cfcore_turn(void *bench, int encoder, int steps);
//!     uint64_t cfcore_leds(void *bench);
//!     float cfcore_gain(void *bench);
//!     int cfcore_screen(void *bench, uint32_t pointer_at, uint8_t *out);
use crate::aot;
use crate::machine::Machine;
use crate::trace::Trace;
use std::cell::RefCell;
use std::ffi::{c_char, c_int, c_void, CStr, CString};

/// One chunk's result. `struct cf_chunk` in C, fields in this order.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Chunk {
    /// Emulated seconds this chunk ran.
    pub emulated_s: f64,
    /// Instructions executed (not the idle skip's credit).
    pub instructions: i64,
    /// ... of which the interpreter ran these.
    pub interpreted: i64,
    /// Instructions the idle skip credited.
    pub idle: i64,
    pub interrupts: u64,
    /// Audio frames played, their left-channel level, and their FNV-1a hash.
    pub frames: u64,
    pub level: f64,
    pub hash: u64,
}

pub struct Bench {
    k: Machine,
    seen: (i64, u64, i64, u64),
}

thread_local! {
    static ERROR: RefCell<CString> = RefCell::new(CString::default());
}

fn fail(e: String) {
    ERROR.with(|s| *s.borrow_mut() = CString::new(e).unwrap_or_default());
}

impl Bench {
    pub fn open(path: &str, ips: i64) -> Result<Bench, String> {
        if ips <= 0 {
            return Err("ips must be positive".into());
        }
        let tr = Trace::read(path)?;
        Ok(Bench { k: Machine::from_trace(&tr, ips)?, seen: (0, 0, 0, 0) })
    }

    /// Run `seconds` more of emulated time. The chunk is filled in even
    /// when the machine stops early.
    pub fn run(&mut self, seconds: f64) -> (Chunk, Result<(), String>) {
        let k = &mut self.k;
        let from = k.now;
        let result = k.run(from + (seconds * k.ips as f64) as i64);
        let mut d = k.dev.borrow_mut();
        let executed = k.now - k.idle_skipped;
        let (mut hash, mut sum) = (0xcbf2_9ce4_8422_2325u64, 0.0);
        for b in &d.audio {
            hash = (hash ^ *b as u64).wrapping_mul(0x0000_0100_0000_01b3);
        }
        let frames = d.audio.len() / 8;
        for f in d.audio.chunks_exact(8) {
            let v = i16::from_be_bytes([f[1], f[2]]) as f64;
            sum += v * v;
        }
        d.audio.clear();
        let chunk = Chunk {
            emulated_s: (k.now - from) as f64 / k.ips as f64,
            instructions: executed - self.seen.0,
            interpreted: (k.it.executed - self.seen.1) as i64,
            idle: k.idle_skipped - self.seen.2,
            interrupts: d.raised - self.seen.3,
            frames: frames as u64,
            level: if frames > 0 { (sum / frames as f64).sqrt() } else { 0.0 },
            hash,
        };
        self.seen = (executed, k.it.executed, k.idle_skipped, d.raised);
        (chunk, result)
    }
}

/// The panel's frame buffer: 128x64, a bit a pixel (emu/panel.py).
pub const SCREEN_BYTES: usize = 1024;

impl Bench {
    /// Forget the recording's panel input: the panel is played live.
    pub fn live(&mut self) {
        self.k.inputs.clear();
    }

    /// Run until `frames` more audio frames have been played (or a little
    /// over: the machine stops between steps) and append them to `pcm` as
    /// interleaved stereo floats. -> why the machine stopped, if it did.
    pub fn advance(&mut self, frames: usize, pcm: &mut Vec<f32>) -> Result<(), String> {
        let k = &mut self.k;
        // A step is under a millisecond; a request clock tick is ips/48000.
        let slice = (k.ips / 48_000).max(1) * 16;
        let mut got = 0;
        // No audio for a second of emulated time means the firmware has
        // stopped playing it: hand back what there is.
        let give_up = k.now + k.ips;
        while got < frames && k.now < give_up {
            let result = k.run(k.now + slice);
            let mut d = k.dev.borrow_mut();
            for f in d.audio.chunks_exact(8) {
                for w in [&f[0..4], &f[4..8]] {
                    let v = i32::from_be_bytes([0, w[1], w[2], w[3]]) << 8 >> 8;
                    pcm.push(v as f32 / 8_388_608.0);
                }
                got += 1;
            }
            d.audio.clear();
            result?;
        }
        Ok(())
    }

    /// The gain the output takes from the codec level the firmware's
    /// VOLUME sets (emu/modelboard.py's `output_gain`): registers 0x1E and
    /// 0x1F hold the level, 0x26 after boot and 0x10 at the bottom. Both
    /// zero is a codec not set up yet.
    pub fn gain(&self) -> f32 {
        let regs = &self.k.dev.borrow().i2c.regs;
        let (left, right) = (regs[0x1E], regs[0x1F]);
        if left == 0 && right == 0 {
            return 1.0;
        }
        let channel = |v: u8| (((v & 0x7F) as f32 - 16.0) / (0x26 as f32 - 16.0)).clamp(0.0, 1.5);
        (channel(left) + channel(right)) / 2.0
    }

    /// The LEDs lit: bit `row * 8 + bit`.
    pub fn leds(&self) -> u64 {
        self.k.dev.borrow().panel.lit().iter().fold(0, |m, led| m | 1u64 << led)
    }

    /// The frame buffer the pointer at `pointer_at` names. -> false when it
    /// does not point into the firmware's memory.
    pub fn screen(&self, pointer_at: u32, out: &mut [u8; SCREEN_BYTES]) -> bool {
        let m = &self.k.m;
        if !m.is_mapped(pointer_at) {
            return false;
        }
        let mut p = [0u8; 4];
        m.read_bytes(pointer_at, &mut p);
        let at = u32::from_be_bytes(p);
        if !(0x4000_0000..0x5000_0000).contains(&at) || !m.is_mapped(at) || !m.is_mapped(at + 1023) {
            return false;
        }
        m.read_bytes(at, out);
        true
    }
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_live(bench: *mut c_void) {
    (*(bench as *mut Bench)).live();
}

/// -> the frames written to `pcm` (two floats each, at most `cap` frames),
/// or -1 if the machine stopped (`cfcore_error` says where).
///
/// # Safety
/// `bench` must come from `cfcore_open` and `pcm` must hold `cap` frames.
#[no_mangle]
pub unsafe extern "C" fn cfcore_advance(bench: *mut c_void, frames: isize, pcm: *mut f32, cap: isize) -> isize {
    let mut buf = Vec::with_capacity(2 * cap.max(0) as usize);
    let result = (*(bench as *mut Bench)).advance(frames.max(0) as usize, &mut buf);
    let n = (buf.len() / 2).min(cap.max(0) as usize);
    std::ptr::copy_nonoverlapping(buf.as_ptr(), pcm, 2 * n);
    match result {
        Ok(()) => n as isize,
        Err(e) => {
            fail(e);
            -1
        }
    }
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_key(bench: *mut c_void, column: c_int, bit: c_int, down: c_int) {
    (*(bench as *mut Bench)).k.dev.borrow_mut().panel.key(column as u8, bit as u8, down != 0);
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_pad(bench: *mut c_void, index: c_int, velocity: c_int) {
    (*(bench as *mut Bench)).k.dev.borrow_mut().panel.pad(index as u8, velocity);
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_turn(bench: *mut c_void, encoder: c_int, steps: c_int) {
    (*(bench as *mut Bench)).k.dev.borrow_mut().panel.turn(encoder as u8, steps);
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_leds(bench: *mut c_void) -> u64 {
    (*(bench as *mut Bench)).leds()
}

/// # Safety
/// `bench` must come from `cfcore_open`.
#[no_mangle]
pub unsafe extern "C" fn cfcore_gain(bench: *mut c_void) -> f32 {
    (*(bench as *mut Bench)).gain()
}

/// -> 1 and the 1024 bytes in `out`, or 0.
///
/// # Safety
/// `bench` must come from `cfcore_open` and `out` must hold 1024 bytes.
#[no_mangle]
pub unsafe extern "C" fn cfcore_screen(bench: *mut c_void, pointer_at: u32, out: *mut u8) -> c_int {
    (*(bench as *mut Bench)).screen(pointer_at, &mut *(out as *mut [u8; SCREEN_BYTES])) as c_int
}

/// -> the bench, or null with `cfcore_error` saying why.
///
/// # Safety
/// `path` must be a NUL-terminated string.
#[no_mangle]
pub unsafe extern "C" fn cfcore_open(path: *const c_char, ips: i64) -> *mut c_void {
    let path = CStr::from_ptr(path).to_string_lossy();
    match Bench::open(&path, ips) {
        Ok(b) => Box::into_raw(Box::new(b)) as *mut c_void,
        Err(e) => {
            fail(e);
            std::ptr::null_mut()
        }
    }
}

/// -> 0, or 1 if the machine stopped (`cfcore_error` says where).
///
/// # Safety
/// `bench` must come from `cfcore_open` and `out` must be writable.
#[no_mangle]
pub unsafe extern "C" fn cfcore_run(bench: *mut c_void, seconds: f64, out: *mut Chunk) -> c_int {
    let (chunk, result) = (*(bench as *mut Bench)).run(seconds);
    *out = chunk;
    match result {
        Ok(()) => 0,
        Err(e) => {
            fail(e);
            1
        }
    }
}

/// # Safety
/// `bench` must come from `cfcore_open`, and is not used again.
#[no_mangle]
pub unsafe extern "C" fn cfcore_close(bench: *mut c_void) {
    if !bench.is_null() {
        drop(Box::from_raw(bench as *mut Bench));
    }
}

/// The last failure on this thread. Valid until the next one.
#[no_mangle]
pub extern "C" fn cfcore_error() -> *const c_char {
    ERROR.with(|s| s.borrow().as_ptr())
}

/// How many translated blocks this build has.
#[no_mangle]
pub extern "C" fn cfcore_blocks() -> u64 {
    aot::BLOCKS as u64
}
