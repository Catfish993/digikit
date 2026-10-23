//! `.dkm`: a module file the digikit runtime loads from the card store at run time (docs/design/digikit-dkm.md).
//!
//! `pack` turns a ColdFire `.dko` object into the file; `Dkm::parse` reads and checks one; `Dkm::image` is the reference
//! loader (copy, relocate) that the runtime's own Rust loader (modules/core/runtime) must agree with. All integers are big
//! endian. The file holds no firmware address: absolute references are `base + offset` (kind 0) or a named import (kind 1).

use crate::dko::Dko;
use crate::error::*;
use crate::link;
use std::collections::{BTreeMap, BTreeSet};

pub const MAGIC: u32 = 0x444b_4d46; // "DKMF"
pub const NONE: u32 = 0xffff_ffff;
pub const SLOTS: u32 = 8;
pub const NAME_LEN: usize = 32;
pub const IMPORT_LEN: usize = 32;
/// Largest module file the v0 runtime stages (its staging buffer).
pub const MAX_FILE: usize = 64 * 1024;
/// The format number and header size, and the header offsets of the declared needs: `[dkm]` of sdk/sdk.toml.
pub fn layout() -> &'static crate::sdkgen::DkmLayout {
    static L: std::sync::OnceLock<crate::sdkgen::DkmLayout> = std::sync::OnceLock::new();
    L.get_or_init(crate::sdkgen::embedded_dkm)
}
/// The format number the packer writes and the loader accepts.
pub fn format() -> u32 {
    layout().format
}
/// The header size in bytes.
pub fn header() -> usize {
    layout().header as usize
}

/// The runtime version this packer targets (`dkrt_sdk::RUNTIME_VERSION`), packed major << 16 | minor << 8 | patch, from sdk/sdk.toml.
pub fn runtime_version() -> u32 {
    crate::sdkgen::embedded_runtime()
}

/// A packed runtime version as "major.minor.patch" (a plain integer below 256 from an old runtime reads as 0.0.N).
pub fn fmt_runtime(v: u32) -> String {
    format!("{}.{}.{}", v >> 16, v >> 8 & 255, v & 255)
}

/// What a load takes of each resource, declared in the header at the offsets of `[dkm.needs]` (sdk/sdk.toml). The packer computes it from
/// the file's own tables with the loader's arithmetic; a host or the DIGIKIT menu compares it with the CAPACITY figures before a load.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Needs {
    /// the arena block of image + bss (payload rounded to 16, plus the block header)
    pub arena: u32,
    /// the arena block of the relocated DSP image, held while it is sent
    pub arena_xfer: u32,
    /// DSP area bytes: image, bss and voice states, rounded to the granule
    pub dsp_area: u32,
    pub dsp_align: u32,
    pub machine_rows: u32,
    pub voice_rows: u32,
    pub param_rows: u32,
    pub page_rows: u32,
    pub fx_slots: u32,
}

/// The arena's block header (`arena.rs`).
pub const ARENA_HDR: u32 = 16;

/// The arena bytes a block of N payload bytes takes: N rounded to 16 and the header.
pub fn arena_block(n: u32) -> u32 {
    n.div_ceil(16) * 16 + ARENA_HDR
}

impl Needs {
    /// The needs of a file with an image of `image` bytes, `bss` bytes, these `register` records and this DSP section (both may be empty).
    pub fn of(image: usize, bss: u32, reg: &[u8], dsp: &[u8]) -> Result<Needs> {
        let mut n = Needs {
            arena: arena_block(image as u32 + bss),
            ..Needs::default()
        };
        (n.param_rows, n.machine_rows, n.page_rows) = record_counts(reg);
        if !dsp.is_empty() {
            let s = crate::dkmdsp::DspSection::parse(dsp)?;
            n.arena_xfer = arena_block(s.image.len() as u32);
            n.dsp_area = s.range_len();
            n.dsp_align = s.align;
            n.voice_rows = s.voices.len() as u32;
        }
        Ok(n)
    }

