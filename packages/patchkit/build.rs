//! Embeds the device files (`devices/*.toml`) so that the device list is data: a new device file is one more entry here after a
//! rebuild, with no change to the Rust sources. The file list is written to `$OUT_DIR/device_files.rs` (see `src/devdata.rs`).
//! The `[[firmware]]` tables are left out: patchkit does not read them, and the browser build must not carry stock-image hashes
//! (they come with the port at run time; `apps/web/tests/patchkit.test.mjs`).

use std::fmt::Write;
use std::path::PathBuf;

fn main() {
    let dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../devices");
    println!("cargo:rerun-if-changed={}", dir.display());
    let mut names: Vec<String> = std::fs::read_dir(&dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| n.ends_with(".toml"))
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    let mut s = String::from("pub const DEVICE_FILES: &[(&str, &str)] = &[\n");
    for n in &names {
        let p = dir.join(n);
        println!("cargo:rerun-if-changed={}", p.display());
        let text = std::fs::read_to_string(&p).unwrap();
        let kept = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join(format!("device-{n}"));
        std::fs::write(&kept, without_firmware(&text)).unwrap();
        let _ = writeln!(s, "    ({n:?}, include_str!({:?})),", kept.to_string_lossy());
    }
    s.push_str("];\n");
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("device_files.rs");
    std::fs::write(out, s).unwrap();
}

/// `text` without its `[[firmware]]` and `[firmware.*]` tables (from such a header to the next header of another table).
fn without_firmware(text: &str) -> String {
    let mut out = String::new();
    let mut skip = false;
    for line in text.lines() {
        let t = line.trim_start();
        if t.starts_with('[') {
            let name = t.trim_start_matches('[').trim_start();
            skip = name.starts_with("firmware]") || name.starts_with("firmware.");
        }
        if !skip {
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}
