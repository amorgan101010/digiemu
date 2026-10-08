//! Cut a recording down to what `cfrun` needs: the starting state, the
//! panel input and the audio to compare with.
//!
//!     cfstart TRACE OUT
//!
//! The result is a trace file that only `cfrun` can use (`cfreplay` has
//! nothing to follow in it). Firmware-derived, like the recording.
use cfcore::trace::Trace;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [trace, out] = args.as_slice() else {
        eprintln!("usage: cfstart TRACE OUT");
        std::process::exit(2);
    };
    let data = std::fs::read(trace).unwrap_or_else(|e| panic!("{trace}: {e}"));
    let tr = Trace::parse(&data).unwrap_or_else(|e| panic!("{trace}: {e}"));
    let small = tr.start_only(&data);
    std::fs::write(out, &small).unwrap_or_else(|e| panic!("{out}: {e}"));
    eprintln!("{out}: {} bytes from {}", small.len(), data.len());
}
