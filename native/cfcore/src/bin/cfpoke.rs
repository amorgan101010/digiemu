//! Work the panel of a machine from the command line and watch its LEDs
//! and screen: for finding out what a key, a knob or an LED is.
//!
//!     cfpoke IN --ips N COMMAND...
//!
//! IN is a starting state or a saved machine; the recording's own panel
//! input is dropped. Commands, in order:
//!
//!     run S          run S emulated seconds
//!     key C B D      press (D 1) or release (D 0) the key at column C, bit B
//!     pad I V        press pad I at velocity V; 0 lifts it
//!     turn E N       turn encoder E by N steps
//!     leds           print the LEDs lit (row * 8 + bit)
//!     watch S        run S seconds, printing each LED that changes
//!     screen AT      print the screen the pointer at hex AT names
//!     peek AT N      print N bytes of guest memory at hex AT
//!     frames         print the panel's scan frames so far and the time
use cfcore::ffi::{Bench, SCREEN_BYTES};

fn lit(mask: u64) -> String {
    (0..64).filter(|i| mask >> i & 1 != 0).map(|i| i.to_string()).collect::<Vec<_>>().join(" ")
}

fn main() {
    let mut args = std::env::args().skip(1);
    let path = args.next().expect("IN");
    let mut ips = 0i64;
    let mut rest: Vec<String> = Vec::new();
    while let Some(a) = args.next() {
        if a == "--ips" {
            ips = args.next().and_then(|v| v.parse().ok()).expect("--ips N");
        } else {
            rest.push(a);
        }
    }
    let mut b = Bench::open(&path, ips).unwrap_or_else(|e| panic!("{e}"));
    b.live();
    let mut pcm = Vec::new();
    let mut run = |b: &mut Bench, seconds: f64| {
        pcm.clear();
        if let Err(e) = b.advance((seconds * 48_000.0) as usize, &mut pcm) {
            eprintln!("STOPPED: {e}");
            std::process::exit(1);
        }
    };
    let mut it = rest.iter();
    let num = |it: &mut std::slice::Iter<String>| -> f64 { it.next().and_then(|v| v.parse().ok()).expect("a number") };
    while let Some(cmd) = it.next() {
        match cmd.as_str() {
            "run" => {
                let s = num(&mut it);
                run(&mut b, s);
            }
            "key" => {
                let (c, bit, d) = (num(&mut it), num(&mut it), num(&mut it));
                b.key(c as u8, bit as u8, d != 0.0);
            }
            "pad" => {
                let (i, v) = (num(&mut it), num(&mut it));
                b.pad(i as u8, v as i32);
            }
            "turn" => {
                let (e, n) = (num(&mut it), num(&mut it));
                b.turn(e as u8, n as i32);
            }
            "leds" => println!("leds: {}", lit(b.leds())),
            "watch" => {
                let s = num(&mut it);
                let mut last = b.leds();
                let mut seen = 0u64;
                for _ in 0..(s * 200.0) as usize {
                    run(&mut b, 0.005);
                    let now = b.leds();
                    seen |= now ^ last;
                    last = now;
                }
                println!("changed: {} | lit at the end: {}", lit(seen), lit(last));
            }
            "frames" => {
                let (frames, at) = b.frames();
                println!("{frames} scan frames at {at:.4} s");
            }
            "peek" => {
                let at = u32::from_str_radix(it.next().expect("AT"), 16).expect("hex AT");
                let mut raw = vec![0u8; num(&mut it) as usize];
                b.peek(at, &mut raw);
                for (n, line) in raw.chunks(16).enumerate() {
                    let hex: Vec<String> = line.iter().map(|v| format!("{v:02x}")).collect();
                    println!("{:08x}: {}", at as usize + 16 * n, hex.join(" "));
                }
            }
            "screen" => {
                let at = u32::from_str_radix(it.next().expect("AT"), 16).expect("hex AT");
                let mut raw = [0u8; SCREEN_BYTES];
                if !b.screen(at, &mut raw) {
                    println!("no screen at {at:#x}");
                    continue;
                }
                // emu/panel.py: byte page + 8 * column, page 0 the bottom
                // eight rows; two rows a line of text here.
                for y in (0..64).step_by(2) {
                    let px = |x: usize, y: usize| raw[(7 - y / 8) + 8 * x] >> (y % 8) & 1 != 0;
                    let line: String = (0..128)
                        .map(|x| match (px(x, y), px(x, y + 1)) {
                            (true, true) => '█',
                            (true, false) => '▀',
                            (false, true) => '▄',
                            _ => ' ',
                        })
                        .collect();
                    println!("{}", line.trim_end());
                }
            }
            other => panic!("unknown command {other}"),
        }
    }
}
