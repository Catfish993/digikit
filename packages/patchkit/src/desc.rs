//! Descriptor records: what a content module declares in C with the `DK_*` macros of `<dk/fx.h>`.
//!
//! The macros put fixed-layout records in the `.dk.desc` section of the module's ColdFire object. `read` decodes them to the typed values
//! of `typed::FxDef`, which `pack` lowers; `strip` takes the section out of the object, as the
//! records never reach the device. The layout is `[desc]` of `sdk/sdk.toml` (`sdkgen::desc_spec`); no offset is written here.

use crate::dko::{Dko, ISA_CHECK_KEY};
use crate::error::*;
use crate::module::Module;
use crate::sdkgen::{self, DescRecord, DescSpec, DescTy};
use crate::typed::{FxDef, FxKnob};

/// The declarations of one object.
#[derive(Debug)]
pub struct Desc {
    /// The effects, sorted by id.
    pub fx: Vec<FxDef>,
}

fn be(b: &[u8], off: usize) -> u32 {
    u32::from_be_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

fn konst(spec: &DescSpec, name: &str) -> Result<u32> {
    spec.consts
        .get(name)
        .copied()
        .ok_or_else(|| err!("{}: [desc] has no const {name}", sdkgen::SOURCE))
}

fn record<'a>(spec: &'a DescSpec, name: &str) -> Result<&'a DescRecord> {
    spec.records
        .iter()
        .find(|r| r.name == name)
        .ok_or_else(|| err!("{}: [desc] has no record {name}", sdkgen::SOURCE))
}

/// A word field of the record in `b`.
fn word(b: &[u8], rec: &DescRecord, name: &str) -> Result<u32> {
    Ok(be(b, rec.field(name)?.off))
}

/// A text field of the record in `b`: printable ASCII up to a NUL, NUL-padded to the end.
fn text(what: &str, b: &[u8], rec: &DescRecord, name: &str) -> Result<String> {
    let f = rec.field(name)?;
    let raw = &b[f.off..f.off + f.size];
    let Some(nul) = raw.iter().position(|&c| c == 0) else {
        return Err(err!("{what}: {name}: no NUL in {} bytes", f.size));
    };
    if raw[nul..].iter().any(|&c| c != 0) {
        return Err(err!("{what}: {name}: bytes after the NUL are not zero"));
    }
    if raw[..nul].iter().any(|c| !(0x20..=0x7e).contains(c)) {
        return Err(err!("{what}: {name}: not printable ASCII"));
    }
    Ok(raw[..nul].iter().map(|&c| c as char).collect())
}

fn knob(what: &str, spec: &DescSpec, b: &[u8], rec: &DescRecord, i: usize) -> Result<FxKnob> {
    let w = word(b, rec, "word")?;
    if w as usize != i {
        return Err(err!("{what}: word {w} is not its index {i}"));
    }
    let display = match word(b, rec, "display")? {
        0 => None,
        d if d == konst(spec, "DK_PERCENT")? => Some("percent".to_string()),
        d if d == konst(spec, "DK_BIPOLAR")? => Some("bipolar".to_string()),
        d => return Err(err!("{what}: display {d} is not DK_PERCENT or DK_BIPOLAR")),
    };
    let float = |name: &str| -> Result<f64> { Ok(f64::from(f32::from_bits(word(b, rec, name)?))) };
    Ok(FxKnob {
        name: text(what, b, rec, "name")?,
        short: text(what, b, rec, "short_name")?,
        min: float("min")?,
        max: float("max")?,
        default: float("def")?,
        display,
        position: Some(word(b, rec, "position")?),
    })
}

