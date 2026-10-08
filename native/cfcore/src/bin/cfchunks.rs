//! Run the whole machine a chunk at a time, as the device benchmark app
//! does (`src/ffi.rs`), and print each chunk: its time, and the hash of the
//! audio it played, to compare with the app's log.
//!
//!     cfchunks START --ips N [--chunk S] [--count N]
use cfcore::ffi::Bench;
use std::time::Instant;

fn main() {
    let mut path = None;
    let (mut ips, mut chunk, mut count) = (0i64, 10.0f64, 12u32);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--ips" => ips = args.next().and_then(|v| v.parse().ok()).expect("--ips N"),
            "--chunk" => chunk = args.next().and_then(|v| v.parse().ok()).expect("--chunk S"),
            "--count" => count = args.next().and_then(|v| v.parse().ok()).expect("--count N"),
            _ => path = Some(a),
        }
    }
    let mut b = Bench::open(&path.expect("START"), ips).unwrap_or_else(|e| panic!("{e}"));
    for n in 1..=count {
        let t0 = Instant::now();
        let (c, result) = b.run(chunk);
        let wall = t0.elapsed().as_secs_f64();
        println!(
            "{n} {:.3} s in {wall:.3} s wall: {:.4} s per emulated s; {:.1}M instructions, {} interpreted, {} interrupts, {} frames, level {:.0}, hash {:016x}",
            c.emulated_s,
            wall / c.emulated_s.max(1e-9),
            c.instructions as f64 / 1e6,
            c.interpreted,
            c.interrupts,
            c.frames,
            c.level,
            c.hash
        );
        if let Err(e) = result {
            eprintln!("STOPPED: {e}");
            std::process::exit(1);
        }
    }
}
