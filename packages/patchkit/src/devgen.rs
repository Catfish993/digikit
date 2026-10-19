//! Constants from device data, for code that cannot read a file: `patchkit gen` writes them, `gen --check` keeps them current.
//!
//! The sources are the device files (`devices/*.toml`: `[board]`, `[digikit]`, `[card.store]`) and the run-time type range of
//! `core/machines` (`types.toml`). A module asks for a file with a `[[gen]]` entry in its `module.toml` (`module::GenDef`):
//!
//! - `rust`: `pub const`s for a crate that is one image for all devices (the digikit runtime). Only constants that every device file
//!   agrees on are written; a difference is an error (the fact must then become a per-device value that the runtime reads from its
//!   device object). Written file: one, with no `{dev}` in the path.
//! - `gas-shared`: the same constants as `.equ NAME, value` lines for ColdFire assembly shared by all devices (one file).
//! - `gas`: `.equ NAME, value` lines for ColdFire assembly, one file per device (`#include "device-<dev>.inc"` in a source of
//!   the module; pk_build expands it, `.include` also works).
//! - `sharc`: `#define NAME value` lines for SHARC+ assembly, one file per device (decimal values: the assembler upper-cases
//!   macro text, which breaks `0x` literals after `+`).
//!
//! The files carry no firmware bytes, only numbers of the device files.

use crate::error::*;
use crate::module::Library;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::fmt::Write;

/// One generated file: path relative to the module directory, and its text.
pub type GenFile = (String, String);

#[derive(Deserialize)]
struct Doc {
    device: Dev,
    board: Option<Board>,
    digikit: Option<Digikit>,
    card: Option<Card>,
}
#[derive(Deserialize)]
struct Dev {
    short: String,
}
#[derive(Deserialize)]
struct Board {
    console_uart_base: u64,
    ddr_cached_base: u64,
    ddr_bytes: u64,
    ddr_uncached_alias: u64,
    esdhc_base: u64,
    dsp_clock_hz: u64,
    dsp_blocks_per_second: u64,
}
#[derive(Deserialize)]
struct Digikit {
    id: u64,
    name: String,
    safe_key: u64,
    dsp_area_bytes: u64,
    dsp_tail_off: u64,
    dsp_tail_words: u64,
    keep_base: u64,
    keep_bytes: u64,
    peek: Vec<[u64; 2]>,
}
#[derive(Deserialize)]
struct Card {
    store: Option<Store>,
}
#[derive(Deserialize)]
struct Store {
    first_sector: u64,
    end_sector: u64,
    selection_sectors: u64,
    header_b_sector: u64,
}

/// The device files that describe a digikit device, by short name.
#[derive(Debug, Default)]
pub struct Facts {
    docs: BTreeMap<String, Entry>,
}

#[derive(Debug)]
struct Entry {
    board: BoardF,
    digikit: DigikitF,
    store: StoreF,
}
#[derive(Debug)]
struct BoardF {
    console_uart_base: u64,
    ddr_cached_base: u64,
    ddr_bytes: u64,
    ddr_uncached_alias: u64,
    esdhc_base: u64,
    dsp_clock_hz: u64,
    dsp_blocks_per_second: u64,
}
#[derive(Debug)]
struct DigikitF {
    id: u64,
    name: String,
    safe_key: u64,
    dsp_area_bytes: u64,
    dsp_tail_off: u64,
    dsp_tail_words: u64,
    keep_base: u64,
    keep_bytes: u64,
    peek: Vec<[u64; 2]>,
}
#[derive(Debug)]
struct StoreF {
    first: u64,
    end: u64,
    selection_sectors: u64,
    header_b: u64,
}

