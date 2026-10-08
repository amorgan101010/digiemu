//! Print the panel's screen from a starting-state file, as the app reads
//! it: the 128x64 frame buffer the pointer at ADDR names (emu/panel.py has
//! the layout).
//!
//!     cfscreen START ADDR [--seconds S --ips N]
use cfcore::ffi::{Bench, SCREEN_BYTES};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut plain = Vec::new();
    let (mut seconds, mut ips) = (0.0f64, 211_700_000i64);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--seconds" => seconds = it.next().and_then(|v| v.parse().ok()).expect("--seconds S"),
            "--ips" => ips = it.next().and_then(|v| v.parse().ok()).expect("--ips N"),
            _ => plain.push(a.clone()),
        }
    }
    let [start, addr] = plain.as_slice() else {
        eprintln!("usage: cfscreen START ADDR [--seconds S --ips N]");
        std::process::exit(2);
    };
    let addr = u32::from_str_radix(addr.trim_start_matches("0x"), 16).expect("ADDR in hex");
    let mut b = Bench::open(start, ips).unwrap_or_else(|e| panic!("{e}"));
    if seconds > 0.0 {
        b.run(seconds).1.unwrap_or_else(|e| panic!("{e}"));
    }
    let mut buf = [0u8; SCREEN_BYTES];
    if !b.screen(addr, &mut buf) {
        eprintln!("{addr:#010x} does not point at a frame buffer");
        std::process::exit(1);
    }
    for y in 0..64 {
        let row: String =
            (0..128).map(|x| if buf[(7 - y / 8) + 8 * x] >> (y % 8) & 1 != 0 { '#' } else { '.' }).collect();
        println!("{row}");
    }
}
