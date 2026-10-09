//! Cut a recording down to what `cfrun` needs: the starting state, the
//! panel input and the audio to compare with.
//!
//!     cfstart TRACE OUT [--card IMAGE --sd-flag HEX [--card-file CARD]]
//!
//! `--card` gives the machine its +Drive: IMAGE is the raw card image the
//! recording's emulator ran against (its `plusdrive.img`), of which only
//! the sectors that hold anything are kept. `--sd-flag` is the address of
//! the card driver's "storage is up" flag, `sd_flag` in `emu/symbols.py`;
//! the card's size is the word the driver keeps 0x24 after it. With
//! `--card-file` the sectors go to CARD, a file of their own that the
//! machine is opened with, and OUT only names it: for a card with samples
//! on it, which is too big to keep in memory and in every saved machine.
//!
//! The result is a trace file that only `cfrun` can use (`cfreplay` has
//! nothing to follow in it). Firmware-derived, like the recording.
use cfcore::esdhc::{Base, Card, Esdhc};
use cfcore::trace::Trace;

fn main() {
    let mut names = Vec::new();
    let mut card = None;
    let mut flag = None;
    let mut card_file = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--card" => card = args.next(),
            "--card-file" => card_file = args.next(),
            "--sd-flag" => flag = args.next().and_then(|v| u32::from_str_radix(v.trim_start_matches("0x"), 16).ok()),
            _ => names.push(a),
        }
    }
    let [trace, out] = names.as_slice() else {
        eprintln!("usage: cfstart TRACE OUT [--card IMAGE --sd-flag HEX [--card-file CARD]]");
        std::process::exit(2);
    };
    let data = std::fs::read(trace).unwrap_or_else(|e| panic!("{trace}: {e}"));
    let mut tr = Trace::parse(&data).unwrap_or_else(|e| panic!("{trace}: {e}"));
    if let Some(image) = card {
        let flag = flag.expect("--card needs --sd-flag HEX");
        if tr.word_at(flag) != Some(1) {
            panic!("the card driver's flag at {flag:#x} is not 1: storage is not up in this state");
        }
        let blocks = tr.word_at(flag + 0x24).expect("the card's size");
        let raw = std::fs::read(&image).unwrap_or_else(|e| panic!("{image}: {e}"));
        let card = match card_file {
            Some(file) => {
                let base_id = Base::pack(&raw, blocks, &file).unwrap_or_else(|e| panic!("{e}"));
                let held = Base::open(&file).unwrap_or_else(|e| panic!("{e}")).sectors();
                eprintln!("{file}: a card of {blocks} sectors, {held} of them holding something, id {base_id:#x}");
                Card { blocks, base_id, ..Default::default() }
            }
            None => {
                let card = Card::from_image(blocks, &raw);
                eprintln!("{image}: a card of {blocks} sectors, {} of them holding something", card.sectors.len());
                card
            }
        };
        tr.card = Some(Esdhc::for_driver(flag, card));
    }
    let small = tr.start_only(&data);
    std::fs::write(out, &small).unwrap_or_else(|e| panic!("{out}: {e}"));
    eprintln!("{out}: {} bytes from {}", small.len(), data.len());
}