impl Facts {
    pub fn from_texts<'a>(texts: impl IntoIterator<Item = &'a str>) -> Result<Facts> {
        let mut docs = BTreeMap::new();
        for t in texts {
            // a sidecar file (the LED maps) has no [device]
            let Ok(d) = toml::from_str::<Doc>(t) else {
                continue;
            };
            let short = d.device.short.clone();
            let (Some(b), Some(k), Some(s)) = (d.board, d.digikit, d.card.and_then(|c| c.store))
            else {
                continue;
            };
            if k.peek.len() > PEEK_MAX {
                return Err(err!(
                    "{short}: [digikit] peek lists at most {PEEK_MAX} ranges"
                ));
            }
            if k.peek
                .iter()
                .any(|r| r[0] >= r[1] || r[0] & 3 != 0 || r[1] & 3 != 0)
            {
                return Err(err!(
                    "{short}: [digikit] peek ranges are [lo, hi) with lo < hi, both multiples of 4"
                ));
            }
            if k.keep_base & 0xfff != 0 || k.keep_bytes == 0 || k.keep_bytes & 0xfff != 0 {
                return Err(err!(
                    "{short}: [digikit] keep_base and keep_bytes must be multiples of 4 KiB"
                ));
            }
            if k.name.len() > 3 || k.name.is_empty() || !k.name.is_ascii() {
                return Err(err!(
                    "{short}: [digikit] name must be 1 to 3 ASCII characters"
                ));
            }
            if docs
                .insert(
                    short.clone(),
                    Entry {
                        board: BoardF {
                            console_uart_base: b.console_uart_base,
                            ddr_cached_base: b.ddr_cached_base,
                            ddr_bytes: b.ddr_bytes,
                            ddr_uncached_alias: b.ddr_uncached_alias,
                            esdhc_base: b.esdhc_base,
                            dsp_clock_hz: b.dsp_clock_hz,
                            dsp_blocks_per_second: b.dsp_blocks_per_second,
                        },
                        digikit: DigikitF {
                            id: k.id,
                            name: k.name,
                            safe_key: k.safe_key,
                            dsp_area_bytes: k.dsp_area_bytes,
                            dsp_tail_off: k.dsp_tail_off,
                            dsp_tail_words: k.dsp_tail_words,
                            keep_base: k.keep_base,
                            keep_bytes: k.keep_bytes,
                            peek: k.peek,
                        },
                        store: StoreF {
                            first: s.first_sector,
                            end: s.end_sector,
                            selection_sectors: s.selection_sectors,
                            header_b: s.header_b_sector,
                        },
                    },
                )
                .is_some()
            {
                return Err(err!("two device files describe {short}"));
            }
        }
        Ok(Facts { docs })
    }

    #[cfg(not(target_arch = "wasm32"))]
    pub fn from_dir(dir: &std::path::Path) -> Result<Facts> {
        let mut texts = vec![];
        if let Ok(rd) = std::fs::read_dir(dir) {
            let mut paths: Vec<_> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
            paths.sort();
            for p in paths {
                if p.extension().is_some_and(|e| e == "toml") {
                    texts.push(
                        std::fs::read_to_string(&p).map_err(|e| err!("{}: {e}", p.display()))?,
                    );
                }
            }
        }
        Facts::from_texts(texts.iter().map(|s| s.as_str()))
    }
}

/// Ranges of RAM the PEEK command may read, at most (a device lists fewer; the rest are 0, 0).
const PEEK_MAX: usize = 4;

/// Is a constant the same on every device (it may go into a runtime that is one image for all devices)?
#[derive(Clone, Copy, PartialEq, Eq)]
enum Scope {
    Shared,
    PerDevice,
}

struct C {
    name: &'static str,
    value: i64,
    scope: Scope,
    note: &'static str,
}

fn c(name: &'static str, value: u64, scope: Scope, note: &'static str) -> C {
    C {
        name,
        value: value as i64,
        scope,
        note,
    }
}

/// The run-time machine type range of a device: `dynamic_first ..= last` of `core/machines` types.toml.
fn dyn_range(lib: &Library, dev: &str) -> Result<(i64, i64)> {
    let d = lib
        .get("core/machines")
        .and_then(|m| m.alloc.get("machines"))
        .and_then(|a| a.devices.get(dev))
        .ok_or_else(|| err!("{dev}: no allocation table (core/machines types.toml)"))?;
    let first = d
        .dynamic_first
        .ok_or_else(|| err!("{dev}: types.toml has no dynamic_first"))?;
    Ok((first, d.last))
}

