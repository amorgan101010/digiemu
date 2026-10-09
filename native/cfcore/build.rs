//! The translated blocks are firmware-derived, so they are never in the
//! tree: `cfgen` writes them (to the ignored out/), and CFCORE_AOT names
//! that file for this build. Without it the crate builds with no blocks.
use std::{env, fs, path::PathBuf};

const STUB: &str = "pub const BLOCKS: usize = 0;\n\
pub const CODE_HASH: u64 = 0;\n\
pub static SPANS: [(u32, u32); 0] = [];\n\
pub fn has(_pc: u32) -> bool { false }\n\
pub fn run(_cpu: &mut Cpu, _m: &mut Mem, _budget: &mut i64) {}\n";

fn main() {
    println!("cargo:rerun-if-env-changed=CFCORE_AOT");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("aot_gen.rs");
    match env::var("CFCORE_AOT") {
        Ok(path) if !path.is_empty() => {
            println!("cargo:rerun-if-changed={path}");
            fs::copy(&path, &out).expect("CFCORE_AOT file");
        }
        _ => fs::write(&out, STUB).unwrap(),
    }
}
