//! Check and time this core on captured workloads.
//!
//!     cfbench [--reps N] WORKLOAD...
//!
//! Each workload is run by the interpreter and by the translated blocks
//! (with the interpreter for anything not translated), each result is
//! compared with the reference engine's, and each is timed. One JSON line
//! per workload on stdout.
use cfcore::interp::Interp;
use cfcore::workload::Workload;
use cfcore::{aot, run_mixed, Cpu, Mem};
use std::time::Instant;

fn stats(mut ns: Vec<u64>) -> (u64, u64) {
    ns.sort_unstable();
    (ns[0], ns[ns.len() / 2])
}

fn main() {
    let mut reps = 200usize;
    let mut paths = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--reps" {
            reps = args.next().and_then(|v| v.parse().ok()).expect("--reps N");
        } else {
            paths.push(a);
        }
    }
    eprintln!("{} translated blocks in this build", aot::BLOCKS);
    for path in &paths {
        let w = Workload::read(path).unwrap_or_else(|e| panic!("{e}"));
        let name = path.rsplit('/').next().unwrap();
        let mut c = Cpu::default();
        let mut m = Mem::new();

        let mut it = Interp::new();
        w.load(&mut c, &mut m);
        it.run(&mut c, &mut m, w.stop, 200_000_000);
        let insns = it.executed;
        let bad_interp = w.check(&c, &mut m);

        let mut it = Interp::new();
        w.load(&mut c, &mut m);
        run_mixed(&mut c, &mut m, &mut it, insns);
        let missed = it.executed;
        let bad_aot = w.check(&c, &mut m);

        let mut t_aot = Vec::with_capacity(reps);
        let mut t_int = Vec::with_capacity(reps);
        let mut it_aot = Interp::new();
        let mut it_int = Interp::new();
        for _ in 0..reps {
            w.load(&mut c, &mut m);
            let t = Instant::now();
            run_mixed(&mut c, &mut m, &mut it_aot, insns);
            t_aot.push(t.elapsed().as_nanos() as u64);
            w.load(&mut c, &mut m);
            let t = Instant::now();
            it_int.run(&mut c, &mut m, w.stop, 200_000_000);
            t_int.push(t.elapsed().as_nanos() as u64);
        }
        let (aot_min, aot_med) = stats(t_aot);
        let (int_min, int_med) = stats(t_int);
        println!(
            "{{\"name\":\"{name}\",\"insns\":{insns},\"ref_blocks\":{},\
\"interp_insns_in_mixed\":{missed},\"interp_ok\":{},\"aot_ok\":{},\
\"aot_ns_min\":{aot_min},\"aot_ns_med\":{aot_med},\"interp_ns_min\":{int_min},\"interp_ns_med\":{int_med}}}",
            w.block_entries,
            bad_interp.is_empty(),
            bad_aot.is_empty(),
        );
        for b in bad_interp.iter().take(12) {
            eprintln!("{name} interp: {b}");
        }
        for b in bad_aot.iter().take(12) {
            eprintln!("{name} aot: {b}");
        }
    }
}
