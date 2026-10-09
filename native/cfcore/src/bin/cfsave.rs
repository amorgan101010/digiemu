//! Run the machine, save it, and say what it came to: for checking that a
//! saved machine carries on as one that was never stopped does.
//!
//!     cfsave IN --ips N [--card FILE] [--run S]... [--out FILE] [--reload] [--let-go]
//!
//! IN is a starting state or a saved machine. Each `--run` runs that many
//! more emulated seconds. `--reload` saves and restores the machine in
//! memory between the runs. `--out` writes the machine at the end.
//! `--let-go` lifts whatever is held on the panel first, as the app does.
//! The line printed is the clock, the audio played and a hash of the whole
//! saved machine, so two ways of getting to the same point can be compared.
use cfcore::machine::Machine;

fn fnv(data: &[u8]) -> u64 {
    data.iter().fold(0xcbf2_9ce4_8422_2325, |h, b| (h ^ *b as u64).wrapping_mul(0x0000_0100_0000_01b3))
}

fn main() {
    let mut path = None;
    let mut ips = 0i64;
    let mut runs = Vec::new();
    let mut out = None;
    let mut reload = false;
    let mut let_go = false;
    let mut card = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--ips" => ips = args.next().and_then(|v| v.parse().ok()).expect("--ips N"),
            "--run" => runs.push(args.next().and_then(|v| v.parse::<f64>().ok()).expect("--run S")),
            "--out" => out = args.next(),
            "--reload" => reload = true,
            "--card" => card = args.next(),
            "--let-go" => let_go = true,
            _ => path = Some(a),
        }
    }
    let mut k = Machine::open_with(&path.expect("IN"), ips, card.as_deref()).unwrap_or_else(|e| panic!("{e}"));
    if let_go {
        k.dev.borrow_mut().panel.let_go();
    }
    for (n, seconds) in runs.iter().enumerate() {
        if reload && n > 0 {
            let saved = k.save().unwrap_or_else(|e| panic!("{e}"));
            let base = k.dev.borrow().esdhc.as_ref().and_then(|e| e.card.base.clone());
            k = Machine::restore(&saved).unwrap_or_else(|e| panic!("{e}"));
            k.attach_card(base).unwrap_or_else(|e| panic!("{e}"));
            let again = k.save().unwrap_or_else(|e| panic!("{e}"));
            if again != saved {
                panic!("a restored machine does not save as it was saved");
            }
        }
        let until = k.now + (seconds * k.ips as f64) as i64;
        if let Err(e) = k.run(until) {
            eprintln!("STOPPED at {:.3} s: {e}", k.now as f64 / k.ips as f64);
            std::process::exit(1);
        }
    }
    let t0 = std::time::Instant::now();
    let saved = k.save().unwrap_or_else(|e| panic!("{e}"));
    eprintln!("copying the machine took {:.1} ms", t0.elapsed().as_secs_f64() * 1e3);
    let d = k.dev.borrow();
    println!(
        "{:.3} s, clock {}, {} steps, {} interrupts, {} bytes of audio (hash {:016x}), {} inputs left, {} keys and {} pads changing; machine {} bytes, hash {:016x}",
        k.now as f64 / k.ips as f64,
        k.now,
        k.steps,
        d.raised,
        d.audio.len(),
        fnv(&d.audio),
        k.inputs.len(),
        d.panel.changing().0,
        d.panel.changing().1,
        saved.len(),
        fnv(&saved)
    );
    if let Some(out) = out {
        let t0 = std::time::Instant::now();
        cfcore::machine::write_saved(&out, &saved).unwrap_or_else(|e| panic!("{e}"));
        eprintln!("writing it took {:.1} ms", t0.elapsed().as_secs_f64() * 1e3);
    }
}
