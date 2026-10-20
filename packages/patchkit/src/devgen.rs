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
//! - `sharc-adapter`: `#define ADM_*` lines for the machine adapter's SHARC+ assembly, one file per device, from `[adapter.machine]`
//!   and `[modules.frame]` of the device file (docs/design/module-sdk.md 5.2). Only those names, no other constant.
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
    limits: Option<Limits>,
    adapter: Option<AdapterDoc>,
    modules: Option<ModulesDoc>,
}
#[derive(Deserialize)]
struct AdapterDoc {
    machine: Option<AdapterMachine>,
}
/// `[adapter.machine]`: the machine adapter's device facts. The strings are checked, the numbers become `ADM_*` defines.
#[derive(Deserialize)]
struct AdapterMachine {
    voices: u64,
    voice_unit: String,
    pitch_hw: u64,
    velocity_hw: u64,
    type_source: String,
    type_hw: Option<u64>,
    record_active: Option<u64>,
    record_mark: Option<u64>,
    note_on: String,
    trig_hw: Option<u64>,
    pair: String,
    tune_slot: Option<u64>,
    param_slots: Vec<u64>,
    reserved_slots: Vec<u64>,
}
#[derive(Deserialize)]
struct ModulesDoc {
    frame: Option<FrameDoc>,
}
#[derive(Deserialize)]
struct FrameDoc {
    base: u64,
    stride: u64,
    group: Option<u64>,
    gap: Option<u64>,
}
#[derive(Deserialize)]
struct Limits {
    modules: u64,
    machine_rows: u64,
    arena_bytes: u64,
    stage_bytes: u64,
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
    limits: LimitsF,
    adapter: Option<AdapterF>,
}
#[derive(Debug)]
struct AdapterF {
    voices: u64,
    pitch_hw: u64,
    velocity_hw: u64,
    type_hw: Option<u64>,
    trig_hw: Option<u64>,
    record_active: Option<u64>,
    record_mark: Option<u64>,
    tune_slot: Option<u64>,
    param_slots: Vec<u64>,
    base: u64,
    stride: u64,
    group: u64,
    gap: u64,
    block_cycles: u64,
}
#[derive(Debug)]
struct LimitsF {
    modules: u64,
    machine_rows: u64,
    arena_bytes: u64,
    stage_bytes: u64,
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
            let (Some(b), Some(k), Some(s), Some(l)) =
                (d.board, d.digikit, d.card.and_then(|c| c.store), d.limits)
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
            let adapter = adapter_facts(
                &short,
                d.adapter.and_then(|a| a.machine),
                d.modules.and_then(|m| m.frame),
                &b,
            )?;
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
                        limits: LimitsF {
                            modules: l.modules,
                            machine_rows: l.machine_rows,
                            arena_bytes: l.arena_bytes,
                            stage_bytes: l.stage_bytes,
                        },
                        adapter,
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

/// Parameter slots a machine can have (the contract's `DK_MAX_PARAMS`).
const ADM_SLOTS: usize = 8;
/// The `ADM_PARAM_SLOT*` value of a parameter without a slot.
const ADM_NO_SLOT: i64 = 255;

/// Check `[adapter.machine]` and join it with `[modules.frame]` and the block length of `[board]`.
fn adapter_facts(
    short: &str,
    m: Option<AdapterMachine>,
    f: Option<FrameDoc>,
    b: &Board,
) -> Result<Option<AdapterF>> {
    let Some(m) = m else {
        return Ok(None);
    };
    let one_of = |what: &str, v: &str, ok: &[&str]| -> Result<()> {
        if ok.contains(&v) {
            Ok(())
        } else {
            Err(err!(
                "{short}: [adapter.machine] {what} is {v:?}, expected one of {ok:?}"
            ))
        }
    };
    one_of("voice_unit", &m.voice_unit, &["track", "slot"])?;
    one_of("type_source", &m.type_source, &["cache", "frame"])?;
    one_of("note_on", &m.note_on, &["record", "trig-edge"])?;
    one_of("pair", &m.pair, &["copy-even", "none"])?;
    let need = |what: &str, v: Option<u64>, when: &str| -> Result<()> {
        if v.is_some() {
            Ok(())
        } else {
            Err(err!("{short}: [adapter.machine] {what} is needed for {when}"))
        }
    };
    if m.note_on == "record" {
        need("record_active", m.record_active, "note_on = \"record\"")?;
        need("record_mark", m.record_mark, "note_on = \"record\"")?;
    }
    if m.note_on == "trig-edge" {
        need("trig_hw", m.trig_hw, "note_on = \"trig-edge\"")?;
    }
    if m.type_source == "frame" {
        need("type_hw", m.type_hw, "type_source = \"frame\"")?;
    }
    if m.param_slots.len() > ADM_SLOTS {
        return Err(err!(
            "{short}: [adapter.machine] param_slots lists at most {ADM_SLOTS} slots"
        ));
    }
    if m.param_slots
        .iter()
        .chain(&m.reserved_slots)
        .any(|s| *s >= ADM_SLOTS as u64)
    {
        return Err(err!(
            "{short}: [adapter.machine] param_slots and reserved_slots are knob positions 0 to {}",
            ADM_SLOTS - 1
        ));
    }
    if m.param_slots.iter().any(|s| m.reserved_slots.contains(s)) {
        return Err(err!(
            "{short}: [adapter.machine] a slot is in both param_slots and reserved_slots"
        ));
    }
    if m.voices == 0 || m.voices > 16 {
        return Err(err!("{short}: [adapter.machine] voices is 1 to 16"));
    }
    let f = f.ok_or_else(|| err!("{short}: [adapter.machine] needs [modules.frame]"))?;
    Ok(Some(AdapterF {
        voices: m.voices,
        pitch_hw: m.pitch_hw,
        velocity_hw: m.velocity_hw,
        type_hw: m.type_hw,
        trig_hw: m.trig_hw,
        record_active: m.record_active,
        record_mark: m.record_mark,
        tune_slot: m.tune_slot,
        param_slots: m.param_slots,
        base: f.base,
        stride: f.stride,
        group: f.group.unwrap_or(0),
        gap: f.gap.unwrap_or(0),
        block_cycles: (b.dsp_clock_hz + b.dsp_blocks_per_second / 2) / b.dsp_blocks_per_second,
    }))
}

const ADM_SLOT_NAMES: [&str; ADM_SLOTS] = [
    "ADM_PARAM_SLOT0",
    "ADM_PARAM_SLOT1",
    "ADM_PARAM_SLOT2",
    "ADM_PARAM_SLOT3",
    "ADM_PARAM_SLOT4",
    "ADM_PARAM_SLOT5",
    "ADM_PARAM_SLOT6",
    "ADM_PARAM_SLOT7",
];

/// The `ADM_*` constants of one device (what the machine adapter's assembly needs; names absent on a device are not written).
fn adapter_constants(a: &AdapterF) -> Vec<C> {
    let p = |name, value: u64, note| c(name, value, Scope::PerDevice, note);
    let mut v = vec![
        p("ADM_VOICES", a.voices, "[adapter.machine] voices"),
        p(
            "ADM_PITCH_HW",
            a.pitch_hw,
            "[adapter.machine] pitch_hw (frame halfword of voice 0's pitch, note << 8; voice n is + n)",
        ),
        p(
            "ADM_VELOCITY_HW",
            a.velocity_hw,
            "[adapter.machine] velocity_hw (frame halfword of voice 0's velocity, velocity << 8; voice n is + n)",
        ),
    ];
    if let Some(x) = a.type_hw {
        v.push(p(
            "ADM_TYPE_HW",
            x,
            "[adapter.machine] type_hw (frame halfword of slot 0's machine type; slot n is + n)",
        ));
    }
    if let Some(x) = a.trig_hw {
        v.push(p(
            "ADM_TRIG_HW",
            x,
            "[adapter.machine] trig_hw (frame halfword whose bit n is slot n's trig)",
        ));
    }
    if let Some(x) = a.record_active {
        v.push(p(
            "ADM_RECORD_ACTIVE",
            x,
            "[adapter.machine] record_active (byte offset in the voice record)",
        ));
    }
    if let Some(x) = a.record_mark {
        v.push(p(
            "ADM_RECORD_MARK",
            x,
            "[adapter.machine] record_mark (byte offset in the voice record)",
        ));
    }
    if let Some(x) = a.tune_slot {
        v.push(p(
            "ADM_TUNE_SLOT",
            x,
            "[adapter.machine] tune_slot (knob position of the stock tune, folded into the pitch)",
        ));
    }
    v.extend([
        p(
            "ADM_PARAM_BASE",
            a.base,
            "[modules.frame] base (halfword of knob 0 of voice 0)",
        ),
        p(
            "ADM_PARAM_STRIDE",
            a.stride,
            "[modules.frame] stride (halfwords from one voice's knobs to the next)",
        ),
        p(
            "ADM_PARAM_GROUP",
            a.group,
            "[modules.frame] group (knobs per group; 0: no groups)",
        ),
        p(
            "ADM_PARAM_GAP",
            a.gap,
            "[modules.frame] gap (halfwords between groups)",
        ),
        p(
            "ADM_NPARAM_SLOTS",
            a.param_slots.len() as u64,
            "[adapter.machine] param_slots: number of knob positions for module parameters",
        ),
    ]);
    for (i, name) in ADM_SLOT_NAMES.iter().enumerate() {
        v.push(C {
            name: *name,
            value: a.param_slots.get(i).map_or(ADM_NO_SLOT, |s| *s as i64),
            scope: Scope::PerDevice,
            note: "[adapter.machine] param_slots: knob position of module parameter i (255: none)",
        });
    }
    v.push(p(
        "ADM_BLOCK_CYCLES",
        a.block_cycles,
        "[board] dsp_clock_hz / dsp_blocks_per_second, rounded (DSP_BLOCK_CYCLES)",
    ));
    v
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
pub fn dyn_range(lib: &Library, dev: &str) -> Result<(i64, i64)> {
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
            "LIMIT_MODULES",
            e.limits.modules,
            Shared,
            "[limits] modules (module records)",
        ),
        c(
            "LIMIT_MACHINE_ROWS",
            e.limits.machine_rows,
            Shared,
            "[limits] machine_rows (run-time machine and voice rows)",
        ),
        c(
            "LIMIT_ARENA_BYTES",
            e.limits.arena_bytes,
            Shared,
            "[limits] arena_bytes (the module arena)",
        ),
        c(
            "LIMIT_STAGE_BYTES",
            e.limits.stage_bytes,
            Shared,
            "[limits] stage_bytes (the staging buffer of a .dkm file)",
        ),
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
            "sharc-adapter" => {
                if !g.path.contains("{dev}") {
                    return Err(err!(
                        "{module}: a `sharc-adapter` gen is one file per device: put {{dev}} in the path"
                    ));
                }
                for (dev, e) in &facts.docs {
                    let a = e
                        .adapter
                        .as_ref()
                        .ok_or_else(|| err!("{module}: {dev} has no [adapter.machine]"))?;
                    out.push((
                        g.path.replace("{dev}", dev),
                        sharc(module, dev, &adapter_constants(a)),
                    ));
                }
            }
            k => return Err(err!("{module}: unknown gen kind {k:?}")),
        }
    }
    Ok(out)
}
