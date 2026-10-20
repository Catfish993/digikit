//! The module contract: the files `patchkit gen` writes from `sdk/sdk.toml` are current (a stale one fails here as it fails
//! `patchkit gen --check`).

use patchkit::sdkgen;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// The text of a C number: decimal or hex, an optional `u` suffix.
fn c_number(s: &str) -> Option<u32> {
    let s = s.trim().trim_end_matches('u');
    match s.strip_prefix("0x") {
        Some(h) => u32::from_str_radix(h, 16).ok(),
        None => s.parse().ok(),
    }
}

/// The `#define NAME value` and `NAME = value,` lines of the header.
fn header_facts(h: &str) -> (Vec<(String, String)>, Vec<(String, u32)>) {
    let mut defs = vec![];
    let mut enums = vec![];
    for line in h.lines() {
        let code = line.split("/*").next().unwrap().trim();
        if let Some(d) = code.strip_prefix("#define ") {
            let mut p = d.split_whitespace();
            if let (Some(n), Some(v)) = (p.next(), p.next()) {
                defs.push((n.to_string(), v.to_string()));
            }
        } else if let Some((n, v)) = code.trim_end_matches(',').split_once(" = ") {
            if let Some(v) = c_number(v) {
                enums.push((n.trim().to_string(), v));
            }
        }
    }
    (defs, enums)
}

/// The field names of the struct `typedef struct NAME { ... }` in order.
fn struct_fields(h: &str, name: &str) -> Vec<String> {
    let start = format!("typedef struct {name} {{");
    let mut inside = false;
    let mut out = vec![];
    for line in h.lines() {
        let code = line.split("/*").next().unwrap().trim();
        if code.starts_with(&start) {
            inside = true;
        } else if inside && code.starts_with('}') {
            break;
        } else if inside && code.ends_with(';') {
            let f = code
                .trim_end_matches(';')
                .rsplit(|c: char| !(c.is_alphanumeric() || c == '_'))
                .next()
                .unwrap();
            out.push(f.to_string());
        }
    }
    out
}

#[test]
fn the_machine_header_matches_sdk_toml() {
    let toml = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let m = sdkgen::machine(&toml).unwrap();
    let h = std::fs::read_to_string(root().join("sdk/dk_machine.h")).unwrap();
    let (defs, enums) = header_facts(&h);
    for c in &m.consts {
        let from_header = match c.name.as_str() {
            "DK_LEVEL_BOUND_BITS" => {
                let d = defs.iter().find(|(n, _)| n == "DK_LEVEL_BOUND").unwrap();
                let f: f32 = d.1.trim_end_matches('f').parse().unwrap();
                Some(f.to_bits())
            }
            n => defs
                .iter()
                .find(|(d, _)| d == n)
                .and_then(|(_, v)| c_number(v))
                .or_else(|| enums.iter().find(|(e, _)| e == n).map(|(_, v)| *v)),
        };
        assert_eq!(from_header, Some(c.value), "{} in sdk/dk_machine.h", c.name);
    }
    for (name, fields) in [("dk_voice_in", &m.voice_in), ("dk_machine_desc", &m.desc)] {
        assert_eq!(
            &struct_fields(&h, name),
            fields,
            "fields of {name} in sdk/dk_machine.h"
        );
    }
}

#[test]
fn the_contract_files_are_current() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let files = sdkgen::render(&text).unwrap();
    assert_eq!(files.len(), 5);
    for (rel, text) in files {
        let path = root().join(&rel);
        let have = std::fs::read_to_string(&path).unwrap_or_default();
        assert_eq!(
            have,
            text,
            "{} is stale: run `patchkit gen`",
            path.display()
        );
    }
}
