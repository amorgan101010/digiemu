//! Run the whole machine natively from a recording's starting state, and
//! compare the audio it plays with what the Python emulator played.
//!
//!     cfrun TRACE --ips N [--seconds S] [--wav DIR]
//!
//! Nothing from the recording is replayed except its starting state and its
//! panel input. The clock here counts real instructions, so the two runs do
//! not line up step for step and the samples are not expected to be equal
//! bit for bit: the comparison is of the sound (level over time, and the
//! correlation of the two signals at their best alignment).
use cfcore::aot;
use cfcore::machine::Machine;
use cfcore::trace::{Item, Trace};
use std::time::Instant;

/// 16-bit left-channel samples from SSI frames (two big-endian 32-bit
/// words, the 24-bit sample right-justified: its top 16 bits are bytes 1
/// and 2).
fn left(raw: &[u8]) -> Vec<f64> {
    raw.chunks_exact(8).map(|f| i16::from_be_bytes([f[1], f[2]]) as f64).collect()
}

fn rms(x: &[f64]) -> f64 {
    if x.is_empty() {
        return 0.0;
    }
    (x.iter().map(|v| v * v).sum::<f64>() / x.len() as f64).sqrt()
}

/// The best normalized correlation of `a` and `b` over lags of up to
/// `max_lag` samples either way. -> (correlation, lag of b after a).
fn best_correlation(a: &[f64], b: &[f64], max_lag: i64) -> (f64, i64) {
    let mut best = (-1.0, 0);
    let mut lag = -max_lag;
    while lag <= max_lag {
        let (mut ab, mut aa, mut bb) = (0.0, 0.0, 0.0);
        for (i, x) in a.iter().enumerate() {
            let j = i as i64 + lag;
            if j >= 0 && (j as usize) < b.len() {
                let y = b[j as usize];
                ab += x * y;
                aa += x * x;
                bb += y * y;
            }
        }
        if aa > 0.0 && bb > 0.0 {
            let c = ab / (aa * bb).sqrt();
            if c > best.0 {
                best = (c, lag);
            }
        }
        lag += 1;
    }
    best
}

fn wav(path: &str, raw: &[u8]) {
    let mut pcm = Vec::with_capacity(raw.len() / 2);
    for f in raw.chunks_exact(4) {
        pcm.extend_from_slice(&[f[2], f[1]]);
    }
    let mut out = Vec::with_capacity(pcm.len() + 44);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + pcm.len() as u32).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&48000u32.to_le_bytes());
    out.extend_from_slice(&(48000u32 * 4).to_le_bytes());
    out.extend_from_slice(&4u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&(pcm.len() as u32).to_le_bytes());
    out.extend_from_slice(&pcm);
    std::fs::write(path, out).unwrap_or_else(|e| panic!("{path}: {e}"));
}

fn main() {
    let mut path = None;
    let mut ips = 0i64;
    let mut seconds = 0.0f64;
    let mut dir = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--ips" => ips = args.next().and_then(|v| v.parse().ok()).expect("--ips N"),
            "--seconds" => seconds = args.next().and_then(|v| v.parse().ok()).expect("--seconds S"),
            "--wav" => dir = args.next(),
            _ => path = Some(a),
        }
    }
    let tr = Trace::read(&path.expect("TRACE")).unwrap_or_else(|e| panic!("{e}"));
    if ips <= 0 {
        panic!("--ips N: this core's instructions per emulated second (cfreplay prints the recording's)");
    }
    if seconds <= 0.0 {
        seconds = tr.emulated_ns as f64 / 1e9;
    }
    let mut python = Vec::new();
    for item in &tr.items {
        if let Item::Audio { off, len } = *item {
            python.extend_from_slice(&tr.blob[off as usize..(off + len) as usize]);
        }
    }
    let mut k = Machine::from_trace(&tr, ips).unwrap_or_else(|e| panic!("{e}"));
    drop(tr);
    eprintln!(
        "{} translated blocks in this build; running {seconds:.3} emulated s at {ips} instructions/s",
        aot::BLOCKS
    );
    let t0 = Instant::now();
    let result = k.run((seconds * ips as f64) as i64);
    let wall = t0.elapsed().as_secs_f64();
    let ran = k.now as f64 / ips as f64;
    if let Err(e) = &result {
        eprintln!("STOPPED after {ran:.3} emulated s: {e}");
    }
    let d = k.dev.borrow();
    let native = &d.audio;
    let executed = k.now - k.idle_skipped;
    eprintln!(
        "{ran:.3} emulated s in {wall:.3} s of CPU: {:.4} s per emulated s; {} steps, {} interrupts, {:.0}M instructions run ({:.1}% of the time idle), {:.2}% of them interpreted",
        wall / ran.max(1e-9),
        k.steps,
        d.raised,
        executed as f64 / 1e6,
        100.0 * k.idle_skipped as f64 / k.now.max(1) as f64,
        100.0 * k.it.executed as f64 / executed.max(1) as f64
    );
    eprintln!(
        "audio: {} frames played ({:.3} s at 48 kHz); the recording has {} ({:.3} s)",
        native.len() / 8,
        native.len() as f64 / 8.0 / 48000.0,
        python.len() / 8,
        python.len() as f64 / 8.0 / 48000.0
    );
    let (a, b) = (left(native), left(&python));
    let n = a.len().min(b.len());
    if n > 0 {
        let same = native.iter().zip(python.iter()).take_while(|(x, y)| x == y).count();
        eprintln!("identical to the recording for the first {} frames", same / 8);
        let (c, lag) = best_correlation(&a[..n], &b[..n], 2400);
        eprintln!(
            "left channel over {:.3} s: level {:.0} here, {:.0} in the recording; best correlation {:.4} with the recording {:+} samples later",
            n as f64 / 48000.0,
            rms(&a[..n]),
            rms(&b[..n]),
            c,
            lag
        );
        let win = 4800;
        let mut line = String::new();
        for w in 0..(n / win).min(40) {
            let (x, y) = (rms(&a[w * win..(w + 1) * win]), rms(&b[w * win..(w + 1) * win]));
            line.push_str(&format!(" {:.0}/{:.0}", x, y));
        }
        eprintln!("level per 0.1 s, here/recording:{line}");
    }
    if let Some(dir) = dir {
        std::fs::create_dir_all(&dir).unwrap();
        wav(&format!("{dir}/native.wav"), native);
        wav(&format!("{dir}/python.wav"), &python);
    }
    if result.is_err() {
        std::process::exit(1);
    }
}