/// The constants of one device.
fn constants(lib: &Library, dev: &str, e: &Entry) -> Result<Vec<C>> {
    use Scope::*;
    let (first, last) = dyn_range(lib, dev)?;
    let s = &e.store;
    if s.selection_sectors != 2 {
        return Err(err!(
            "{dev}: [card.store] selection_sectors must be 2 (the runtime writes two copies of the selection record)"
        ));
    }
    if s.header_b != s.end - s.selection_sectors - 1 {
        return Err(err!(
            "{dev}: [card.store] header_b_sector must be the sector before the selection record"
        ));
    }
    let b = &e.board;
    let k = &e.digikit;
    let n = (last - first + 1) as u64;
    let name_word = k
        .name
        .bytes()
        .chain(std::iter::repeat(0))
        .take(4)
        .fold(0u64, |a, x| (a << 8) | x as u64);
    let mut v = vec![
        c("DEVICE_ID", k.id, PerDevice, "[digikit] id"),
        c(
            "DEVICE_NAME_WORD",
            name_word,
            PerDevice,
            "[digikit] name, NUL padded, as a big-endian word",
        ),
        c("STORE_FIRST", s.first, Shared, "[card.store] first_sector"),
        c(
            "STORE_SECTORS",
            s.end - s.first,
            Shared,
            "[card.store] end_sector - first_sector",
        ),
        c(
            "STORE_HEADER_B",
            s.header_b - s.first,
            Shared,
            "[card.store] header_b_sector - first_sector (store header copy B, relative to the store)",
        ),
        c(
            "STORE_SEL_A",
            s.end - 2,
            Shared,
            "[card.store] end_sector - 2 (selection record copy A, absolute)",
        ),
        c(
            "STORE_SEL_B",
            s.end - 1,
            Shared,
            "[card.store] end_sector - 1 (selection record copy B, absolute)",
        ),
        c(
            "DYN_TYPE_FIRST",
            first as u64,
            Shared,
            "core/machines types.toml dynamic_first",
        ),
        c(
            "DYN_TYPE_LAST",
            last as u64,
            Shared,
            "core/machines types.toml last",
        ),
        c(
            "DYN_TYPES",
            n,
            Shared,
            "run-time machine types; core/dynreg reserves one machine row and core/dyndsp one voice row for each",
        ),
        c(
            "CONSOLE_UART_BASE",
            b.console_uart_base,
            Shared,
            "[board] console_uart_base",
        ),
        c(
            "DDR_CACHED_BASE",
            b.ddr_cached_base,
            Shared,
            "[board] ddr_cached_base",
        ),
        c("DDR_BYTES", b.ddr_bytes, Shared, "[board] ddr_bytes"),
        c(
            "DDR_UNCACHED_ALIAS",
            b.ddr_uncached_alias,
            Shared,
            "[board] ddr_uncached_alias",
        ),
        c("ESDHC_BASE", b.esdhc_base, Shared, "[board] esdhc_base"),
        c(
            "SAFE_KEY_CODE",
            k.safe_key,
            Shared,
            "[digikit] safe_key (a panel control code)",
        ),
        c(
            "DSP_BLOCK_CYCLES",
            (b.dsp_clock_hz + b.dsp_blocks_per_second / 2) / b.dsp_blocks_per_second,
            Shared,
            "[board] dsp_clock_hz / dsp_blocks_per_second, rounded (the clock is open, finding 41)",
        ),
        c(
            "DL_AREA_BYTES",
            k.dsp_area_bytes,
            PerDevice,
            "[digikit] dsp_area_bytes",
        ),
        c(
            "DL_AREA_WORDS",
            k.dsp_area_bytes / 4,
            PerDevice,
            "[digikit] dsp_area_bytes / 4",
        ),
        c(
            "DL_TAIL_OFF",
            k.dsp_tail_off,
            PerDevice,
            "[digikit] dsp_tail_off",
        ),
        c(
            "DL_TAIL_WORDS",
            k.dsp_tail_words,
            PerDevice,
            "[digikit] dsp_tail_words",
        ),
        c(
            "KEEP_BASE",
            k.keep_base,
            Shared,
            "[digikit] keep_base (RAM a soft reset is assumed to keep: the crash record)",
        ),
        c("KEEP_BYTES", k.keep_bytes, Shared, "[digikit] keep_bytes"),
        c(
            "PEEK_N",
            k.peek.len() as u64,
            PerDevice,
            "[digikit] peek: number of ranges",
        ),
    ];
    for i in 0..PEEK_MAX {
        let r = k.peek.get(i).copied().unwrap_or([0, 0]);
        v.push(C {
            name: PEEK_LO[i],
            value: r[0] as i64,
            scope: PerDevice,
            note: "[digikit] peek range start (RAM the PEEK command may read)",
        });
        v.push(C {
            name: PEEK_HI[i],
            value: r[1] as i64,
            scope: PerDevice,
            note: "[digikit] peek range end (exclusive)",
        });
    }
    Ok(v)
}

const PEEK_LO: [&str; PEEK_MAX] = ["PEEK_LO_0", "PEEK_LO_1", "PEEK_LO_2", "PEEK_LO_3"];
const PEEK_HI: [&str; PEEK_MAX] = ["PEEK_HI_0", "PEEK_HI_1", "PEEK_HI_2", "PEEK_HI_3"];

fn hex(v: i64) -> String {
    format!("{v:#x}")
}

const NOTE: &str =
    "generated by `patchkit gen` from the device files and core/machines types.toml; do not edit";

