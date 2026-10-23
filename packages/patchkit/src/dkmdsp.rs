//! The DSP section of a `.dkm`: the SHARC+ code and data of a run-time machine, linked at a nominal base and relocated by the
//! digikit runtime on the ColdFire before it sends the bytes to the DSP (docs/design/digikit-dkm.md, "DSP section").
//!
//! All integers are big endian. Layout (offsets from the start of the section):
//!
//! ```text
//! 0x00 u32 magic "DKDS"   0x04 u32 version = 3 (2: voices of 48 bytes; 1: of 16 bytes without ranges)   0x08 u32 device (1 dt2, 2 dn2)
//! 0x0c u32 image_size   bytes of text and data that are sent (a multiple of 4)
//! 0x10 u32 bss_size     bytes reserved behind the image, not sent
//! 0x14 u32 align        placement alignment in bytes (8 or more, a power of two)
//! 0x18 u32 image_off    0x1c u32 reloc_off   0x20 u32 reloc_count
//! 0x24 u32 import_off   0x28 u32 import_count   0x2c u32 voice_off   0x30 u32 voice_count
//! 0x34 .. 0x40 reserved (0)
//! imports: 32-byte NUL padded names of DSP symbols the runtime resolves (`dyndsp_info` of core/dyn-dsp)
//! relocs : 8 bytes each: u32 image byte offset of a 48-bit instruction, u32 kind << 28 | class << 24 | import index
//!            kind 0 abs24 (the instruction's 24-bit address field), kind 1 abs32 (its 32-bit immediate)
//!            class 0: the value is a short-word pc of this object (add the pc shift of the placement)
//!                  1: the value is a byte address of this object (add the byte shift)
//!                  2: the value is the addend of the import (add the import's value, a pc or a byte address as exported)
//! voices : 48 bytes each: u32 machine type, u32 image byte offset of the entry routine, 8 x u32 range of machine knob 0 .. 7 for the runtime
//!            guard of core/dsp-voice (min | max << 16; 0: undeclared), 2 x u32 reserved. Version 1 had 16 bytes (type, entry, 2 reserved)
//!          version 3: 64 bytes each: 0 u32 type, 4 u32 render entry (image byte offset), 8 u32 note_on entry (image byte offset; 0: none),
//!            12 u32 state_bytes (per voice), 16 u32 clear_bytes (zeroed at note-on), 20..51 8 x u32 ranges as above, 52 u32 voices (number of
//!            voice states to reserve behind the image and bss), 56..63 reserved 0. The packer writes version 3 only when a voice has a
//!            note_on entry or a state; otherwise version 2 (the runtime parses all three; v1 and v2 voices have note_on 0 and no state)
//! image  : the linked bytes at the nominal base `L2_BYTE_BASE`; every relocated field holds `S + A` at that base
//! ```
//!
//! A text symbol is a short-word pc (`L2_SW_BASE + (byte - L2_BYTE_BASE) / 2`), a data or bss symbol a byte address. Placed at
//! the L2 byte address `B`, the pc shift is `(B - L2_BYTE_BASE) / 2` and the byte shift `B - L2_BYTE_BASE`.

use crate::dko::Dko;
use crate::dsp::{L2_BYTE_BASE, L2_SW_BASE};
use crate::error::*;
use crate::link;
use std::collections::{BTreeMap, BTreeSet};

