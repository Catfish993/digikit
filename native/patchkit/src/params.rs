//! `core/params`: add parameter descriptors to the ColdFire MAIN OS (Digitakt II).
//!
//! The firmware's parameter table has one 60-byte row per parameter code (`0x113` rows on 1.16 and 1.17, finding 02:
//! owner, mirror index, min, max, default, flag, kind, names, formatter). Every reader inlines its own clamp
//! `code < 0x113 ? code : 0` and its own absolute address of the table, so a longer table needs all of them
//! changed. Content modules declare `[[param]]` entries; patchkit assigns the codes `0x113, 0x114, ...` in
//! canonical module order and generates, **at patch time from the image itself** (nothing of the firmware is stored):
//!
//! * the table relocated to `cf.expansion` with one extra row per added parameter, copied from the row of a stock
//!   `template` code and then given its own names, range, default and engine slot;
//! * the repointing of every absolute reference to the old table (found by a scan of the code, see [`scan`]) and
//!   the raise of every clamp bound of the shapes `cmpi.l #N,Dn; scs/scc/bhi/bls`;
//! * a shim in front of the accessor of the second per-code table (value-class objects built at boot, 0x44 bytes
//!   each): an added code answers with its template's object.
//!
//! Loops that visit every code (the defaults refresh, the per-owner lookup maps, the owner enumeration of the
//! modulation destination list) end on `bne`/`beq`, not on a clamp, and keep the stock count: the new rows do not
//! enter them. A machine's new parameters therefore reach the sound's engine slots positionally (finding 02), not
//! through those maps.
//!
//! The scan is part of the port: `tools/port_resolve.py` records the number and a hash of the sites in the anchor
//! `cf.params.sites` and patchkit checks that its own scan finds the same ones.

use crate::dko::*;
use crate::error::*;
use crate::machines::{Asm, GenPatch, Reader, expect, rel};
use crate::module::{Library, ParamDef};
use crate::port::Port;
use std::collections::BTreeMap;

pub const FRAMEWORK: &str = "core/params";
/// Stride of the parameter table.
pub const ROW: usize = 60;
/// Most added parameters.
pub const MAX_ADDED: usize = 64;
/// The mirror index of each descriptor field (engine slot), finding 02.
pub const MIRROR: [u32; 8] = [25, 26, 27, 28, 31, 32, 33, 34];

pub const T1_ANCHOR: &str = "cf.params.t1";
pub const T2_ANCHOR: &str = "cf.params.t2";
pub const SITES_ANCHOR: &str = "cf.params.sites";
const MAIN_ANCHOR: &str = "cf.main";

pub fn anchor_names() -> Vec<String> {
    vec![
        T1_ANCHOR.into(),
        T2_ANCHOR.into(),
        SITES_ANCHOR.into(),
        MAIN_ANCHOR.into(),
    ]
}

pub fn missing_anchors(port: &Port) -> Vec<String> {
    anchor_names()
        .into_iter()
        .filter(|n| port.anchor(n).is_none())
        .collect()
}

/// A parameter with the module that declared it.
#[derive(Clone, Debug)]
pub struct Declared {
    pub module: String,
    pub def: ParamDef,
}

/// The parameters of the selected modules in canonical order.
pub fn declared(lib: &Library, order: &[String]) -> Result<Vec<Declared>> {
    let mut out: Vec<Declared> = vec![];
    for id in order {
        let m = lib.get(id).ok_or_else(|| err!("unknown module {id}"))?;
        for d in &m.manifest.param {
            if let Some(prev) = out.iter().find(|p| p.def.id == d.id) {
                return Err(err!(
                    "parameter id {} is declared by both {} and {id}",
                    d.id,
                    prev.module
                ));
            }
            out.push(Declared {
                module: id.clone(),
                def: d.clone(),
            });
        }
    }
    Ok(out)
}

fn text(what: &str, s: &str) -> Result<()> {
    if s.is_empty() || !s.is_ascii() || s.contains('\0') {
        return Err(err!("parameter {what} {s:?} must be non-empty ASCII"));
    }
    Ok(())
}

/// Checks that need no image. `count` is the stock row count when known.
pub fn validate(d: &ParamDef, count: Option<u32>) -> Result<()> {
    let mut chars = d.id.chars();
    let ok = chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
    if !ok {
        return Err(err!("parameter id {:?} must match [a-z][a-z0-9_]*", d.id));
    }
    text("name", &d.name)?;
    text("short", &d.short)?;
    if d.slot > 7 {
        return Err(err!(
            "parameter {}: slot {} is not a descriptor field 0..7",
            d.id,
            d.slot
        ));
    }
    if d.min > d.max || d.default < d.min || d.default > d.max {
        return Err(err!(
            "parameter {}: need min <= default <= max, got {}, {}, {}",
            d.id,
            d.min,
            d.default,
            d.max
        ));
    }
    if let Some(n) = count {
        for (what, c) in [
            ("template", Some(d.template)),
            ("formatter_from", d.formatter_from),
        ] {
            if let Some(c) = c
                && c >= n
            {
                return Err(err!(
                    "parameter {}: {what} {c} is not a stock code (0..{})",
                    d.id,
                    n - 1
                ));
            }
        }
    }
    Ok(())
}