fn fx_def(what: &str, spec: &DescSpec, b: &[u8], rec: &DescRecord) -> Result<FxDef> {
    let req = |name: &str| -> Result<String> {
        let t = text(what, b, rec, name)?;
        if t.is_empty() {
            return Err(err!("{what}: {name} is empty"));
        }
        Ok(t)
    };
    let opt = |name: &str| -> Result<Option<String>> {
        Ok(Some(text(what, b, rec, name)?).filter(|t| !t.is_empty()))
    };
    let master = konst(spec, "DK_ROUTE_MASTER")?;
    let send = konst(spec, "DK_ROUTE_SEND")?;
    let route = match word(b, rec, "route")? {
        0 => None,
        r if r == master => Some("master".to_string()),
        r if r == send => Some("send".to_string()),
        r => {
            return Err(err!(
                "{what}: route {r} is not DK_ROUTE_MASTER or DK_ROUTE_SEND"
            ));
        }
    };
    let mask = word(b, rec, "routes")?;
    if mask & !(master | send) != 0 {
        return Err(err!(
            "{what}: routes {mask:#x} has bits other than DK_ROUTE_MASTER and DK_ROUTE_SEND"
        ));
    }
    let routes = if mask == 0 {
        None
    } else {
        let mut v = vec![];
        if mask & send != 0 {
            v.push("send".to_string());
        }
        if mask & master != 0 {
            v.push("master".to_string());
        }
        Some(v)
    };
    // the knobs: a prefix of present entries, the rest all zero
    let kf = rec.field("knob")?;
    let DescTy::Rec { rec: kname, count } = &kf.ty else {
        return Err(err!(
            "{}: [desc] {}.knob is not an array of records",
            sdkgen::SOURCE,
            rec.name
        ));
    };
    let krec = record(spec, kname)?;
    let mut knobs = vec![];
    let mut absent = false;
    for i in 0..*count {
        let kb = &b[kf.off + i * krec.size..kf.off + (i + 1) * krec.size];
        let kwhat = format!("{what}: knob {i}");
        if text(&kwhat, kb, krec, "name")?.is_empty() {
            if kb.iter().any(|&c| c != 0) {
                return Err(err!("{kwhat}: has no name but is not all zero"));
            }
            absent = true;
            continue;
        }
        if absent {
            return Err(err!("{kwhat}: follows an unused entry (a gap)"));
        }
        knobs.push(knob(&kwhat, spec, kb, krec, i)?);
    }
    Ok(FxDef {
        id: req("id")?,
        process: req("process")?,
        desc: req("desc")?,
        defaults: vec![],
        devices: vec![],
        tab: opt("tab")?,
        title: opt("title")?,
        mix: i64::from(word(b, rec, "mix")?),
        knob: knobs,
        route,
        routes,
    })
}

/// Sort by id; an id twice is an error.
fn sorted(mut fx: Vec<FxDef>) -> Result<Vec<FxDef>> {
    fx.sort_by(|a, b| a.id.cmp(&b.id));
    if let Some(w) = fx.windows(2).find(|w| w[0].id == w[1].id) {
        return Err(err!("effect {} is declared twice", w[0].id));
    }
    Ok(fx)
}

/// The records of an object. Only a ColdFire object can hold them.
pub fn read(obj: &Dko) -> Result<Desc> {
    let spec = sdkgen::desc_spec();
    let mut fx = vec![];
    for sec in obj.sections.iter().filter(|s| s.name == spec.section) {
        let sname = &sec.name;
        if obj.arch != "cf" {
            return Err(err!(
                "{sname}: only a ColdFire object holds descriptor records, this one is {}",
                obj.arch
            ));
        }
        if let Some(r) = obj.relocs.iter().find(|r| r.section == *sname) {
            return Err(err!(
                "{sname}: relocation at {:#x} ({}): a record holds no addresses",
                r.offset,
                r.symbol
            ));
        }
        let b = unhex(&sec.hex)?;
        let head = record(&spec, "dk_desc_fx")?;
        let (kind_off, size_off) = (head.field("kind")?.off, head.field("size")?.off);
        let fx_kind = konst(&spec, "DK_DESC_FX")?;
        let mut at = 0;
        let mut index = 0;
        while at < b.len() {
            let what = format!("{sname}: record {index}");
            if b.len() - at < kind_off.max(size_off) + 4 {
                return Err(err!("{what}: the section ends inside the header"));
            }
            let kind = be(&b, at + kind_off);
            let size = be(&b, at + size_off) as usize;
            let rec = spec
                .records
                .iter()
                .find(|r| r.kind == Some(kind))
                .ok_or_else(|| err!("{what}: unknown record kind {kind}"))?;
            if size != rec.size {
                return Err(err!(
                    "{what}: size {size} is not the {} bytes of {}",
                    rec.size,
                    rec.name
                ));
            }
            if size > b.len() - at {
                return Err(err!(
                    "{what}: {} bytes do not fit in the {} left",
                    size,
                    b.len() - at
                ));
            }
            if kind == fx_kind {
                fx.push(fx_def(&what, &spec, &b[at..at + size], rec)?);
            } else {
                return Err(err!("{what}: no reader for {}", rec.name));
            }
            at += size;
            index += 1;
        }
    }
    Ok(Desc { fx: sorted(fx)? })
}

