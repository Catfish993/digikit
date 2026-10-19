//! The linker: lays an object's sections out contiguously from a base address, resolves symbols and
//! applies relocations. ColdFire relocations are R_68K_32 / PC32 / PC16 / PC8; SHARC+ relocations write
//! the low 24 or 32 bits of a 48-bit instruction.

use crate::dko::*;
use crate::dsp;
use crate::error::*;
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub struct Linked {
    pub cpu: String,
    pub base: u64,
    /// Bytes from `base` (text and data; the bss is not included).
    pub image: Vec<u8>,
    /// bss that follows `image`: reserved, zeroed by the ColdFire loader, not loaded by the SHARC boot stream.
    pub bss_size: u64,
    /// Natural-unit address per defined symbol (see dko.rs).
    pub symbols: BTreeMap<String, u64>,
    pub globals: Vec<String>,
    /// Byte ranges `[lo, hi)` of the executable sections, for disassembly.
    pub text_ranges: Vec<(u64, u64)>,
    /// Natural-unit address of every section (bytes for ColdFire), by section name.
    pub places: BTreeMap<String, u64>,
}

fn align_up(x: u64, a: u64) -> u64 {
    x.div_ceil(a) * a
}

const KIND_ORDER: [&str; 3] = ["text", "data", "bss"];

fn ordered(obj: &Dko) -> Vec<&DkoSection> {
    let mut v: Vec<&DkoSection> = obj.sections.iter().collect();
    v.sort_by_key(|s| KIND_ORDER.iter().position(|k| *k == s.kind).unwrap_or(9));
    v
}

/// Total bytes the object occupies from an aligned base (alignment of the base itself excluded) and the
/// strictest alignment it needs. Used by the allocator before the base is known.
pub fn footprint(obj: &Dko) -> (u64, u64) {
    let sharc = obj.arch == "sharc";
    let mut at = 0u64;
    let mut max_align = if sharc { 8 } else { 4 };
    for s in ordered(obj) {
        let al = section_align(obj, s);
        max_align = max_align.max(al);
        at = align_up(at, al) + s.size as u64;
    }
    at = align_up(at, 4);
    (at, max_align)
}

fn section_align(obj: &Dko, s: &DkoSection) -> u64 {
    let a = s.align as u64;
    if obj.arch == "sharc" {
        a.max(if s.kind == "text" { 8 } else { 4 })
    } else if s.kind == "bss" {
        // the loader zeroes bss right behind the copied payload, in words
        a.max(4)
    } else {
        a
    }
}

/// Symbols only (addresses in natural units), without relocating: lets every object see every export.
pub fn define(obj: &Dko, base: u64) -> Result<Linked> {
    link_impl(obj, base, &BTreeMap::new(), false)
}

/// ColdFire: bytes of the object that must be copied (text + data); the bss comes after.
pub fn link(obj: &Dko, base: u64, externs: &BTreeMap<String, u64>) -> Result<Linked> {
    link_impl(obj, base, externs, true)
}