/// The assigned codes: `stock count + i` in canonical order.
pub fn assign(defs: &[Declared], count: u32) -> BTreeMap<String, u32> {
    defs.iter()
        .enumerate()
        .map(|(i, d)| (d.def.id.clone(), count + i as u32))
        .collect()
}

/// The sites of the code that depend on the table: absolute references to it and clamp bounds.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Sites {
    /// address of each 32-bit word that points into the table
    pub refs: Vec<u64>,
    /// (address of the compare instruction, the immediate it holds)
    pub bounds: Vec<(u64, u32)>,
}

impl Sites {
    pub fn len(&self) -> usize {
        self.refs.len() + self.bounds.len()
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// sha256 of the sites: the addresses as big-endian words, then `ffffffff`, then the bounds as address and
    /// immediate (what `tools/port_resolve.py` records).
    pub fn digest(&self) -> String {
        let mut b = vec![];
        for r in &self.refs {
            b.extend_from_slice(&(*r as u32).to_be_bytes());
        }
        b.extend_from_slice(&[0xff; 4]);
        for (a, i) in &self.bounds {
            b.extend_from_slice(&(*a as u32).to_be_bytes());
            b.extend_from_slice(&i.to_be_bytes());
        }
        sha256_hex(&b)
    }
}

/// Scan the MAIN OS image `main` (loaded at `main_base`) below the table (`t1`, `count` rows):
///
/// * every 2-aligned big-endian word in `[t1, t1 + count * 60)` is a reference to the table (the table's own end
///   address is not: another table follows it);
/// * every `cmpi.l #N,Dn` or `cmpa.l #N,An` whose immediate is `count` or `count - 1` and whose next instruction is
///   `scs`/`scc` (`55`/`54`) or `bhi`/`bls` (`62`/`63`) is a clamp bound. The compares that end a loop over all
///   codes (`bne`, `beq`) are not.
pub fn scan(main: &[u8], main_base: u64, t1: u64, count: u32) -> Sites {
    let mut s = Sites::default();
    let end = (t1.saturating_sub(main_base) as usize).min(main.len());
    let hi = t1 + count as u64 * ROW as u64;
    let be = |o: usize| u32::from_be_bytes([main[o], main[o + 1], main[o + 2], main[o + 3]]);
    let mut o = 0usize;
    while o + 4 <= end {
        let v = be(o) as u64;
        if v >= t1 && v < hi {
            s.refs.push(main_base + o as u64);
        }
        if o + 7 <= end {
            let op = u16::from_be_bytes([main[o], main[o + 1]]);
            let imm = be(o + 2);
            if (op & 0xfff8 == 0x0c80 || op & 0xf1ff == 0xb1fc)
                && (imm == count || imm + 1 == count)
                && matches!(main[o + 6], 0x54 | 0x55 | 0x62 | 0x63)
            {
                s.bounds.push((main_base + o as u64, imm));
            }
        }
        o += 2;
    }
    s
}

pub struct Generated {
    /// (object, region it is placed in)
    pub objects: Vec<(Dko, &'static str)>,
    pub patches: Vec<GenPatch>,
    /// (parameter id, code, declaring module)
    pub codes: Vec<(String, u32, String)>,
}

/// Everything the framework changes, for the parameters `defs` (canonical order), read from the image through `read`.
pub fn generate(port: &Port, read: Reader, defs: &[Declared]) -> Result<Generated> {
    let k = defs.len();
    if k == 0 {
        return Err(err!("params: nothing to generate"));
    }
    if k > MAX_ADDED {
        return Err(err!("params: {k} added parameters, at most {MAX_ADDED}"));
    }
    let anchor = |n: &str| {
        port.anchor(n)
            .ok_or_else(|| err!("port {} has no anchor {n}", port.id()))
    };
    let t1 = anchor(T1_ANCHOR)?;
    let t2 = anchor(T2_ANCHOR)?;
    let sites_a = anchor(SITES_ANCHOR)?;
    let main = anchor(MAIN_ANCHOR)?;
    if t1.len % ROW as u64 != 0 {
        return Err(err!("params: the table anchor is {} bytes", t1.len));
    }
    let count = (t1.len / ROW as u64) as u32;
    for d in defs {
        validate(&d.def, Some(count))?;
    }
    let codes: Vec<(String, u32, String)> = defs
        .iter()
        .enumerate()
        .map(|(i, d)| (d.def.id.clone(), count + i as u32, d.module.clone()))
        .collect();

    // ---- the sites, checked against the port
    let image = read(main.addr, main.len)?;
    let sites = scan(&image, main.addr, t1.addr, count);
    if sites.len() as u64 != sites_a.len || sites.digest() != sites_a.sha256 {
        return Err(err!(
            "params: the scan found {} sites (hash {}), the port recorded {} (hash {})",
            sites.len(),
            &sites.digest()[..12],
            sites_a.len,
            &sites_a.sha256[..12.min(sites_a.sha256.len())]
        ));
    }

    let stock = read(t1.addr, t1.len)?;
    let word = |row: u32, off: usize| {
        let at = row as usize * ROW + off;
        u32::from_be_bytes(stock[at..at + 4].try_into().unwrap())
    };

    let mut data = Asm::new("parfw.data");
    data.align(4);
    data.sym("parfw_t1");
    data.bytes(&stock);
    for (i, d) in defs.iter().enumerate() {
        let p = &d.def;
        let t = p.template;
        data.w32(word(t, 0)); // owner
        data.w32(MIRROR[p.slot as usize]);
        data.w32(p.min);
        data.w32(p.max);
        data.w32(p.default);
        data.w32(word(t, 0x14));
        let cc: u32 = if p.slot == 3 { 0xffff } else { 0x10 + p.slot };
        data.w32(cc << 16 | (word(t, 0x18) & 0xffff));
        data.w32(0x80 + p.slot);
        data.w32(word(t, 0x20));
        data.w32(word(t, 0x24));
        data.abs32(&format!("parfw_n_{i}"), 0);
        data.w32(word(t, 0x2c));
        data.abs32(&format!("parfw_s_{i}"), 0);
        data.w32(word(p.formatter_from.unwrap_or(t), 0x34));
        data.w32(word(t, 0x38));
    }
    debug_assert_eq!(data.code.len(), stock.len() + k * ROW);
    for (i, d) in defs.iter().enumerate() {
        data.sym(&format!("parfw_n_{i}"));
        data.bytes(d.def.name.as_bytes());
        data.bytes(&[0]);
        data.sym(&format!("parfw_s_{i}"));
        data.bytes(d.def.short.as_bytes());
        data.bytes(&[0]);
    }
    data.align(4);
    data.sym("parfw_t2map");
    for d in defs {
        data.w32(d.def.template);
    }

    let mut patches: Vec<GenPatch> = vec![];
    let off = |addr: u64| addr - main.addr;
    let (t2_lo, t2_hi) = (t2.addr, t2.addr + t2.len);
    for r in &sites.refs {
        let old = u32::from_be_bytes(
            image[off(*r) as usize..off(*r) as usize + 4]
                .try_into()
                .unwrap(),
        ) as u64;
        patches.push(GenPatch {
            anchor: MAIN_ANCHOR.into(),
            at: off(*r),
            bytes: vec![0; 4],
            relocs: vec![rel(0, "parfw_t1", (old - t1.addr) as i64)],
            what: "param table reference".into(),
        });
    }
    let mut raised = 0usize;
    for (a, imm) in &sites.bounds {
        if *a >= t2_lo && *a < t2_hi {
            continue; // the second table's accessor is replaced below
        }
        patches.push(GenPatch {
            anchor: MAIN_ANCHOR.into(),
            at: off(*a) + 2,
            bytes: (imm + k as u32).to_be_bytes().to_vec(),
            relocs: vec![],
            what: "param code bound".into(),
        });
        raised += 1;
    }
    if raised == 0 {
        return Err(err!("params: no clamp bound found"));
    }

    // ---- the second table: an added code answers with its template's row. The accessor begins
    // `move.l 4(sp),D0; cmpi.l #count,D0` (10 bytes), the shim replays both.
    let head = read(t2.addr, t2.len)?;
    let mut want = vec![0x20, 0x2f, 0x00, 0x04, 0x0c, 0x80];
    want.extend_from_slice(&count.to_be_bytes());
    expect("param accessor head", &head, &want)?;
    let mut asm = Asm::new("parfw.text");
    asm.align(2);
    asm.sym("parfw_t2_shim");
    asm.w16(0x202f);
    asm.w16(0x0004); // move.l 4(sp),D0
    asm.cmpi_l(0, count);
    asm.bcc_w(5, "stock"); // below the stock count (carry set)
    asm.cmpi_l(0, count + k as u32);
    asm.bcc_w(4, "stock"); // beyond the added rows: the stock code clamps it
    asm.w16(0x0480);
    asm.w32(count); // subi.l #count,D0
    asm.w16(0xe588); // lsl.l #2,D0
    asm.w16(0x41f9);
    asm.abs32("parfw_t2map", 0); // lea map,A0
    asm.w16(0x2030);
    asm.w16(0x0800); // move.l (0,A0,D0.l),D0
    asm.label("stock");
    asm.cmpi_l(0, count);
    asm.jmp_const((t2.addr + t2.len) as u32);
    asm.finish()?;
    let mut bytes = vec![0x4e, 0xf9, 0, 0, 0, 0];
    bytes.extend_from_slice(&[0x4e, 0x71, 0x4e, 0x71]);
    patches.push(GenPatch {
        anchor: T2_ANCHOR.into(),
        at: 0,
        bytes,
        relocs: vec![rel(2, "parfw_t2_shim", 0)],
        what: "second table accessor".into(),
    });
    data.finish()?;

    let obj = |sections: Vec<DkoSection>, symbols: Vec<DkoSymbol>, relocs: Vec<DkoReloc>| Dko {
        format: FORMAT,
        arch: "cf".into(),
        source_sha256: sha256_hex(b"core/params generated"),
        toolchain: BTreeMap::from([("generator".into(), "patchkit params.rs".into())]),
        sections,
        symbols,
        relocs,
    };
    let sym = |name: &String, section: &str, value: usize| DkoSymbol {
        name: name.clone(),
        section: Some(section.into()),
        value: value as u64,
        global: true,
    };
    let code = obj(
        vec![DkoSection {
            name: "parfw.text".into(),
            kind: "text".into(),
            align: 2,
            size: asm.code.len() as u32,
            hex: hex(&asm.code),
        }],
        asm.syms
            .iter()
            .map(|(n, o)| sym(n, "parfw.text", *o))
            .collect(),
        asm.relocs.clone(),
    );
    let tables = obj(
        vec![DkoSection {
            name: "parfw.data".into(),
            kind: "data".into(),
            align: 4,
            size: data.code.len() as u32,
            hex: hex(&data.code),
        }],
        data.syms
            .iter()
            .map(|(n, o)| sym(n, "parfw.data", *o))
            .collect(),
        data.relocs.clone(),
    );
    Ok(Generated {
        objects: vec![(code, "cf.flash_cave"), (tables, "cf.expansion")],
        patches,
        codes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn def() -> ParamDef {
        ParamDef {
            id: "decay".into(),
            name: "Decay".into(),
            short: "DECAY".into(),
            template: 0xd1,
            slot: 1,
            min: 0,
            max: 0x7f00,
            default: 0x4000,
            formatter_from: None,
        }
    }

    #[test]
    fn definitions_are_validated() {
        assert!(validate(&def(), Some(275)).is_ok());
        let mut d = def();
        d.slot = 8;
        assert!(validate(&d, None).unwrap_err().0.contains("slot 8"));
        let mut d = def();
        d.default = 0x8000;
        assert!(validate(&d, None).unwrap_err().0.contains("min <= default"));
        let mut d = def();
        d.template = 275;
        assert!(validate(&d, Some(275)).unwrap_err().0.contains("template"));
        let mut d = def();
        d.id = "Bad".into();
        assert!(validate(&d, None).is_err());
    }

    #[test]
    fn scan_finds_references_and_clamp_bounds_but_not_loop_ends() {
        // base 0x1000: [cmpi.l #5,D0; scs D1b] [lea (0x2004).l,A0] [cmpi.l #5,D2; bne.b] [cmpi.l #4,D1; bhi.b]
        let mut m = vec![];
        m.extend_from_slice(&[0x0c, 0x80, 0, 0, 0, 5, 0x55, 0xc1]); // bound
        m.extend_from_slice(&[0x41, 0xf9, 0, 0, 0x20, 0x04]); // ref into [0x2000, 0x2000 + 5 * 60)
        m.extend_from_slice(&[0x0c, 0x82, 0, 0, 0, 5, 0x66, 0xdc]); // loop end
        m.extend_from_slice(&[0x0c, 0x81, 0, 0, 0, 4, 0x62, 0x04]); // bound
        m.extend_from_slice(&[0, 0, 0, 0]);
        let s = scan(&m, 0x1000, 0x2000, 5);
        assert_eq!(s.refs, vec![0x1000 + 10]);
        assert_eq!(s.bounds, vec![(0x1000, 5), (0x1000 + 22, 4)]);
        assert_eq!(s.digest(), s.clone().digest());
    }

    #[test]
    fn codes_follow_the_stock_count() {
        let defs = vec![Declared {
            module: "m".into(),
            def: def(),
        }];
        assert_eq!(assign(&defs, 275)["decay"], 275);
    }
}