    /// (name in `[dkm.needs]`, value) of every field.
    pub fn fields(&self) -> [(&'static str, u32); 9] {
        [
            ("ARENA", self.arena),
            ("ARENA_XFER", self.arena_xfer),
            ("DSP_AREA", self.dsp_area),
            ("DSP_ALIGN", self.dsp_align),
            ("MACHINE_ROWS", self.machine_rows),
            ("VOICE_ROWS", self.voice_rows),
            ("PARAM_ROWS", self.param_rows),
            ("PAGE_ROWS", self.page_rows),
            ("FX_SLOTS", self.fx_slots),
        ]
    }

    fn offset(name: &str) -> usize {
        *layout()
            .needs
            .get(name)
            .unwrap_or_else(|| panic!("sdk/sdk.toml [dkm.needs] has no {name}")) as usize
    }

    /// Write the fields into a header.
    fn write(&self, f: &mut [u8]) {
        for (name, v) in self.fields() {
            put(f, Self::offset(name), v);
        }
    }

    /// Read the fields of a header (at least `header()` bytes).
    pub fn read(h: &[u8]) -> Needs {
        let g = |name| get(h, Self::offset(name));
        Needs {
            arena: g("ARENA"),
            arena_xfer: g("ARENA_XFER"),
            dsp_area: g("DSP_AREA"),
            dsp_align: g("DSP_ALIGN"),
            machine_rows: g("MACHINE_ROWS"),
            voice_rows: g("VOICE_ROWS"),
            param_rows: g("PARAM_ROWS"),
            page_rows: g("PAGE_ROWS"),
            fx_slots: g("FX_SLOTS"),
        }
    }
}

// header field offsets
pub const H_SIZE: usize = 0x08;
pub const H_HDRSIZE: usize = 0x0c;
pub const H_SHA: usize = 0x10;
pub const H_RUNTIME: usize = 0x30;
pub const H_VERSION: usize = 0x34;
/// 0x38..0x40: contract versions built against, (major, minor) of api, machine, fx, service; major 0 = not implemented.
pub const H_CONTRACT: usize = 0x38;
pub const H_NAME: usize = 0x40;
pub const H_IMAGE_OFF: usize = 0x60;
pub const H_IMAGE_SIZE: usize = 0x64;
pub const H_BSS: usize = 0x68;
pub const H_ALIGN: usize = 0x6c;
pub const H_RELOC_OFF: usize = 0x70;
pub const H_RELOC_N: usize = 0x74;
pub const H_IMPORT_OFF: usize = 0x78;
pub const H_IMPORT_N: usize = 0x7c;
pub const H_INIT: usize = 0x80;
pub const H_FINI: usize = 0x84;
pub const H_SLOT_OFF: usize = 0x88;
pub const H_SLOT_N: usize = 0x8c;
pub const H_REG_OFF: usize = 0x90;
pub const H_REG_SIZE: usize = 0x94;
pub const H_DSP_OFF: usize = 0x98;
pub const H_DSP_SIZE: usize = 0x9c;
/// The runtime version that loads a DSP section (`dkrt_sdk::RUNTIME_VERSION` is at least this).
pub const RUNTIME_DSP: u32 = 5; // DSP sections of version 2 (voices with knob ranges, finding 51); a runtime 2 to 4 loads version 1 only

pub struct Options {
    pub name: String,
    /// major << 16 | minor << 8 | patch
    pub version: u32,
    pub runtime_min: u32,
}

/// "1.2.3" -> 0x010203
pub fn parse_version(s: &str) -> Result<u32> {
    let p: Vec<&str> = s.split('.').collect();
    if p.len() != 3 {
        return Err(err!("version {s:?}: expected major.minor.patch"));
    }
    let mut v = 0u32;
    for x in p {
        let n: u32 = x.parse().map_err(|_| err!("version {s:?}: bad number"))?;
        if n > 255 {
            return Err(err!("version {s:?}: a part is over 255"));
        }
        v = v << 8 | n;
    }
    Ok(v)
}

fn put(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_be_bytes());
}
fn get(b: &[u8], off: usize) -> u32 {
    u32::from_be_bytes(b[off..off + 4].try_into().unwrap())
}
fn align_up(x: usize, a: usize) -> usize {
    x.div_ceil(a) * a
}

fn file_sha(file: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(&file[..H_SHA]);
    h.update([0u8; 32]);
    h.update(&file[H_SHA + 32..]);
    h.finalize().into()
}

fn valid_name(n: &str) -> bool {
    !n.is_empty()
        && n.len() < NAME_LEN
        && n.bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.' | b'/'))
}

/// Pack a ColdFire object. Exports by name: `dkm_init`, `dkm_fini`, `dkm_slot_<n>` (n < 8). Every other undefined symbol is
/// an import resolved by the runtime's table at load time; only 32-bit absolute relocations may name one.
pub fn pack(obj: &Dko, o: &Options) -> Result<Vec<u8>> {
    pack_with(Some(obj), &[], o)
}

/// The image of a data-only module: `dkm_init` is `moveq #0,d0; rts`. The `register` records do the work.
const STUB_IMAGE: [u8; 4] = [0x70, 0x00, 0x4e, 0x75];

/// How many (param, machine, page) records the `register` records hold. Walks the records by their kind and length words; it stops at a
/// malformed one (the loader refuses the table with E_FORMAT).
fn record_counts(reg: &[u8]) -> (u32, u32, u32) {
    let w = |at: usize| -> Option<usize> {
        let b = reg.get(at..at + 4)?;
        Some(u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as usize)
    };
    let mut n = (0, 0, 0);
    let mut at = crate::register::HEADER;
    while let (Some(kind), Some(len)) = (w(at), w(at + 4)) {
        match kind as u32 {
            crate::register::KIND_PARAM => n.0 += 1,
            crate::register::KIND_MACHINE => n.1 += 1,
            crate::register::KIND_PAGE => n.2 += 1,
            _ => {}
        }
        if len < 8 {
            break;
        }
        at += len;
    }
    n
}