/// A copy of the object without the descriptor sections, their symbols and their relocations; `None` when nothing is left in it.
/// The ISA check record, if it matched the original, is renewed for the remaining bytes.
pub fn strip(obj: &Dko) -> Option<Dko> {
    let section = sdkgen::desc_spec().section;
    let mut o = obj.clone();
    o.sections.retain(|s| s.name != section);
    o.symbols
        .retain(|s| s.section.as_deref() != Some(section.as_str()));
    o.relocs.retain(|r| r.section != section);
    if !o.sections.iter().any(|s| s.size > 0) {
        return None;
    }
    if obj.toolchain.contains_key(ISA_CHECK_KEY) && obj.require_isa_checked().is_ok() {
        o.stamp_isa();
    }
    Some(o)
}

/// The effects of a module: the records of its ColdFire objects.
pub fn module_fx(m: &Module) -> Result<Vec<FxDef>> {
    let mut fx = vec![];
    for o in &m.objects {
        fx.extend(read(o)?.fx);
    }
    sorted(fx)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dko::{DkoReloc, DkoSection, DkoSymbol};
    use crate::sdkgen::DescField;

    fn put(b: &mut [u8], off: usize, v: u32) {
        b[off..off + 4].copy_from_slice(&v.to_be_bytes());
    }

    /// Text in field `f` of the record that starts at `base`.
    fn put_text(b: &mut [u8], base: usize, f: &DescField, s: &str) {
        let at = base + f.off;
        b[at..at + f.size].fill(0);
        b[at..at + s.len()].copy_from_slice(s.as_bytes());
    }

    /// One knob entry at index `i` of the effect record `b`.
    fn set_knob(b: &mut [u8], spec: &DescSpec, i: usize, name: &str, position: u32) {
        let fx = record(spec, "dk_desc_fx").unwrap();
        let k = record(spec, "dk_desc_knob").unwrap();
        let base = fx.field("knob").unwrap().off + i * k.size;
        put(b, base + k.field("word").unwrap().off, i as u32);
        put(b, base + k.field("position").unwrap().off, position);
        put(b, base + k.field("display").unwrap().off, 2);
        put(b, base + k.field("min").unwrap().off, (-64.0f32).to_bits());
        put(b, base + k.field("max").unwrap().off, 63.0f32.to_bits());
        put(b, base + k.field("def").unwrap().off, 0.0f32.to_bits());
        put_text(b, base, k.field("name").unwrap(), name);
        put_text(b, base, k.field("short_name").unwrap(), "SHIFT");
    }

    /// An effect record `id` with one knob.
    fn fx_bytes(id: &str) -> Vec<u8> {
        let spec = sdkgen::desc_spec();
        let fx = record(&spec, "dk_desc_fx").unwrap();
        let mut b = vec![0u8; fx.size];
        put(
            &mut b,
            fx.field("kind").unwrap().off,
            spec.consts["DK_DESC_FX"],
        );
        put(&mut b, fx.field("size").unwrap().off, fx.size as u32);
        put_text(&mut b, 0, fx.field("id").unwrap(), id);
        put_text(&mut b, 0, fx.field("process").unwrap(), "_fs_process");
        put_text(&mut b, 0, fx.field("desc").unwrap(), "_fs_desc");
        put_text(&mut b, 0, fx.field("tab").unwrap(), "FSH");
        put_text(&mut b, 0, fx.field("title").unwrap(), "Freq Shifter");
        put(&mut b, fx.field("mix").unwrap().off, 127);
        put(
            &mut b,
            fx.field("route").unwrap().off,
            spec.consts["DK_ROUTE_SEND"],
        );
        put(
            &mut b,
            fx.field("routes").unwrap().off,
            spec.consts["DK_ROUTE_SEND"] | spec.consts["DK_ROUTE_MASTER"],
        );
        set_knob(&mut b, &spec, 0, "Freq Shift", 0);
        b
    }

    fn obj(bytes: &[u8]) -> Dko {
        Dko {
            format: 1,
            arch: "cf".into(),
            source_sha256: String::new(),
            toolchain: Default::default(),
            sections: vec![DkoSection {
                name: sdkgen::desc_spec().section,
                kind: "data".into(),
                align: 4,
                size: bytes.len() as u32,
                hex: hex(bytes),
            }],
            symbols: vec![],
            relocs: vec![],
        }
    }

    /// The error of reading the default record after `edit`.
    fn broken(edit: impl Fn(&mut Vec<u8>, &DescSpec)) -> String {
        let spec = sdkgen::desc_spec();
        let mut b = fx_bytes("fshift");
        edit(&mut b, &spec);
        read(&obj(&b)).unwrap_err().0
    }

    #[test]
    fn an_effect_record_reads_to_its_fx_def() {
        let d = read(&obj(&fx_bytes("fshift"))).unwrap();
        let [f] = d.fx.as_slice() else {
            panic!("one effect")
        };
        assert_eq!(
            (f.id.as_str(), f.process.as_str(), f.desc.as_str()),
            ("fshift", "_fs_process", "_fs_desc")
        );
        assert_eq!(
            (f.tab.as_deref(), f.title.as_deref()),
            (Some("FSH"), Some("Freq Shifter"))
        );
        assert_eq!(f.mix, 127);
        assert_eq!(f.route.as_deref(), Some("send"));
        assert_eq!(
            f.routes,
            Some(vec!["send".to_string(), "master".to_string()])
        );
        assert!(f.defaults.is_empty() && f.devices.is_empty());
        let [k] = f.knob.as_slice() else {
            panic!("one knob")
        };
        assert_eq!((k.name.as_str(), k.short.as_str()), ("Freq Shift", "SHIFT"));
        assert_eq!((k.min, k.max, k.default), (-64.0, 63.0, 0.0));
        assert_eq!(
            (k.display.as_deref(), k.position),
            (Some("bipolar"), Some(0))
        );
    }

    #[test]
    fn effects_are_sorted_and_an_id_twice_is_refused() {
        let mut b = fx_bytes("zeta");
        b.extend(fx_bytes("alpha"));
        let ids: Vec<_> = read(&obj(&b))
            .unwrap()
            .fx
            .into_iter()
            .map(|f| f.id)
            .collect();
        assert_eq!(ids, ["alpha", "zeta"]);
        let mut b = fx_bytes("same");
        b.extend(fx_bytes("same"));
        assert!(read(&obj(&b)).unwrap_err().0.contains("twice"));
    }

    #[test]
    fn a_malformed_text_field_is_refused() {
        let id = |spec: &DescSpec| {
            record(spec, "dk_desc_fx")
                .unwrap()
                .field("id")
                .unwrap()
                .clone()
        };
        assert!(
            broken(|b, s| {
                let f = id(s);
                b[f.off..f.off + f.size].fill(b'a');
            })
            .contains("no NUL")
        );
        assert!(broken(|b, s| b[id(s).off + 10] = b'x').contains("after the NUL"));
        assert!(broken(|b, s| b[id(s).off] = 0x7f).contains("printable"));
    }

    #[test]
    fn a_malformed_knob_is_refused() {
        let fx_knob = |spec: &DescSpec| {
            let fx = record(spec, "dk_desc_fx").unwrap();
            let k = record(spec, "dk_desc_knob").unwrap();
            (fx.field("knob").unwrap().off, k.clone())
        };
        // display is not DK_PERCENT or DK_BIPOLAR
        assert!(
            broken(|b, s| {
                let (base, k) = fx_knob(s);
                put(b, base + k.field("display").unwrap().off, 3);
            })
            .contains("display 3")
        );
        // knob 2 present while knob 1 is unused
        assert!(broken(|b, s| set_knob(b, s, 2, "Other", 2)).contains("gap"));
        // the word is not the index
        assert!(
            broken(|b, s| {
                let (base, k) = fx_knob(s);
                put(b, base + k.field("word").unwrap().off, 1);
            })
            .contains("word 1")
        );
        // an unused entry holds data
        assert!(
            broken(|b, s| {
                let (base, k) = fx_knob(s);
                b[base + k.size + k.field("min").unwrap().off] = 1;
            })
            .contains("not all zero")
        );
    }

    #[test]
    fn a_malformed_record_is_refused() {
        let fx_field =
            |spec: &DescSpec, n: &str| record(spec, "dk_desc_fx").unwrap().field(n).unwrap().off;
        assert!(broken(|b, s| put(b, fx_field(s, "kind"), 99)).contains("unknown record kind"));
        assert!(
            broken(|b, s| {
                let size = record(s, "dk_desc_fx").unwrap().size as u32;
                put(b, fx_field(s, "size"), size + 4);
            })
            .contains("size")
        );
        // a record cut short
        let b = fx_bytes("fshift");
        assert!(
            read(&obj(&b[..b.len() - 4]))
                .unwrap_err()
                .0
                .contains("do not fit")
        );
        // routes with a bit that is no route
        assert!(broken(|b, s| put(b, fx_field(s, "routes"), 8)).contains("routes"));
    }

    #[test]
    fn a_relocation_or_a_sharc_object_is_refused() {
        let mut o = obj(&fx_bytes("fshift"));
        o.relocs.push(DkoReloc {
            section: o.sections[0].name.clone(),
            offset: 0,
            rtype: "abs32".into(),
            symbol: "_x".into(),
            addend: 0,
        });
        assert!(read(&o).unwrap_err().0.contains("relocation"));
        let mut o = obj(&fx_bytes("fshift"));
        o.arch = "sharc".into();
        assert!(read(&o).unwrap_err().0.contains("ColdFire"));
    }

    #[test]
    fn strip_takes_the_records_out() {
        // a records-only object has nothing left
        assert!(strip(&obj(&fx_bytes("fshift"))).is_none());
        // the other sections stay; the records' symbols and relocations go
        let mut o = obj(&fx_bytes("fshift"));
        let desc = o.sections[0].name.clone();
        o.sections.push(DkoSection {
            name: ".text".into(),
            kind: "text".into(),
            align: 2,
            size: 2,
            hex: "4e75".into(),
        });
        for (name, section) in [
            ("_desc", Some(desc.clone())),
            ("_main", Some(".text".to_string())),
        ] {
            o.symbols.push(DkoSymbol {
                name: name.into(),
                section,
                value: 0,
                global: true,
            });
        }
        o.relocs.push(DkoReloc {
            section: desc,
            offset: 0,
            rtype: "abs32".into(),
            symbol: "_main".into(),
            addend: 0,
        });
        let s = strip(&o).unwrap();
        assert_eq!(s.sections.len(), 1);
        assert_eq!(s.sections[0].name, ".text");
        assert_eq!(s.symbols.len(), 1);
        assert_eq!(s.symbols[0].name, "_main");
        assert!(s.relocs.is_empty());
    }
}