/// The shared constants, checked to be equal on every device.
fn shared<'a>(module: &str, per_dev: &'a [(String, Vec<C>)]) -> Result<Vec<&'a C>> {
    let (first_dev, first) = &per_dev[0];
    let mut out = vec![];
    for (i, k) in first.iter().enumerate() {
        if k.scope != Scope::Shared {
            continue;
        }
        for (dev, other) in &per_dev[1..] {
            if other[i].value != k.value {
                return Err(err!(
                    "{module}: {} is {} on {first_dev} but {} on {dev}; the runtime is one image for all devices, so the fact must become a per-device value that it reads from its device object",
                    k.name,
                    hex(k.value),
                    hex(other[i].value)
                ));
            }
        }
        out.push(k);
    }
    Ok(out)
}

fn rust(module: &str, per_dev: &[(String, Vec<C>)]) -> Result<String> {
    let mut s = format!(
        "// {module}: {NOTE}.\n// Only constants that every device file agrees on; the runtime is one image for all devices.\n\n"
    );
    for k in shared(module, per_dev)? {
        let _ = writeln!(s, "/// {}", k.note);
        let _ = writeln!(s, "pub const {}: u32 = {};", k.name, hex(k.value));
    }
    Ok(s)
}

fn gas_shared(module: &str, per_dev: &[(String, Vec<C>)]) -> Result<String> {
    let mut s = format!("/* {module}: {NOTE}. Constants that every device file agrees on. */\n");
    for k in shared(module, per_dev)? {
        let _ = writeln!(
            s,
            "    .equ {}, {}    /* {} */",
            k.name,
            hex(k.value),
            k.note
        );
    }
    Ok(s)
}

fn gas(module: &str, dev: &str, cs: &[C]) -> String {
    let mut s = format!("/* {module} on {dev}: {NOTE}. */\n");
    for k in cs {
        let _ = writeln!(
            s,
            "    .equ {}, {}    /* {} */",
            k.name,
            hex(k.value),
            k.note
        );
    }
    s
}

fn sharc(module: &str, dev: &str, cs: &[C]) -> String {
    let mut s = format!(
        "// {module} on {dev}: {NOTE}.\n// Decimal values: the assembler upper-cases macro text, which breaks `0x` literals after `+`.\n"
    );
    for k in cs {
        // the note goes on its own line: the assembler's preprocessor keeps a trailing comment in the macro body, where it would
        // swallow the rest of a line that uses the macro (`R3 = DL_TAIL_WORDS - 3;`)
        let _ = writeln!(s, "// {}\n#define {} {}", k.note, k.name, k.value);
    }
    s
}

/// The files a module asks for with `[[gen]]` (empty: it asks for none).
pub fn generate(lib: &Library, facts: &Facts, module: &str) -> Result<Vec<GenFile>> {
    let m = lib
        .get(module)
        .ok_or_else(|| err!("unknown module {module}"))?;
    if m.manifest.gens.is_empty() {
        return Ok(vec![]);
    }
    if facts.docs.is_empty() {
        return Err(err!(
            "{module}: [[gen]] needs device files with [board], [digikit] and [card.store]"
        ));
    }
    let mut per_dev: Vec<(String, Vec<C>)> = vec![];
    for (dev, e) in &facts.docs {
        per_dev.push((dev.clone(), constants(lib, dev, e)?));
    }
    let mut out = vec![];
    for g in &m.manifest.gens {
        match g.kind.as_str() {
            "rust" => {
                if g.path.contains("{dev}") {
                    return Err(err!("{module}: a `rust` gen is one file for all devices"));
                }
                out.push((g.path.clone(), rust(module, &per_dev)?));
            }
            "gas-shared" => {
                if g.path.contains("{dev}") {
                    return Err(err!(
                        "{module}: a `gas-shared` gen is one file for all devices"
                    ));
                }
                out.push((g.path.clone(), gas_shared(module, &per_dev)?));
            }
            "gas" | "sharc" => {
                if !g.path.contains("{dev}") {
                    return Err(err!(
                        "{module}: a `{}` gen is one file per device: put {{dev}} in the path",
                        g.kind
                    ));
                }
                for (dev, cs) in &per_dev {
                    let text = if g.kind == "gas" {
                        gas(module, dev, cs)
                    } else {
                        sharc(module, dev, cs)
                    };
                    out.push((g.path.replace("{dev}", dev), text));
                }
            }
            k => return Err(err!("{module}: unknown gen kind {k:?}")),
        }
    }
    Ok(out)
}