/// Do the `register` records hold a machine record? (A data-only machine module has no DSP section but is a machine.)
fn has_machine_record(reg: &[u8]) -> bool {
    record_counts(reg).1 > 0
}

/// Pack an object (or none: a data-only module) with `register` records (see [`register`]).
pub fn pack_with(obj: Option<&Dko>, reg: &[u8], o: &Options) -> Result<Vec<u8>> {
    pack_full(obj, reg, &[], o)
}

/// Pack an object (or none), `register` records and a DSP section (see [`crate::dkmdsp`]; empty: none). A file with a DSP section
/// needs a runtime of version [`RUNTIME_DSP`] or later.
pub fn pack_full(obj: Option<&Dko>, reg: &[u8], dsp: &[u8], o: &Options) -> Result<Vec<u8>> {
    pack_inner(obj, reg, dsp, o, MAX_FILE, true)
}

/// [`pack_full`] with another file size limit, for files the loader must refuse: `dkm_synth` makes files over the staging buffer on purpose
/// (the capacity tests of the runtime's `E_TOO_BIG`) and DSP sections it cannot parse (`E_DSP_FORMAT`, which have no declared DSP needs).
pub fn pack_full_max(
    obj: Option<&Dko>,
    reg: &[u8],
    dsp: &[u8],
    o: &Options,
    max_file: usize,
) -> Result<Vec<u8>> {
    pack_inner(obj, reg, dsp, o, max_file, false)
}

