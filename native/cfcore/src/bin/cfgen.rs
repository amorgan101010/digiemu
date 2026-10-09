//! Translate blocks of one firmware image to Rust, ahead of time.
//!
//!     cfgen --image section_3_MAIN_OS.bin [--base 0x40000400] --out FILE
//!           [--census] [--seeds FILE] [WORKLOAD...]
//!
//! Block addresses come from workload files (the blocks the reference engine
//! entered) and from seed files: one address per line, `b 4000abcd` for a
//! block to translate, `x 4000abcd` for an address that must stay with the
//! interpreter (something hooks it). The output is firmware-derived: write
//! it under out/.
use cfcore::insn::{decode, emit, Insn};
use cfcore::workload::Workload;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

/// A block longer than this is cut and falls through to a new one.
const MAX_INSNS: usize = 64;

fn num(s: &str) -> u32 {
    match s.strip_prefix("0x") {
        Some(hex) => u32::from_str_radix(hex, 16),
        None => s.parse(),
    }
    .unwrap_or_else(|_| panic!("bad number {s}"))
}

fn main() {
    let mut image = None;
    let mut out = None;
    let mut base = 0x4000_0400u32;
    let mut census = false;
    let mut seeds: BTreeSet<u32> = BTreeSet::new();
    let mut skip: BTreeSet<u32> = BTreeSet::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--image" => image = args.next(),
            "--out" => out = args.next(),
            "--base" => base = num(&args.next().expect("--base ADDR")),
            "--census" => census = true,
            "--seeds" => {
                let path = args.next().expect("--seeds FILE");
                let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
                for line in text.lines() {
                    let mut parts = line.split_whitespace();
                    let (Some(kind), Some(addr)) = (parts.next(), parts.next()) else { continue };
                    let addr = u32::from_str_radix(addr, 16).expect("seed address");
                    seeds.insert(addr);
                    if kind == "x" {
                        skip.insert(addr);
                    }
                }
            }
            path => {
                let w = Workload::read(path).unwrap_or_else(|e| panic!("{e}"));
                seeds.extend(w.blocks.iter().copied());
                seeds.insert(w.entry);
                seeds.insert(w.stop);
            }
        }
    }
    let image = std::fs::read(image.expect("--image FILE")).expect("image");
    let end = base + image.len() as u32;
    let mut word = |a: u32| -> u16 {
        let i = a.wrapping_sub(base) as usize;
        match image.get(i..i + 2) {
            Some(b) => u16::from_be_bytes([b[0], b[1]]),
            None => 0x4afc,
        }
    };
    // Translation resumes right after an instruction the interpreter keeps.
    for &addr in &skip {
        if addr >= base && addr < end {
            let (_, len) = decode(&mut word, addr);
            seeds.insert(addr + len);
        }
    }

    let mut blocks: BTreeMap<u32, (Vec<Insn>, u32)> = BTreeMap::new();
    let mut unknown: BTreeMap<u32, (u32, u32)> = BTreeMap::new();
    let mut insns = 0usize;
    let mut work: Vec<u32> = seeds.iter().copied().collect();
    while let Some(start) = work.pop() {
        if start < base || start >= end || skip.contains(&start) || blocks.contains_key(&start) {
            continue;
        }
        let mut pc = start;
        let mut body = Vec::new();
        let mut ok = true;
        loop {
            let (insn, len) = decode(&mut word, pc);
            if let Insn::Trap(..) = insn {
                // Left to the interpreter: a caller may want to see each one.
                ok = false;
                break;
            }
            if let Insn::Unsupported(op, at) = insn {
                let e = unknown.entry(op).or_insert((0, at));
                e.0 += 1;
                ok = false;
                break;
            }
            body.push(insn);
            pc += len;
            if insn.is_flow() || seeds.contains(&pc) {
                break;
            }
            if body.len() >= MAX_INSNS {
                seeds.insert(pc);
                work.push(pc);
                break;
            }
        }
        if ok {
            insns += body.len();
            blocks.insert(start, (body, pc));
        }
    }
    eprintln!(
        "{} seeds ({} kept for the interpreter), {} blocks translated ({} instructions), {} unknown opcodes",
        seeds.len(),
        skip.len(),
        blocks.len(),
        insns,
        unknown.len()
    );
    for (op, (n, at)) in &unknown {
        eprintln!("  unknown {op:#06x} in {n} blocks, first at {at:#010x}");
    }
    if census {
        return;
    }

    // `budget` counts instructions. A block runs only if all of it fits, so
    // the count is exact; the caller interprets the remainder. Blocks are
    // numbered, and a block that knows its successor names it by number, so
    // the compiler can turn the loop's `match` into jumps between blocks.
    // Only returns and computed jumps look their target up.
    let index: BTreeMap<u32, usize> = blocks.keys().enumerate().map(|(i, a)| (*a, i)).collect();
    let none = blocks.len();
    let mut s = String::new();
    s.push_str("// Generated by cfgen from a firmware image: firmware-derived, never commit.\n");
    writeln!(s, "pub const BLOCKS: usize = {none};").unwrap();
    // The image bytes the blocks were made from, for the machine to check
    // its code against: the spans, and an FNV-1a hash of what is in them.
    let mut spans: Vec<(u32, u32)> = Vec::new();
    for (start, (_, next)) in &blocks {
        match spans.last_mut() {
            Some((a, n)) if *start <= *a + *n => *n = (*n).max(next - *a),
            _ => spans.push((*start, next - start)),
        }
    }
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for (a, n) in &spans {
        let at = (a - base) as usize;
        for b in &image[at..(at + *n as usize).min(image.len())] {
            hash = (hash ^ *b as u64).wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    writeln!(s, "pub const CODE_HASH: u64 = {hash:#x};").unwrap();
    writeln!(s, "pub static SPANS: [(u32, u32); {}] = [", spans.len()).unwrap();
    for chunk in spans.chunks(8) {
        let row: Vec<String> = chunk.iter().map(|(a, n)| format!("({a:#x}, {n})")).collect();
        writeln!(s, "    {},", row.join(", ")).unwrap();
    }
    s.push_str("];\n");
    // An open-addressed table from block address to block number.
    let bits = (2 * none.max(1)).next_power_of_two().trailing_zeros().max(4);
    let size = 1usize << bits;
    let mut table = vec![(0u32, none); size];
    for (addr, i) in &index {
        let mut h = (addr.wrapping_mul(0x9e37_79b1) >> (32 - bits)) as usize;
        while table[h].1 != none {
            h = (h + 1) & (size - 1);
        }
        table[h] = (*addr, *i);
    }
    writeln!(s, "static TABLE: [(u32, u16); {size}] = [").unwrap();
    for chunk in table.chunks(8) {
        let row: Vec<String> = chunk.iter().map(|(a, i)| format!("({a:#x}, {i})")).collect();
        writeln!(s, "    {},", row.join(", ")).unwrap();
    }
    s.push_str("];\n");
    writeln!(
        s,
        "#[inline(always)]\nfn index(pc: u32) -> usize {{\n    let mut h = (pc.wrapping_mul(0x9e37_79b1) >> {}) as usize;\n    loop {{\n        let (a, i) = TABLE[h];\n        if a == pc || i as usize == {none} {{\n            return i as usize;\n        }}\n        h = (h + 1) & {};\n    }}\n}}",
        32 - bits,
        size - 1
    )
    .unwrap();
    writeln!(s, "pub fn has(pc: u32) -> bool {{\n    index(pc) != {none}\n}}").unwrap();
    s.push_str("pub fn run(cpu: &mut Cpu, m: &mut Mem, budget: &mut i64) {\n");
    s.push_str("    let mut s = *cpu;\n    let c = &mut s;\n    let mut left = *budget;\n");
    s.push_str("    let mut i = index(c.pc);\n    'run: loop {\n        match i {\n");
    let goto = |target: u32| match index.get(&target) {
        Some(i) => format!("i = {i};"),
        None => format!("c.pc = {target:#x}; break 'run;"),
    };
    for (start, (body, next)) in &blocks {
        let n = body.len();
        let (last, rest) = body.split_last().unwrap();
        writeln!(s, "            {} => {{", index[start]).unwrap();
        writeln!(s, "                if left < {n} {{ c.pc = {start:#x}; break 'run; }}").unwrap();
        writeln!(s, "                left -= {n};").unwrap();
        for insn in rest {
            writeln!(s, "                {}", emit(insn)).unwrap();
        }
        match last {
            Insn::Bcc(cc, target, fall) => writeln!(
                s,
                "                if ops::cond(c, {cc}) {{ {} }} else {{ {} }}",
                goto(*target),
                goto(*fall)
            )
            .unwrap(),
            Insn::Bra(target) | Insn::Jmp(cfcore::insn::Ea::Abs(target)) => {
                writeln!(s, "                {}", goto(*target)).unwrap()
            }
            Insn::Bsr(target, _) | Insn::Jsr(cfcore::insn::Ea::Abs(target), _) => {
                writeln!(s, "                {}\n                {}", emit(last), goto(*target)).unwrap()
            }
            other if other.is_flow() => {
                writeln!(s, "                {}\n                i = index(c.pc);", emit(other)).unwrap()
            }
            other => {
                writeln!(s, "                {}\n                {}", emit(other), goto(*next)).unwrap()
            }
        }
        s.push_str("            }\n");
    }
    s.push_str("            _ => break,\n        }\n    }\n    *cpu = s;\n    *budget = left;\n}\n");
    std::fs::write(out.expect("--out FILE"), s).expect("write output");
}