fn link_impl(
    obj: &Dko,
    base: u64,
    externs: &BTreeMap<String, u64>,
    relocate: bool,
) -> Result<Linked> {
    let sharc = obj.arch == "sharc";
    let (_, max_align) = footprint(obj);
    if !base.is_multiple_of(max_align) {
        return Err(err!("object base {base:#x} is not aligned to {max_align}"));
    }
    let mut place: BTreeMap<&str, u64> = BTreeMap::new();
    let mut image: Vec<u8> = Vec::new();
    let mut at = base;
    let mut bss_start: Option<u64> = None;
    let mut bss_size = 0u64;
    let mut text_ranges = vec![];
    for s in ordered(obj) {
        at = align_up(at, section_align(obj, s));
        place.insert(&s.name, at);
        if s.kind == "text" {
            text_ranges.push((at, at + s.size as u64));
        }
        if s.kind == "bss" {
            // reserved memory behind the payload: ColdFire's loader zeroes it, a SHARC boot stream does not load it
            // (the program must write it before it reads it)
            if bss_start.is_none() {
                // the payload ends where the bss begins (padding included)
                image.resize((at - base) as usize, 0);
            }
            bss_start.get_or_insert(at);
            bss_size = at + s.size as u64 - bss_start.unwrap();
        } else {
            image.resize((at - base) as usize, 0);
            image.extend_from_slice(&crate::error::unhex(&s.hex)?);
        }
        at += s.size as u64;
    }
    if sharc {
        image.resize(align_up(image.len() as u64, 4) as usize, 0);
    }
    // Symbol table in natural units.
    let kind_of: BTreeMap<&str, &str> = obj
        .sections
        .iter()
        .map(|s| (s.name.as_str(), s.kind.as_str()))
        .collect();
    let mut symbols: BTreeMap<String, u64> = BTreeMap::new();
    let mut globals = vec![];
    for sy in &obj.symbols {
        if let Some(sec) = &sy.section {
            let b = *place
                .get(sec.as_str())
                .ok_or_else(|| err!("symbol {}: unknown section {sec}", sy.name))?;
            let v = if sharc && kind_of[sec.as_str()] == "text" {
                dsp::l2_byte_to_pc(b as u32) as u64 + sy.value
            } else {
                b + sy.value
            };
            symbols.insert(sy.name.clone(), v);
            if sy.global {
                globals.push(sy.name.clone());
            }
        }
    }
    let resolve = |name: &str| -> Result<u64> {
        symbols
            .get(name)
            .or_else(|| externs.get(name))
            .copied()
            .ok_or_else(|| err!("unresolved symbol {name}"))
    };
    for r in obj.relocs.iter().filter(|_| relocate) {
        let sec = obj
            .sections
            .iter()
            .find(|s| s.name == r.section)
            .ok_or_else(|| err!("reloc in unknown section {}", r.section))?;
        let secbase = place[r.section.as_str()];
        let s_val = resolve(&r.symbol)? as i64 + r.addend;
        if sharc {
            if sec.kind != "text" {
                return Err(err!("relocation in non-text SHARC section {}", r.section));
            }
            let at = ((secbase - base) + r.offset * 2) as usize;
            if at + 6 > image.len() {
                return Err(err!(
                    "relocation at parcel {} runs past the section",
                    r.offset
                ));
            }
            match r.rtype.as_str() {
                "sharc.abs24" => {
                    if !(0..1 << 24).contains(&s_val) {
                        return Err(err!(
                            "{}: {s_val:#x} does not fit a 24-bit target",
                            r.symbol
                        ));
                    }
                    image[at + 2] = ((s_val >> 16) & 0xff) as u8;
                    image[at + 4..at + 6].copy_from_slice(&((s_val & 0xffff) as u16).to_le_bytes());
                }
                "sharc.abs32" => {
                    let v = s_val as u64 & 0xffff_ffff;
                    image[at + 2..at + 4].copy_from_slice(&((v >> 16) as u16).to_le_bytes());
                    image[at + 4..at + 6].copy_from_slice(&((v & 0xffff) as u16).to_le_bytes());
                }
                t => return Err(err!("relocation type {t} is not a SHARC relocation")),
            }
        } else {
            let place_addr = secbase + r.offset;
            let (size, pcrel) = match r.rtype.as_str() {
                "cf.32" => (4usize, false),
                "cf.pc32" => (4, true),
                "cf.pc16" => (2, true),
                "cf.pc8" => (1, true),
                t => return Err(err!("relocation type {t} is not a ColdFire relocation")),
            };
            let mut v = s_val;
            let (lo, hi) = if pcrel {
                v -= place_addr as i64;
                (-(1i64 << (8 * size - 1)), 1i64 << (8 * size - 1))
            } else {
                (-(1i64 << (8 * size - 1)), 1i64 << (8 * size))
            };
            if !(lo..hi).contains(&v) {
                return Err(err!(
                    "relocation {} at {place_addr:#x} -> {}: {v:#x} out of range",
                    r.rtype,
                    r.symbol
                ));
            }
            let off = (place_addr - base) as usize;
            if off + size > image.len() {
                return Err(err!(
                    "relocation at {place_addr:#x} is in bss or past the image"
                ));
            }
            let be = (v as u64).to_be_bytes();
            image[off..off + size].copy_from_slice(&be[8 - size..]);
        }
    }
    Ok(Linked {
        cpu: obj.arch.clone(),
        base,
        image,
        bss_size,
        symbols,
        globals,
        text_ranges,
        places: place.iter().map(|(k, v)| (k.to_string(), *v)).collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn obj(
        arch: &str,
        sections: Vec<DkoSection>,
        symbols: Vec<DkoSymbol>,
        relocs: Vec<DkoReloc>,
    ) -> Dko {
        Dko {
            format: FORMAT,
            arch: arch.into(),
            source_sha256: "0".repeat(64),
            toolchain: Default::default(),
            sections,
            symbols,
            relocs,
        }
    }

    #[test]
    fn sharc_relocations_write_the_low_bits_of_the_instruction() {
        // two 48-bit instructions in stream order (parcels little endian): CALL x / R0 = DM(gain)
        let text = [
            0x04, 0x18, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00,
        ];
        let o = obj(
            "sharc",
            vec![
                sec("t", "text", 8, &text),
                sec("d", "data", 4, &[1, 2, 3, 4]),
            ],
            vec![
                sym("gain", Some("d"), 0),
                sym("fn", Some("t"), 3),
                sym("ext", None, 0),
            ],
            vec![
                rel("t", 0, "sharc.abs24", "ext", 0),
                rel("t", 3, "sharc.abs32", "gain", 4),
            ],
        );
        let ext = BTreeMap::from([("ext".to_string(), 0xB9_1234u64)]);
        let l = link(&o, 0x2001_ab80, &ext).unwrap();
        // CALL: bits 23..16 in the low byte of parcel 1, bits 15..0 in parcel 2
        assert_eq!(&l.image[0..6], &[0x04, 0x18, 0xb9, 0x00, 0x34, 0x12]);
        // DM address: the data section follows the 12 bytes of text at its own 4-byte alignment, plus the addend
        let gain = 0x2001_ab80u64 + 12;
        assert_eq!(l.symbols["gain"], gain);
        assert_eq!(
            &l.image[8..12],
            &[
                ((gain >> 16) as u8),
                ((gain >> 24) as u8),
                (((gain + 4) & 0xff) as u8),
                (((gain + 4) >> 8) as u8)
            ]
        );
        assert_eq!(
            l.symbols["fn"],
            0xB8_0000 + 0x1_AB80 / 2 + 3,
            "text symbols are short-word pcs"
        );
    }

    #[test]
    fn coldfire_pc_relative_relocations_are_range_checked() {
        let text = [0x61, 0x00, 0, 0, 0x4e, 0x75];
        let o = obj(
            "cf",
            vec![sec(".text", "text", 2, &text)],
            vec![sym("f", Some(".text"), 0), sym("far", None, 0)],
            vec![rel(".text", 2, "cf.pc16", "far", 0)],
        );
        let near = BTreeMap::from([("far".to_string(), 0x4700_1000u64)]);
        let l = link(&o, 0x4700_0000, &near).unwrap();
        assert_eq!(&l.image[2..4], &(0x1000i16 - 2).to_be_bytes()); // S + A - P with P the displacement word
        let far = BTreeMap::from([("far".to_string(), 0x4710_0000u64)]);
        assert!(
            link(&o, 0x4700_0000, &far)
                .unwrap_err()
                .0
                .contains("out of range")
        );
        assert!(
            link(&o, 0x4700_0000, &BTreeMap::new())
                .unwrap_err()
                .0
                .contains("unresolved symbol far")
        );
    }

    #[test]
    fn cf_bss_follows_the_payload_and_is_not_in_the_image() {
        let o = obj(
            "cf",
            vec![
                sec(".text", "text", 2, &[0x4e, 0x75]),
                DkoSection {
                    name: ".bss".into(),
                    kind: "bss".into(),
                    align: 2,
                    size: 10,
                    hex: String::new(),
                },
            ],
            vec![sym("b", Some(".bss"), 4)],
            vec![],
        );
        let l = link(&o, 0x4700_0000, &BTreeMap::new()).unwrap();
        assert_eq!(
            l.image.len(),
            4,
            "payload padded to the (word aligned) bss start"
        );
        assert_eq!(l.bss_size, 10);
        assert_eq!(l.symbols["b"], 0x4700_0004 + 4);
    }
}