fn pack_inner(
    obj: Option<&Dko>,
    reg: &[u8],
    dsp: &[u8],
    o: &Options,
    max_file: usize,
    strict: bool,
) -> Result<Vec<u8>> {
    if let Some(obj) = obj {
        obj.require_isa_checked()?;
    }
    let stub = Dko {
        format: crate::dko::FORMAT,
        arch: "cf".into(),
        source_sha256: "0".repeat(64),
        toolchain: Default::default(),
        sections: vec![crate::dko::DkoSection {
            name: ".text".into(),
            kind: "text".into(),
            align: 4,
            size: STUB_IMAGE.len() as u32,
            hex: hex(&STUB_IMAGE),
        }],
        symbols: vec![crate::dko::DkoSymbol {
            name: "dkm_init".into(),
            section: Some(".text".into()),
            value: 0,
            global: true,
        }],
        relocs: vec![],
    };
    let has_obj = obj.is_some();
    let obj = obj.unwrap_or(&stub);
    if obj.arch != "cf" {
        return Err(err!(
            "a .dkm holds ColdFire code; the object is {}",
            obj.arch
        ));
    }
    if !valid_name(&o.name) {
        return Err(err!(
            "module name {:?}: 1 to 31 characters of A-Z a-z 0-9 - _ . /",
            o.name
        ));
    }
    let defined: BTreeSet<&str> = obj
        .symbols
        .iter()
        .filter(|s| s.section.is_some())
        .map(|s| s.name.as_str())
        .collect();
    let mut imports: BTreeSet<&str> = BTreeSet::new();
    for r in &obj.relocs {
        if !defined.contains(r.symbol.as_str()) {
            if r.rtype != "cf.32" {
                return Err(err!(
                    "relocation {} against the import {} in {}: only cf.32 can name an import (a PC-relative reference \
                     to the firmware would depend on where the module is loaded)",
                    r.rtype,
                    r.symbol,
                    r.section
                ));
            }
            imports.insert(r.symbol.as_str());
        }
    }
    let imports: Vec<&str> = imports.into_iter().collect();
    for n in &imports {
        if n.len() >= IMPORT_LEN {
            return Err(err!("import name {n} is longer than {}", IMPORT_LEN - 1));
        }
    }
    // Link at base 0 with every import at 0: the linker leaves S + A in each word, which is exactly what the loader adds
    // the base (or the import's address) to. PC-relative references inside the module are final.
    let externs: BTreeMap<String, u64> = imports.iter().map(|n| (n.to_string(), 0)).collect();
    let linked = link::link(obj, 0, &externs)?;
    let image = linked.image.clone();
    let kind_of: BTreeMap<&str, &str> = obj
        .sections
        .iter()
        .map(|s| (s.name.as_str(), s.kind.as_str()))
        .collect();
    let mut relocs: Vec<(u32, u32)> = vec![];
    for r in &obj.relocs {
        let sec = kind_of
            .get(r.section.as_str())
            .ok_or_else(|| err!("reloc in unknown section {}", r.section))?;
        if *sec == "bss" {
            return Err(err!("relocation in bss section {}", r.section));
        }
        let off = linked.places[&r.section] + r.offset;
        if off + 4 > image.len() as u64 && r.rtype == "cf.32" {
            return Err(err!("relocation at {off:#x} is past the image"));
        }
        if let Some(i) = imports.iter().position(|n| *n == r.symbol) {
            relocs.push((off as u32, 1 << 24 | i as u32));
        } else if r.rtype == "cf.32" {
            relocs.push((off as u32, 0));
        }
    }
    relocs.sort();
    let sym = |n: &str| -> Result<Option<u32>> {
        match linked.symbols.get(n) {
            None => Ok(None),
            Some(v) if *v < image.len() as u64 => Ok(Some(*v as u32)),
            Some(v) => Err(err!("{n} = {v:#x} is outside the loaded image")),
        }
    };
    let init = sym("dkm_init")?;
    let fini = sym("dkm_fini")?;
    let mut slots: Vec<(u32, u32)> = vec![];
    for n in 0..SLOTS {
        if let Some(v) = sym(&format!("dkm_slot_{n}"))? {
            slots.push((n, v));
        }
    }
    for s in &obj.symbols {
        if let Some(n) = s.name.strip_prefix("dkm_slot_")
            && s.section.is_some()
            && n.parse::<u32>().map(|n| n >= SLOTS).unwrap_or(true)
        {
            return Err(err!("{}: slot numbers are 0 to {}", s.name, SLOTS - 1));
        }
    }
    if init.is_none() && fini.is_none() && slots.is_empty() {
        return Err(err!(
            "the object exports none of dkm_init, dkm_fini, dkm_slot_<n>"
        ));
    }
    let align = obj
        .sections
        .iter()
        .filter(|s| s.kind != "bss" || s.size > 0)
        .map(|s| s.align)
        .max()
        .unwrap_or(4)
        .clamp(4, 16);
    if obj.sections.iter().any(|s| s.align > 16) {
        return Err(err!("a section needs alignment over 16 (the arena's)"));
    }

    let import_off = header();
    let reloc_off = import_off + imports.len() * IMPORT_LEN;
    let slot_off = reloc_off + relocs.len() * 8;
    let reg_off = slot_off + slots.len() * 8;
    let reg_size = reg.len();
    let dsp_off = align_up(reg_off + reg_size, 16);
    let image_off = align_up(dsp_off + dsp.len(), 16);
    let total = image_off + image.len();
    if total > max_file {
        return Err(err!(
            "the module file is {total} bytes; the runtime stages {max_file}"
        ));
    }
    let mut f = vec![0u8; total];
    put(&mut f, 0, MAGIC);
    put(&mut f, 4, format());
    put(&mut f, H_SIZE, total as u32);
    put(&mut f, H_HDRSIZE, header() as u32);
    put(
        &mut f,
        H_RUNTIME,
        if dsp.is_empty() {
            o.runtime_min
        } else {
            o.runtime_min.max(RUNTIME_DSP)
        },
    );
    put(&mut f, H_VERSION, o.version);
    // contracts: api always; machine with a DSP section or a machine record; service with slot functions or a real `dkm_init`/`dkm_fini` (not the stub)
    let sdk = crate::sdkgen::embedded();
    let has_service = !slots.is_empty() || has_obj && (init.is_some() || fini.is_some());
    let none = (0, 0);
    let pairs = [
        sdk.api,
        if dsp.is_empty() && !has_machine_record(reg) {
            none
        } else {
            sdk.machine
        },
        none,
        if has_service { sdk.service } else { none },
    ];
    for (i, (maj, min)) in pairs.iter().enumerate() {
        f[H_CONTRACT + 2 * i] = *maj;
        f[H_CONTRACT + 2 * i + 1] = *min;
    }
    f[H_NAME..H_NAME + o.name.len()].copy_from_slice(o.name.as_bytes());
    put(&mut f, H_IMAGE_OFF, image_off as u32);
    put(&mut f, H_IMAGE_SIZE, image.len() as u32);
    put(&mut f, H_BSS, linked.bss_size as u32);
    put(&mut f, H_ALIGN, align);
    put(&mut f, H_RELOC_OFF, reloc_off as u32);
    put(&mut f, H_RELOC_N, relocs.len() as u32);
    put(&mut f, H_IMPORT_OFF, import_off as u32);
    put(&mut f, H_IMPORT_N, imports.len() as u32);
    put(&mut f, H_INIT, init.unwrap_or(NONE));
    put(&mut f, H_FINI, fini.unwrap_or(NONE));
    put(&mut f, H_SLOT_OFF, slot_off as u32);
    put(&mut f, H_SLOT_N, slots.len() as u32);
    put(&mut f, H_REG_OFF, reg_off as u32);
    put(&mut f, H_REG_SIZE, reg_size as u32);
    for (i, n) in imports.iter().enumerate() {
        let at = import_off + i * IMPORT_LEN;
        f[at..at + n.len()].copy_from_slice(n.as_bytes());
    }
    for (i, (off, k)) in relocs.iter().enumerate() {
        put(&mut f, reloc_off + i * 8, *off);
        put(&mut f, reloc_off + i * 8 + 4, *k);
    }
    for (i, (n, v)) in slots.iter().enumerate() {
        put(&mut f, slot_off + i * 8, *n);
        put(&mut f, slot_off + i * 8 + 4, *v);
    }
    f[reg_off..reg_off + reg_size].copy_from_slice(reg);
    if !dsp.is_empty() {
        put(&mut f, H_DSP_OFF, dsp_off as u32);
        put(&mut f, H_DSP_SIZE, dsp.len() as u32);
        f[dsp_off..dsp_off + dsp.len()].copy_from_slice(dsp);
    }
    f[image_off..].copy_from_slice(&image);
    let needs = Needs::of(image.len(), linked.bss_size as u32, reg, dsp);
    // a section the loader refuses (synthetic files only) declares no DSP needs
    let needs = match needs {
        Err(_) if !strict => Needs::of(image.len(), linked.bss_size as u32, reg, &[])?,
        n => n?,
    };
    needs.write(&mut f);
    let sha = file_sha(&f);
    f[H_SHA..H_SHA + 32].copy_from_slice(&sha);
    Ok(f)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dkm {
    pub name: String,
    pub version: u32,
    pub runtime_min: u32,
    pub sha256: String,
    pub image: Vec<u8>,
    pub bss: u32,
    pub align: u32,
    /// (offset in the loaded memory, 0 = base-relative or 1 + import index)
    pub relocs: Vec<(u32, Option<usize>)>,
    pub imports: Vec<String>,
    pub init: Option<u32>,
    pub fini: Option<u32>,
    pub slots: Vec<(u32, u32)>,
    pub register: Vec<u8>,
    /// The DSP section, as bytes (empty: none); parse it with [`crate::dkmdsp::DspSection::parse`].
    pub dsp: Vec<u8>,
    /// The declared needs (the header's needs block).
    pub needs: Needs,
}

impl Dkm {
    /// The size breakdown of a file of `total` bytes: `<total> bytes (image <n>, bss <n>, reloc <n>, import <n>, registry <n>, dsp <n>)`.
    /// The file is the header, the import, reloc and slot tables, the registry, the DSP section and the image, in
    /// that order, with padding to 16 before the DSP section and the image. So `total` = header + slots (8 bytes each,
    /// not listed) + padding (under 32) + image + reloc (8 bytes each) + import (32 bytes each) + registry + dsp; `bss` is
    /// memory the loader adds, not file bytes.
    pub fn size_summary(&self, total: usize) -> String {
        format!(
            "{total} bytes (image {}, bss {}, reloc {}, import {}, registry {}, dsp {})",
            self.image.len(),
            self.bss,
            self.relocs.len() * 8,
            self.imports.len() * IMPORT_LEN,
            self.register.len(),
            self.dsp.len()
        )
    }

    /// Parse and check a file the way the runtime does: magic, sizes, bounds, sha256.
    pub fn parse(f: &[u8]) -> Result<Dkm> {
        if f.len() < header() || get(f, 0) != MAGIC {
            return Err(err!("not a .dkm file (magic)"));
        }
        if get(f, 4) != format() {
            return Err(err!("format {} is not supported", get(f, 4)));
        }
        if get(f, H_SIZE) as usize != f.len() || get(f, H_HDRSIZE) as usize != header() {
            return Err(err!("size fields do not match the file"));
        }
        if file_sha(f) != f[H_SHA..H_SHA + 32] {
            return Err(err!("sha256 mismatch"));
        }
        let rng = |off: usize, n: usize| -> Result<&[u8]> {
            f.get(off..off.checked_add(n).ok_or_else(|| err!("overflow"))?)
                .ok_or_else(|| err!("table at {off:#x}+{n:#x} is outside the file"))
        };
        let name_raw = &f[H_NAME..H_NAME + NAME_LEN];
        let nl = name_raw.iter().position(|b| *b == 0).unwrap_or(NAME_LEN);
        let name =
            String::from_utf8(name_raw[..nl].to_vec()).map_err(|_| err!("name is not UTF-8"))?;
        if !valid_name(&name) {
            return Err(err!("bad module name {name:?}"));
        }
        let image_size = get(f, H_IMAGE_SIZE) as usize;
        let image = rng(get(f, H_IMAGE_OFF) as usize, image_size)?.to_vec();
        let imp_n = get(f, H_IMPORT_N) as usize;
        let imp = rng(get(f, H_IMPORT_OFF) as usize, imp_n * IMPORT_LEN)?;
        let imports = (0..imp_n)
            .map(|i| {
                let e = &imp[i * IMPORT_LEN..(i + 1) * IMPORT_LEN];
                let l = e.iter().position(|b| *b == 0).unwrap_or(IMPORT_LEN);
                String::from_utf8_lossy(&e[..l]).into_owned()
            })
            .collect::<Vec<_>>();
        let rel_n = get(f, H_RELOC_N) as usize;
        let rel = rng(get(f, H_RELOC_OFF) as usize, rel_n * 8)?;
        let mut relocs = vec![];
        for i in 0..rel_n {
            let off = get(rel, i * 8);
            let k = get(rel, i * 8 + 4);
            if off as usize + 4 > image_size {
                return Err(err!("relocation at {off:#x} is outside the image"));
            }
            relocs.push((
                off,
                match k >> 24 {
                    0 => None,
                    1 if ((k & 0xff_ffff) as usize) < imports.len() => {
                        Some((k & 0xff_ffff) as usize)
                    }
                    _ => return Err(err!("bad relocation kind {k:#x}")),
                },
            ));
        }
        let slot_n = get(f, H_SLOT_N) as usize;
        let sl = rng(get(f, H_SLOT_OFF) as usize, slot_n * 8)?;
        let slots = (0..slot_n)
            .map(|i| (get(sl, i * 8), get(sl, i * 8 + 4)))
            .collect::<Vec<_>>();
        let opt = |v: u32| (v != NONE).then_some(v);
        Ok(Dkm {
            name,
            version: get(f, H_VERSION),
            runtime_min: get(f, H_RUNTIME),
            sha256: hex(&f[H_SHA..H_SHA + 32]),
            image,
            bss: get(f, H_BSS),
            align: get(f, H_ALIGN),
            relocs,
            imports,
            init: opt(get(f, H_INIT)),
            fini: opt(get(f, H_FINI)),
            slots,
            register: rng(get(f, H_REG_OFF) as usize, get(f, H_REG_SIZE) as usize)?.to_vec(),
            dsp: rng(get(f, H_DSP_OFF) as usize, get(f, H_DSP_SIZE) as usize)?.to_vec(),
            needs: Needs::read(f),
        })
    }

    /// The reference loader: the module's memory at `base` (image, then zeroed bss), imports resolved by `resolve`.
    pub fn memory(&self, base: u32, resolve: &dyn Fn(&str) -> Option<u32>) -> Result<Vec<u8>> {
        let mut m = self.image.clone();
        m.resize(self.image.len() + self.bss as usize, 0);
        for (off, k) in &self.relocs {
            let add = match k {
                None => base,
                Some(i) => resolve(&self.imports[*i])
                    .ok_or_else(|| err!("import {} is not provided", self.imports[*i]))?,
            };
            let at = *off as usize;
            let v = u32::from_be_bytes(m[at..at + 4].try_into().unwrap()).wrapping_add(add);
            m[at..at + 4].copy_from_slice(&v.to_be_bytes());
        }
        Ok(m)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dko::*;

    fn sec(name: &str, kind: &str, align: u32, bytes: &[u8]) -> DkoSection {
        DkoSection {
            name: name.into(),
            kind: kind.into(),
            align,
            size: bytes.len() as u32,
            hex: hex(bytes),
        }
    }
    fn sym(name: &str, section: Option<&str>, value: u64) -> DkoSymbol {
        DkoSymbol {
            name: name.into(),
            section: section.map(String::from),
            value,
            global: true,
        }
    }
    fn rel(section: &str, offset: u64, t: &str, symbol: &str, addend: i64) -> DkoReloc {
        DkoReloc {
            section: section.into(),
            offset,
            rtype: t.into(),
            symbol: symbol.into(),
            addend,
        }
    }
    /// text: `jsr imp.l; jsr f.l; rts` plus a data pointer to bss and to an import with an addend. Carries the build's ISA check record.
    fn object() -> Dko {
        let mut o = unstamped();
        o.stamp_isa();
        o
    }
    fn unstamped() -> Dko {
        let text = [0x4e, 0xb9, 0, 0, 0, 0, 0x4e, 0xb9, 0, 0, 0, 0, 0x4e, 0x75];
        Dko {
            format: FORMAT_DKO,
            arch: "cf".into(),
            source_sha256: "0".repeat(64),
            toolchain: Default::default(),
            sections: vec![
                sec(".text", "text", 2, &text),
                sec(".data", "data", 4, &[0; 8]),
                DkoSection {
                    name: ".bss".into(),
                    kind: "bss".into(),
                    align: 4,
                    size: 12,
                    hex: String::new(),
                },
            ],
            symbols: vec![
                sym("dkm_init", Some(".text"), 12),
                sym("dkm_slot_0", Some(".text"), 6),
                sym("counter", Some(".bss"), 4),
                sym("fw_thing", None, 0),
            ],
            relocs: vec![
                rel(".text", 2, "cf.32", "fw_thing", 0),
                rel(".text", 8, "cf.32", "dkm_slot_0", 0),
                rel(".data", 0, "cf.32", "counter", 0),
                rel(".data", 4, "cf.32", "fw_thing", 0x10),
            ],
        }
    }
    const FORMAT_DKO: u32 = crate::dko::FORMAT;

    /// A DSP section of an 8-byte image with BSS bytes of bss and one version 3 voice with `voices` states of 20 bytes (0: version 2).
    fn dsp_section(bss: u32, voices: u32) -> Vec<u8> {
        crate::dkmdsp::DspSection {
            device: 1,
            image: vec![0; 8],
            bss,
            align: 16,
            imports: vec![],
            relocs: vec![],
            voices: vec![crate::dkmdsp::Voice {
                type_no: 17,
                entry: 0,
                note_on: 0,
                state_bytes: if voices == 0 { 0 } else { 20 },
                clear_bytes: 0,
                voices,
                ranges: [0; 8],
            }],
        }
        .encode()
    }

    fn opts() -> Options {
        Options {
            name: "test/x".into(),
            version: 0x010203,
            runtime_min: 1,
        }
    }

    #[test]
    fn pack_parse_and_relocate_equal_the_linker() {
        let o = object();
        let f = pack(&o, &opts()).unwrap();
        let d = Dkm::parse(&f).unwrap();
        assert_eq!(d.name, "test/x");
        assert_eq!(d.imports, vec!["fw_thing"]);
        assert_eq!(d.init, Some(12));
        assert_eq!(d.slots, vec![(0, 6)]);
        assert_eq!(d.bss, 12);
        // the loader at any base equals the patch-time linker at that base with the import at its address
        for base in [0x47e0_0000u32, 0x47e1_2340] {
            let mem = d
                .memory(base, &|n| (n == "fw_thing").then_some(0x4012_deda))
                .unwrap();
            let ext = BTreeMap::from([("fw_thing".to_string(), 0x4012_dedau64)]);
            let l = link::link(&o, base as u64, &ext).unwrap();
            assert_eq!(&mem[..l.image.len()], &l.image[..]);
            assert!(mem[l.image.len()..].iter().all(|b| *b == 0));
            assert_eq!(mem.len() as u64, l.image.len() as u64 + l.bss_size);
        }
    }

    #[test]
    fn corruption_and_bad_inputs_are_refused() {
        let f = pack(&object(), &opts()).unwrap();
        let mut bad = f.clone();
        let n = bad.len();
        bad[n - 1] ^= 1;
        assert!(Dkm::parse(&bad).unwrap_err().0.contains("sha256"));
        let mut bad = f.clone();
        bad[0] ^= 1;
        assert!(Dkm::parse(&bad).unwrap_err().0.contains("magic"));
        let mut o = object();
        o.relocs.push(rel(".text", 2, "cf.pc32", "fw_thing", 0));
        o.stamp_isa();
        assert!(pack(&o, &opts()).unwrap_err().0.contains("PC-relative"));
        let mut o = object();
        o.symbols.push(sym("dkm_slot_9", Some(".text"), 6));
        o.stamp_isa();
        assert!(pack(&o, &opts()).unwrap_err().0.contains("slot numbers"));
        let mut o = Options {
            name: "bad name".into(),
            ..opts()
        };
        assert!(pack(&object(), &o).is_err());
        o.name = "x".repeat(32);
        assert!(pack(&object(), &o).is_err());
    }

    #[test]
    fn the_header_carries_the_contract_versions() {
        // 0x38..0x40: api, machine, fx, service as (major, minor)
        let c = |f: &[u8]| f[H_CONTRACT..H_CONTRACT + 8].to_vec();
        // ColdFire code with slots and init, no DSP section: api and service
        let f = pack(&object(), &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 0, 0, 0, 0, 1, 0]);
        // with a DSP section: machine too
        let dsp = dsp_section(0, 0);
        let f = pack_full(Some(&object()), &[], &dsp, &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 1, 0, 0, 0, 1, 0]);
        // a data-only module with a DSP section: machine, no service
        let f = pack_full(None, &[], &dsp, &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 1, 0, 0, 0, 0, 0]);
        // a data-only module without DSP code: api only
        let f = pack_with(None, &[], &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 0, 0, 0, 0, 0, 0]);
        // register records that hold a machine: machine too (a data-only machine module); param and page records alone: not
        let rec = |kind: u32, len: usize| {
            let mut r = kind.to_be_bytes().to_vec();
            r.extend((len as u32).to_be_bytes());
            r.resize(len, 0);
            r
        };
        let table = |recs: &[Vec<u8>]| {
            let mut t = vec![0u8; crate::register::HEADER];
            for r in recs {
                t.extend(r);
            }
            t
        };
        let param = rec(crate::register::KIND_PARAM, crate::register::LEN_PARAM);
        let machine = rec(crate::register::KIND_MACHINE, crate::register::LEN_MACHINE);
        let f = pack_with(None, &table(&[param.clone(), machine]), &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 1, 0, 0, 0, 0, 0]);
        let f = pack_with(None, &table(&[param]), &opts()).unwrap();
        assert_eq!(c(&f), vec![1, 2, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn the_header_declares_the_needs() {
        // ColdFire only: the arena block of image + bss (12); no DSP, no records
        let f = pack(&object(), &opts()).unwrap();
        let d = Dkm::parse(&f).unwrap();
        assert_eq!(get(&f, 4), format());
        assert_eq!(get(&f, H_HDRSIZE) as usize, header());
        let want = Needs {
            arena: arena_block(d.image.len() as u32 + 12),
            ..Needs::default()
        };
        assert_eq!(d.needs, want);
        assert_eq!(d.needs.arena % 16, 0);
        // a DSP section: image 8, bss 100, three states of 20 (rounded to 24): 8 + 100 -> 112, + 72 = 184, granule 192; the relocated image is a block of 8
        let f = pack_full(Some(&object()), &[], &dsp_section(100, 3), &opts()).unwrap();
        let n = Dkm::parse(&f).unwrap().needs;
        assert_eq!(
            (n.dsp_area, n.dsp_align, n.voice_rows, n.arena_xfer),
            (192, 16, 1, arena_block(8))
        );
        // without state (version 2) the voice adds nothing: 8 + 100 = 108 -> 112 -> granule 128
        let f = pack_full(Some(&object()), &[], &dsp_section(100, 0), &opts()).unwrap();
        assert_eq!(Dkm::parse(&f).unwrap().needs.dsp_area, 128);
        // register records: the counts of each kind
        let rec = |kind: u32, len: usize| {
            let mut r = kind.to_be_bytes().to_vec();
            r.extend((len as u32).to_be_bytes());
            r.resize(len, 0);
            r
        };
        let mut t = vec![0u8; crate::register::HEADER];
        for r in [
            rec(crate::register::KIND_PARAM, crate::register::LEN_PARAM),
            rec(crate::register::KIND_PARAM, crate::register::LEN_PARAM),
            rec(crate::register::KIND_MACHINE, crate::register::LEN_MACHINE),
            rec(crate::register::KIND_PAGE, crate::register::LEN_PAGE),
        ] {
            t.extend(r);
        }
        let f = pack_with(None, &t, &opts()).unwrap();
        let n = Dkm::parse(&f).unwrap().needs;
        assert_eq!(
            (n.param_rows, n.machine_rows, n.page_rows, n.fx_slots),
            (2, 1, 1, 0)
        );
    }

    #[test]
    fn the_needs_layout_matches_the_sdk_fields() {
        // every field of `Needs` has an offset in [dkm.needs], none shares one, and all lie in the header behind the fixed fields
        let names: Vec<&str> = Needs::default().fields().iter().map(|f| f.0).collect();
        let mut keys: Vec<&str> = layout().needs.keys().map(|k| k.as_str()).collect();
        keys.sort();
        let mut want = names.clone();
        want.sort();
        assert_eq!(keys, want);
        // distinct values land at distinct offsets
        let n = Needs {
            arena: 1,
            arena_xfer: 2,
            dsp_area: 3,
            dsp_align: 4,
            machine_rows: 5,
            voice_rows: 6,
            param_rows: 7,
            page_rows: 8,
            fx_slots: 9,
        };
        let mut h = vec![0u8; header()];
        n.write(&mut h);
        assert_eq!(Needs::read(&h), n);
    }

    #[test]
    fn packing_is_deterministic() {
        assert_eq!(
            pack(&object(), &opts()).unwrap(),
            pack(&object(), &opts()).unwrap()
        );
    }

    #[test]
    fn an_object_that_has_not_passed_the_isa_check_is_refused() {
        // no record: made by hand, or built before the record existed
        let e = pack(&unstamped(), &opts()).unwrap_err().0;
        assert!(e.contains("no ISA check record"), "{e}");
        // tampered: a byte of the code, a relocation, a symbol, a section's alignment changed after the build
        let mut o = object();
        let mut b = unhex(&o.sections[0].hex).unwrap();
        b[13] ^= 0x04; // `rts` becomes another word
        o.sections[0].hex = hex(&b);
        assert!(pack(&o, &opts()).unwrap_err().0.contains("does not match"));
        let mut o = object();
        o.relocs[0].addend = 4;
        assert!(pack(&o, &opts()).unwrap_err().0.contains("does not match"));
        let mut o = object();
        o.symbols[1].value = 12;
        assert!(pack(&o, &opts()).unwrap_err().0.contains("does not match"));
        let mut o = object();
        o.sections[1].align = 8;
        assert!(pack(&o, &opts()).unwrap_err().0.contains("does not match"));
        // a record copied from another object does not help
        let mut o = object();
        o.toolchain.insert(ISA_CHECK_KEY.into(), "0".repeat(64));
        assert!(pack(&o, &opts()).is_err());
        // the untouched object packs
        assert!(pack(&object(), &opts()).is_ok());
    }
}