pub const MAGIC: u32 = 0x444b_4453; // "DKDS"
pub const VERSION: u32 = 2;
/// The version with `note_on` entries and per-voice state (voices of `VOICE_LEN_V3` bytes).
pub const VERSION_3: u32 = 3;
/// The version without voice ranges (voices of `VOICE_LEN_V1` bytes); the runtime still loads it, with every range undeclared.
pub const VERSION_1: u32 = 1;
pub const HEADER: usize = 0x40;
pub const RELOC_LEN: usize = 8;
pub const VOICE_LEN: usize = 48;
pub const VOICE_LEN_V3: usize = 64;
/// `dk_machine_desc` (SDK contract): magic "DKMA" and the contract number the packer accepts, and its size in u32 words.
pub const DESC_MAGIC: u32 = 0x444b_4d41;
pub const DESC_CONTRACT: u32 = 1;
pub const DESC_WORDS: usize = 10;
pub const VOICE_LEN_V1: usize = 16;
pub const IMPORT_LEN: usize = 32;
pub const MAX_RELOCS: usize = 4096;
pub const MAX_IMPORTS: usize = 32;
pub const MAX_VOICES: usize = 8;
/// Placement granule of the runtime's area allocator (bytes).
pub const GRANULE: u32 = 64;

pub const KIND_ABS24: u32 = 0;
pub const KIND_ABS32: u32 = 1;
pub const CLASS_TEXT: u32 = 0;
pub const CLASS_DATA: u32 = 1;
pub const CLASS_IMPORT: u32 = 2;

fn put(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_be_bytes());
}
fn get(b: &[u8], off: usize) -> u32 {
    u32::from_be_bytes(b[off..off + 4].try_into().unwrap())
}
fn align_up(x: usize, a: usize) -> usize {
    x.div_ceil(a) * a
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Voice {
    pub type_no: u32,
    /// byte offset of the entry routine in the image
    pub entry: u32,
    /// byte offset of the note_on routine in the image (0: none)
    pub note_on: u32,
    /// bytes of state per voice, bytes of it zeroed at note-on, number of voice states
    pub state_bytes: u32,
    pub clear_bytes: u32,
    pub voices: u32,
    /// range of machine knob 0 .. 7 (min | max << 16; 0 undeclared)
    pub ranges: [u32; 8],
}

impl Voice {
    /// The record needs version 3.
    fn contract(&self) -> bool {
        self.note_on != 0 || self.state_bytes != 0 || self.clear_bytes != 0 || self.voices != 0
    }
}

/// A voice as the manifest names it. `render` (and `note_on`) are global text symbols of the DSP object; `desc` is a data symbol
/// holding a `dk_machine_desc` (10 u32 words, little endian in the image) that gives voices, state and clear bytes.
#[derive(Clone, Debug)]
pub struct VoiceSpec {
    pub type_no: u32,
    pub render: String,
    pub note_on: Option<String>,
    pub desc: Option<String>,
    pub ranges: [u32; 8],
}

impl VoiceSpec {
    /// A voice with only a render entry (no note_on, no state).
    pub fn old(type_no: u32, render: String, ranges: [u32; 8]) -> VoiceSpec {
        VoiceSpec {
            type_no,
            render,
            note_on: None,
            desc: None,
            ranges,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reloc {
    pub offset: u32,
    pub kind: u32,
    pub class: u32,
    pub import: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DspSection {
    pub device: u32,
    pub image: Vec<u8>,
    pub bss: u32,
    pub align: u32,
    pub imports: Vec<String>,
    pub relocs: Vec<Reloc>,
    pub voices: Vec<Voice>,
}

/// Link `obj` (a SHARC+ object) at the nominal base and encode the section. `voices` names the entry routine of each machine type
/// (a global text symbol of the object).
pub fn pack(obj: &Dko, device: u32, voices: &[VoiceSpec]) -> Result<Vec<u8>> {
    if obj.arch != "sharc" {
        return Err(err!(
            "a DSP section holds a SHARC+ object; the object is {}",
            obj.arch
        ));
    }
    let kind_of: BTreeMap<&str, &str> = obj
        .sections
        .iter()
        .map(|s| (s.name.as_str(), s.kind.as_str()))
        .collect();
    let defined: BTreeMap<&str, (&str, bool)> = obj
        .symbols
        .iter()
        .filter_map(|s| {
            s.section
                .as_ref()
                .map(|sec| (s.name.as_str(), (kind_of[sec.as_str()], s.global)))
        })
        .collect();
    let mut imports: BTreeSet<&str> = BTreeSet::new();
    for r in &obj.relocs {
        if !defined.contains_key(r.symbol.as_str()) {
            imports.insert(r.symbol.as_str());
        }
    }
    let imports: Vec<&str> = imports.into_iter().collect();
    if imports.len() > MAX_IMPORTS {
        return Err(err!("{} DSP imports; at most {MAX_IMPORTS}", imports.len()));
    }
    for n in &imports {
        if n.len() >= IMPORT_LEN {
            return Err(err!(
                "DSP import name {n} is longer than {}",
                IMPORT_LEN - 1
            ));
        }
    }
    let externs: BTreeMap<String, u64> = imports.iter().map(|n| (n.to_string(), 0)).collect();
    let linked = link::link(obj, L2_BYTE_BASE as u64, &externs)?;
    let image = linked.image.clone();
    if image.is_empty() || image.len() % 4 != 0 {
        return Err(err!("the DSP image is {} bytes", image.len()));
    }
    let align = obj
        .sections
        .iter()
        .map(|s| s.align)
        .max()
        .unwrap_or(8)
        .max(8);
    if !align.is_power_of_two() || align > GRANULE {
        return Err(err!(
            "DSP section alignment {align}: a power of two up to {GRANULE}"
        ));
    }
    let mut relocs = vec![];
    for r in &obj.relocs {
        let sec = linked.places[&r.section];
        let off = sec - L2_BYTE_BASE as u64 + r.offset * 2;
        if kind_of.get(r.section.as_str()) != Some(&"text") || off + 6 > image.len() as u64 {
            return Err(err!(
                "DSP relocation in {} at parcel {}",
                r.section,
                r.offset
            ));
        }
        let kind = match r.rtype.as_str() {
            "sharc.abs24" => KIND_ABS24,
            "sharc.abs32" => KIND_ABS32,
            t => return Err(err!("DSP relocation type {t}")),
        };
        let (class, import) = match defined.get(r.symbol.as_str()) {
            Some(("text", _)) => (CLASS_TEXT, 0),
            Some(_) => (CLASS_DATA, 0),
            None => (
                CLASS_IMPORT,
                imports.iter().position(|n| *n == r.symbol).unwrap() as u32,
            ),
        };
        if kind == KIND_ABS24 && class == CLASS_DATA {
            return Err(err!(
                "{}: a 24-bit field cannot hold a data address of the placement",
                r.symbol
            ));
        }
        relocs.push(Reloc {
            offset: off as u32,
            kind,
            class,
            import,
        });
    }
    relocs.sort_by_key(|r| (r.offset, r.kind));
    if relocs.len() > MAX_RELOCS {
        return Err(err!(
            "{} DSP relocations; at most {MAX_RELOCS}",
            relocs.len()
        ));
    }
    if voices.len() > MAX_VOICES {
        return Err(err!("{} voices; at most {MAX_VOICES}", voices.len()));
    }
    let mut vs = vec![];
    let entry_of = |name: &String| -> Result<u32> {
        match defined.get(name.as_str()) {
            Some(("text", true)) => {}
            Some(_) => {
                return Err(err!(
                    "voice entry {name}: not a global text symbol of the DSP object"
                ));
            }
            None => {
                return Err(err!(
                    "voice entry {name}: the DSP object does not define it"
                ));
            }
        }
        let pc = linked.symbols[name.as_str()];
        Ok(((pc - L2_SW_BASE as u64) * 2) as u32)
    };
    for v in voices {
        let entry = entry_of(&v.render)?;
        let note_on = match &v.note_on {
            Some(n) => {
                let e = entry_of(n)?;
                if e == 0 {
                    return Err(err!(
                        "voice entry {n}: at image offset 0, which the section reads as no note_on"
                    ));
                }
                e
            }
            None => 0,
        };
        let (mut voices_n, mut state_bytes, mut clear_bytes) = (0, 0, 0);
        if let Some(d) = &v.desc {
            match defined.get(d.as_str()) {
                Some(("data", _)) => {}
                Some(_) => {
                    return Err(err!("voice desc {d}: not a data symbol of the DSP object"));
                }
                None => {
                    return Err(err!("voice desc {d}: the DSP object does not define it"));
                }
            }
            let at = (linked.symbols[d.as_str()] - L2_BYTE_BASE as u64) as usize;
            let bytes = image
                .get(at..at + DESC_WORDS * 4)
                .ok_or_else(|| err!("voice desc {d}: shorter than {} bytes", DESC_WORDS * 4))?;
            let w = |i: usize| u32::from_le_bytes(bytes[i * 4..i * 4 + 4].try_into().unwrap());
            if w(0) != DESC_MAGIC {
                return Err(err!(
                    "voice desc {d}: magic {:#x}, not DKMA ({DESC_MAGIC:#x})",
                    w(0)
                ));
            }
            if w(1) != DESC_CONTRACT {
                return Err(err!(
                    "voice desc {d}: contract {}, this packer takes {DESC_CONTRACT}",
                    w(1)
                ));
            }
            voices_n = w(3);
            state_bytes = w(4);
            clear_bytes = w(5);
            if clear_bytes > state_bytes {
                return Err(err!(
                    "voice desc {d}: state_clear_bytes {clear_bytes} is more than state_bytes {state_bytes}"
                ));
            }
        }
        vs.push(Voice {
            type_no: v.type_no,
            entry,
            note_on,
            state_bytes,
            clear_bytes,
            voices: voices_n,
            ranges: v.ranges,
        });
    }
    let s = DspSection {
        device,
        image,
        bss: linked.bss_size.div_ceil(4) as u32 * 4,
        align,
        imports: imports.iter().map(|s| s.to_string()).collect(),
        relocs,
        voices: vs,
    };
    Ok(s.encode())
}

impl DspSection {
    pub fn encode(&self) -> Vec<u8> {
        let import_off = HEADER;
        let reloc_off = import_off + self.imports.len() * IMPORT_LEN;
        let voice_off = reloc_off + self.relocs.len() * RELOC_LEN;
        let v3 = self.voices.iter().any(|v| v.contract());
        let vlen = if v3 { VOICE_LEN_V3 } else { VOICE_LEN };
        let image_off = align_up(voice_off + self.voices.len() * vlen, 16);
        let mut f = vec![0u8; image_off + self.image.len()];
        put(&mut f, 0, MAGIC);
        put(&mut f, 4, if v3 { VERSION_3 } else { VERSION });
        put(&mut f, 8, self.device);
        put(&mut f, 0x0c, self.image.len() as u32);
        put(&mut f, 0x10, self.bss);
        put(&mut f, 0x14, self.align);
        put(&mut f, 0x18, image_off as u32);
        put(&mut f, 0x1c, reloc_off as u32);
        put(&mut f, 0x20, self.relocs.len() as u32);
        put(&mut f, 0x24, import_off as u32);
        put(&mut f, 0x28, self.imports.len() as u32);
        put(&mut f, 0x2c, voice_off as u32);
        put(&mut f, 0x30, self.voices.len() as u32);
        for (i, n) in self.imports.iter().enumerate() {
            let at = import_off + i * IMPORT_LEN;
            f[at..at + n.len()].copy_from_slice(n.as_bytes());
        }
        for (i, r) in self.relocs.iter().enumerate() {
            let at = reloc_off + i * RELOC_LEN;
            put(&mut f, at, r.offset);
            put(&mut f, at + 4, r.kind << 28 | r.class << 24 | r.import);
        }
        for (i, v) in self.voices.iter().enumerate() {
            let at = voice_off + i * vlen;
            put(&mut f, at, v.type_no);
            put(&mut f, at + 4, v.entry);
            if v3 {
                put(&mut f, at + 8, v.note_on);
                put(&mut f, at + 12, v.state_bytes);
                put(&mut f, at + 16, v.clear_bytes);
                for (k, w) in v.ranges.iter().enumerate() {
                    put(&mut f, at + 20 + 4 * k, *w);
                }
                put(&mut f, at + 52, v.voices);
            } else {
                for (k, w) in v.ranges.iter().enumerate() {
                    put(&mut f, at + 8 + 4 * k, *w);
                }
            }
        }
        f[image_off..].copy_from_slice(&self.image);
        f
    }

    /// Bytes of the range the runtime reserves for the section (`dsp.rs`, `extent`, then the granule): image and bss rounded to 8, then
    /// `voices * state_bytes` (each rounded to 8) of every version 3 voice, rounded up to a granule. `dkneeds` computes the same from the file.
    pub fn range_len(&self) -> u32 {
        let mut n = (self.image.len() as u32 + self.bss).div_ceil(8) * 8;
        for v in &self.voices {
            n += v.voices * v.state_bytes.div_ceil(8) * 8;
        }
        n.div_ceil(GRANULE) * GRANULE
    }

    /// Parse and check a section the way the runtime does.
    pub fn parse(f: &[u8]) -> Result<DspSection> {
        if f.len() < HEADER || get(f, 0) != MAGIC {
            return Err(err!("not a DSP section (magic)"));
        }
        let version = get(f, 4);
        if version != VERSION && version != VERSION_1 && version != VERSION_3 {
            return Err(err!("DSP section version {version}"));
        }
        let vlen = match version {
            VERSION_1 => VOICE_LEN_V1,
            VERSION_3 => VOICE_LEN_V3,
            _ => VOICE_LEN,
        };
        let rng = |off: u32, n: usize| -> Result<&[u8]> {
            let off = off as usize;
            f.get(off..off.checked_add(n).ok_or_else(|| err!("overflow"))?)
                .ok_or_else(|| err!("DSP section table at {off:#x}+{n:#x} is outside the section"))
        };
        let image_size = get(f, 0x0c) as usize;
        let align = get(f, 0x14);
        if image_size == 0
            || image_size % 4 != 0
            || !align.is_power_of_two()
            || !(8..=GRANULE).contains(&align)
        {
            return Err(err!(
                "DSP section: image size {image_size}, alignment {align}"
            ));
        }
        let image = rng(get(f, 0x18), image_size)?.to_vec();
        let nrel = get(f, 0x20) as usize;
        let nimp = get(f, 0x28) as usize;
        let nvoice = get(f, 0x30) as usize;
        if nrel > MAX_RELOCS || nimp > MAX_IMPORTS || nvoice > MAX_VOICES {
            return Err(err!("DSP section: too many relocations, imports or voices"));
        }
        let imp = rng(get(f, 0x24), nimp * IMPORT_LEN)?;
        let imports = (0..nimp)
            .map(|i| {
                let e = &imp[i * IMPORT_LEN..(i + 1) * IMPORT_LEN];
                let l = e.iter().position(|b| *b == 0).unwrap_or(IMPORT_LEN);
                String::from_utf8_lossy(&e[..l]).into_owned()
            })
            .collect::<Vec<_>>();
        let rel = rng(get(f, 0x1c), nrel * RELOC_LEN)?;
        let mut relocs = vec![];
        for i in 0..nrel {
            let off = get(rel, i * RELOC_LEN);
            let w = get(rel, i * RELOC_LEN + 4);
            let r = Reloc {
                offset: off,
                kind: w >> 28,
                class: w >> 24 & 15,
                import: w & 0xffff,
            };
            if off as usize + 6 > image_size
                || r.kind > KIND_ABS32
                || r.class > CLASS_IMPORT
                || (r.class == CLASS_IMPORT && r.import as usize >= nimp)
            {
                return Err(err!("DSP relocation {i}: {off:#x} {w:#x}"));
            }
            relocs.push(r);
        }
        let vo = rng(get(f, 0x2c), nvoice * vlen)?;
        let voices = (0..nvoice)
            .map(|i| {
                let at = i * vlen;
                let v3 = version == VERSION_3;
                Voice {
                    type_no: get(vo, at),
                    entry: get(vo, at + 4),
                    note_on: if v3 { get(vo, at + 8) } else { 0 },
                    state_bytes: if v3 { get(vo, at + 12) } else { 0 },
                    clear_bytes: if v3 { get(vo, at + 16) } else { 0 },
                    voices: if v3 { get(vo, at + 52) } else { 0 },
                    ranges: match version {
                        VERSION_1 => [0; 8],
                        VERSION_3 => std::array::from_fn(|k| get(vo, at + 20 + 4 * k)),
                        _ => std::array::from_fn(|k| get(vo, at + 8 + 4 * k)),
                    },
                }
            })
            .collect::<Vec<_>>();
        if voices.iter().any(|v| {
            v.entry as usize >= image_size
                || v.entry % 2 != 0
                || v.note_on as usize >= image_size
                || v.note_on % 2 != 0
                || v.clear_bytes > v.state_bytes
        }) {
            return Err(err!("DSP voice entry outside the image"));
        }
        Ok(DspSection {
            device: get(f, 8),
            image,
            bss: get(f, 0x10),
            align,
            imports,
            relocs,
            voices,
        })
    }

    /// The reference loader: the image placed at the L2 byte address `base`, imports resolved by `resolve` (pc or byte address as
    /// the DSP symbol is exported). The runtime's Rust loader must produce the same bytes.
    pub fn relocated(&self, base: u32, resolve: &dyn Fn(&str) -> Option<u32>) -> Result<Vec<u8>> {
        if base % self.align != 0 || base < L2_BYTE_BASE {
            return Err(err!("DSP placement {base:#x}"));
        }
        let pc_shift = (base - L2_BYTE_BASE) / 2;
        let byte_shift = base - L2_BYTE_BASE;
        let mut m = self.image.clone();
        for r in &self.relocs {
            let at = r.offset as usize;
            let add = match r.class {
                CLASS_TEXT => pc_shift,
                CLASS_DATA => byte_shift,
                _ => resolve(&self.imports[r.import as usize]).ok_or_else(|| {
                    err!(
                        "DSP import {} is not provided",
                        self.imports[r.import as usize]
                    )
                })?,
            };
            let le16 = |m: &[u8], o: usize| u16::from_le_bytes([m[o], m[o + 1]]) as u32;
            if r.kind == KIND_ABS24 {
                let v = ((m[at + 2] as u32) << 16 | le16(&m, at + 4)).wrapping_add(add);
                if v >= 1 << 24 {
                    return Err(err!(
                        "DSP relocation at {at:#x}: {v:#x} does not fit 24 bits"
                    ));
                }
                m[at + 2] = (v >> 16) as u8;
                m[at + 4..at + 6].copy_from_slice(&(v as u16).to_le_bytes());
            } else {
                let v = (le16(&m, at + 2) << 16 | le16(&m, at + 4)).wrapping_add(add);
                m[at + 2..at + 4].copy_from_slice(&((v >> 16) as u16).to_le_bytes());
                m[at + 4..at + 6].copy_from_slice(&(v as u16).to_le_bytes());
            }
        }
        Ok(m)
    }

    /// The pc of voice `i` placed at `base`.
    pub fn voice_pc(&self, i: usize, base: u32) -> u32 {
        L2_SW_BASE + (base - L2_BYTE_BASE + self.voices[i].entry) / 2
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
    fn sym(name: &str, section: Option<&str>, value: u64, global: bool) -> DkoSymbol {
        DkoSymbol {
            name: name.into(),
            section: section.map(String::from),
            value,
            global,
        }
    }
    fn rel(offset: u64, t: &str, symbol: &str, addend: i64) -> DkoReloc {
        DkoReloc {
            section: "t".into(),
            offset,
            rtype: t.into(),
            symbol: symbol.into(),
            addend,
        }
    }
    /// Three instructions (CALL local, load a data address, CALL an import) and a bss.
    fn object() -> Dko {
        let text = [0u8; 24];
        Dko {
            format: crate::dko::FORMAT,
            arch: "sharc".into(),
            source_sha256: "0".repeat(64),
            toolchain: Default::default(),
            sections: vec![
                sec("t", "text", 8, &text),
                sec("d", "data", 4, &[1, 2, 3, 4]),
                DkoSection {
                    name: "b".into(),
                    kind: "bss".into(),
                    align: 4,
                    size: 64,
                    hex: String::new(),
                },
            ],
            symbols: vec![
                sym("entry", Some("t"), 0, true),
                sym("again", Some("t"), 3, false),
                sym("var", Some("d"), 0, false),
                sym("buf", Some("b"), 8, true),
                sym("helper", None, 0, true),
            ],
            relocs: vec![
                rel(0, "sharc.abs24", "again", 0),
                rel(3, "sharc.abs32", "buf", 4),
                rel(6, "sharc.abs24", "helper", 0),
                rel(9, "sharc.abs32", "var", 0),
            ],
        }
    }

    #[test]
    fn relocation_at_any_base_equals_the_linker() {
        let o = object();
        let bytes = pack(
            &o,
            1,
            &[VoiceSpec::old(
                16,
                "entry".into(),
                [0, 0x7f00_0000, 0, 0, 0, 0, 0, 0],
            )],
        )
        .unwrap();
        let d = DspSection::parse(&bytes).unwrap();
        assert_eq!(d.imports, vec!["helper"]);
        assert_eq!(d.bss, 64);
        assert_eq!(
            d.voices,
            vec![Voice {
                type_no: 16,
                entry: 0,
                note_on: 0,
                state_bytes: 0,
                clear_bytes: 0,
                voices: 0,
                ranges: [0, 0x7f00_0000, 0, 0, 0, 0, 0, 0]
            }]
        );
        for base in [0x2000_0000u32, 0x2001_ab40, 0x2003_f000] {
            let pc_of = |b: u32| L2_SW_BASE + (b - L2_BYTE_BASE) / 2;
            let ext = BTreeMap::from([("helper".to_string(), 0xb9_1234u64)]);
            let l = link::link(&o, base as u64, &ext).unwrap();
            let mem = d
                .relocated(base, &|n| (n == "helper").then_some(0xb9_1234))
                .unwrap();
            assert_eq!(mem, l.image, "base {base:#x}");
            assert_eq!(d.voice_pc(0, base), pc_of(base));
            assert_eq!(l.symbols["entry"] as u32, d.voice_pc(0, base));
        }
    }

    #[test]
    fn bad_objects_and_sections_are_refused() {
        let o = object();
        assert!(
            pack(&o, 1, &[VoiceSpec::old(16, "nope".into(), [0; 8])])
                .unwrap_err()
                .0
                .contains("does not define")
        );
        assert!(
            pack(&o, 1, &[VoiceSpec::old(16, "again".into(), [0; 8])])
                .unwrap_err()
                .0
                .contains("global text")
        );
        let mut c = object();
        c.relocs.push(rel(0, "sharc.abs24", "var", 0));
        assert!(pack(&c, 1, &[]).unwrap_err().0.contains("24-bit"));
        let mut c = object();
        c.arch = "cf".into();
        assert!(pack(&c, 1, &[]).is_err());
        let good = pack(&o, 1, &[]).unwrap();
        let mut bad = good.clone();
        bad[0] ^= 1;
        assert!(DspSection::parse(&bad).is_err());
        // a placement that leaves a 24-bit field out of range
        let d = DspSection::parse(&good).unwrap();
        assert!(d.relocated(0x2000_0004, &|_| Some(0)).is_err());
        assert!(d.relocated(0x2000_0000, &|_| None).is_err());
        assert!(d.relocated(0x2000_0000, &|_| Some(0x100_0000)).is_err());
    }

    #[test]
    fn packing_is_deterministic() {
        let o = object();
        assert_eq!(pack(&o, 1, &[]).unwrap(), pack(&o, 1, &[]).unwrap());
    }
}
