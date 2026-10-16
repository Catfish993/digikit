//! The `register` section of a `.dkm`: typed machine and parameter records for the digikit runtime's registry
//! (docs/design/digikit-registry.md). The packer lowers a dynamic module's typed sections (`typed.rs`) for one device into
//! these records; the runtime (`modules/core/runtime/src/rs/src/registry.rs`) validates them before it writes any row.
//!
//! All integers are big endian. Layout:
//!
//! ```text
//! 0x00 u32 magic "DKRG"   0x04 u32 version = 1   0x08 u32 device (1 dt2, 2 dn2)   0x0c u32 record count
//! 0x10 records, each: u32 kind, u32 length (bytes, a multiple of 4, this header included), then
//!   kind 1 PARAM   (0x50): u32 index, template, slot; i32 min, max, default; name[32]; short[16]
//!   kind 2 MACHINE (0x6c): u32 type, clone_of, position, own_dsp, level; i32 knobs[8]; name[24]; short[24]
//!   kind 3 PAGE    (0x60): u32 type, knobs[8], level; name[24]; title[24]            (Digitone II)
//! ```
//!
//! A knob is a stock parameter code (0 = none) or `KNOB_PARAM | index` of a PARAM record of the same file. A page's knob
//! is `KNOB_PARAM | index` or 0; its sound-array slot is the device's slot base plus the knob position. On a device that
//! keeps its knobs in pages (the Digitone II) the machine record's knobs are 0.

use crate::error::*;
use crate::typed::{DevResolved, Knob};
use std::collections::BTreeMap;

pub const MAGIC: u32 = 0x444b_5247; // "DKRG"
pub const VERSION: u32 = 1;
pub const HEADER: usize = 16;
pub const KIND_PARAM: u32 = 1;
pub const KIND_MACHINE: u32 = 2;
pub const KIND_PAGE: u32 = 3;
pub const LEN_PARAM: usize = 0x50;
pub const LEN_MACHINE: usize = 0x6c;
pub const LEN_PAGE: usize = 0x60;
pub const MAX_RECORDS: usize = 32;
pub const KNOBS: usize = 8;
pub const KNOB_PARAM: u32 = 0x8000_0000;
/// `position` of a run-time machine: listed after the built-in machines, in type order.
pub const POS_END: u32 = 255;
pub const MAX_PARAMS: usize = 16;
pub const MAX_MACHINES: usize = 8;
pub const MAX_PAGES: usize = 8;
pub const MACHINE_NAME: usize = 24;
pub const PARAM_NAME: usize = 32;
pub const PARAM_SHORT: usize = 16;

pub fn device_id(dev: &str) -> Result<u32> {
    match dev {
        "dt2" => Ok(1),
        "dn2" => Ok(2),
        _ => Err(err!("no run-time registry for device {dev:?}")),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Param {
    pub index: u32,
    pub template: u32,
    pub slot: u32,
    pub min: i32,
    pub max: i32,
    pub default: i32,
    pub name: String,
    pub short: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Machine {
    pub type_no: u32,
    pub clone_of: u32,
    pub position: u32,
    pub own_dsp: u32,
    pub level: u32,
    pub knobs: [u32; KNOBS],
    pub name: String,
    pub short: String,
}

/// A parameter page of a machine (Digitone II): the knobs A .. H and the level code.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    pub type_no: u32,
    pub knobs: [u32; KNOBS],
    pub level: u32,
    pub name: String,
    pub title: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Register {
    pub device: u32,
    pub params: Vec<Param>,
    pub machines: Vec<Machine>,
    pub pages: Vec<Page>,
}

fn text_ok(what: &str, s: &str, max: usize) -> Result<()> {
    if s.is_empty() || s.len() > max {
        return Err(err!("{what} {s:?}: 1 to {max} characters"));
    }
    if !s.bytes().all(|c| (0x20..0x7f).contains(&c)) {
        return Err(err!("{what} {s:?}: printable ASCII only"));
    }
    Ok(())
}

fn put_text(out: &mut Vec<u8>, s: &str, field: usize) {
    let mut b = s.as_bytes().to_vec();
    b.resize(field, 0);
    out.extend_from_slice(&b);
}

fn p32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_be_bytes());
}

impl Register {
    /// Lower one device's resolved typed sections. `types` maps a machine id to its permanent run-time type number.
    pub fn lower(r: &DevResolved, types: &BTreeMap<String, i64>) -> Result<Register> {
        let mut reg = Register {
            device: device_id(&r.dev)?,
            ..Default::default()
        };
        for (i, p) in r.params.iter().enumerate() {
            reg.params.push(Param {
                index: i as u32,
                template: p.template as u32,
                slot: p.slot as u32,
                min: p.min as i32,
                max: p.max as i32,
                default: p.default as i32,
                name: p.name.clone(),
                short: p.short.clone(),
            });
        }
        for m in &r.machines {
            if m.position.is_some_and(|p| p != POS_END as i64) {
                return Err(err!(
                    "machine {}: a run-time machine is listed after the built-in ones; drop `position`",
                    m.id
                ));
            }
            let t = types.get(&m.id).copied().ok_or_else(|| {
                err!(
                    "machine {}: no run-time type number (add it to [{}.dynamic] of modules/core/machines/types.toml)",
                    m.id,
                    r.dev
                )
            })?;
            let mut knobs = [0u32; KNOBS];
            if r.model.knobs.to == "machine" {
                for (j, k) in m.knobs.iter().enumerate() {
                    knobs[j] = match k {
                        Knob::Code(c) if (0..=0xffff).contains(c) => *c as u32,
                        Knob::Code(c) => return Err(err!("machine {}: knob code {c}", m.id)),
                        Knob::Param(id) => {
                            let i = r.params.iter().position(|p| &p.id == id).ok_or_else(|| {
                                err!("machine {}: knob names {id:?}, not a param", m.id)
                            })?;
                            KNOB_PARAM | i as u32
                        }
                    };
                }
            }
            // otherwise the device keeps its knobs in pages: the machine record's knobs stay 0
            reg.machines.push(Machine {
                type_no: t as u32,
                clone_of: m.clone_of as u32,
                position: POS_END,
                own_dsp: u32::from(m.own_dsp),
                level: m.level as u32,
                knobs,
                name: m.name.clone(),
                short: m.short.clone(),
            });
        }
        for g in &r.pages {
            let t = types.get(&g.machine).copied().ok_or_else(|| {
                err!("page {}: machine {:?} has no run-time type number", g.id, g.machine)
            })?;
            let mut knobs = [0u32; KNOBS];
            for (j, k) in g.knobs.iter().enumerate() {
                knobs[j] = match k {
                    Knob::Code(0) => 0,
                    Knob::Code(c) => return Err(err!("page {}: knob {j}: stock code {c}", g.id)),
                    Knob::Param(id) => {
                        let i = r.params.iter().position(|p| &p.id == id).ok_or_else(|| {
                            err!("page {}: knob names {id:?}, not a param", g.id)
                        })?;
                        KNOB_PARAM | i as u32
                    }
                };
            }
            reg.pages.push(Page {
                type_no: t as u32,
                knobs,
                level: g.level as u32,
                name: g.name.clone(),
                title: g.title.clone(),
            });
        }
        reg.check()?;
        Ok(reg)
    }

    /// The checks the runtime repeats on the file.
    pub fn check(&self) -> Result<()> {
        if self.params.len() > MAX_PARAMS
            || self.machines.len() > MAX_MACHINES
            || self.pages.len() > MAX_PAGES
        {
            return Err(err!(
                "at most {MAX_PARAMS} params, {MAX_MACHINES} machines and {MAX_PAGES} pages per module"
            ));
        }
        for p in &self.params {
            text_ok("param name", &p.name, PARAM_NAME - 1)?;
            text_ok("param short", &p.short, PARAM_SHORT - 1)?;
            if p.min > p.max {
                return Err(err!("param {}: min above max", p.name));
            }
        }
        let mut seen = vec![];
        for m in &self.machines {
            text_ok("machine name", &m.name, MACHINE_NAME - 1)?;
            text_ok("machine short", &m.short, MACHINE_NAME - 1)?;
            if seen.contains(&m.type_no) {
                return Err(err!("two machines with type {}", m.type_no));
            }
            seen.push(m.type_no);
            for k in m.knobs {
                if k & KNOB_PARAM != 0 && ((k & !KNOB_PARAM) as usize) >= self.params.len() {
                    return Err(err!("machine {}: knob names a missing param", m.name));
                }
            }
        }
        for g in &self.pages {
            text_ok("page name", &g.name, MACHINE_NAME - 1)?;
            text_ok("page title", &g.title, MACHINE_NAME - 1)?;
            if !self.machines.iter().any(|m| m.type_no == g.type_no) {
                return Err(err!("page {}: no machine of type {} in the file", g.name, g.type_no));
            }
            for k in g.knobs {
                if k != 0 && (k & KNOB_PARAM == 0 || ((k & !KNOB_PARAM) as usize) >= self.params.len()) {
                    return Err(err!("page {}: knob is not a param of the file", g.name));
                }
            }
        }
        Ok(())
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut o = vec![];
        p32(&mut o, MAGIC);
        p32(&mut o, VERSION);
        p32(&mut o, self.device);
        p32(&mut o, (self.params.len() + self.machines.len() + self.pages.len()) as u32);
        for p in &self.params {
            p32(&mut o, KIND_PARAM);
            p32(&mut o, LEN_PARAM as u32);
            p32(&mut o, p.index);
            p32(&mut o, p.template);
            p32(&mut o, p.slot);
            p32(&mut o, p.min as u32);
            p32(&mut o, p.max as u32);
            p32(&mut o, p.default as u32);
            put_text(&mut o, &p.name, PARAM_NAME);
            put_text(&mut o, &p.short, PARAM_SHORT);
        }
        for m in &self.machines {
            p32(&mut o, KIND_MACHINE);
            p32(&mut o, LEN_MACHINE as u32);
            for v in [m.type_no, m.clone_of, m.position, m.own_dsp, m.level] {
                p32(&mut o, v);
            }
            for k in m.knobs {
                p32(&mut o, k);
            }
            put_text(&mut o, &m.name, MACHINE_NAME);
            put_text(&mut o, &m.short, MACHINE_NAME);
        }
        for g in &self.pages {
            p32(&mut o, KIND_PAGE);
            p32(&mut o, LEN_PAGE as u32);
            p32(&mut o, g.type_no);
            for k in g.knobs {
                p32(&mut o, k);
            }
            p32(&mut o, g.level);
            put_text(&mut o, &g.name, MACHINE_NAME);
            put_text(&mut o, &g.title, MACHINE_NAME);
        }
        o
    }

    pub fn parse(b: &[u8]) -> Result<Register> {
        let g = |off: usize| -> Result<u32> {
            b.get(off..off + 4)
                .map(|x| u32::from_be_bytes(x.try_into().unwrap()))
                .ok_or_else(|| err!("register section: read past the end"))
        };
        if g(0)? != MAGIC || g(4)? != VERSION {
            return Err(err!("register section: magic or version"));
        }
        let mut reg = Register {
            device: g(8)?,
            ..Default::default()
        };
        let n = g(12)? as usize;
        let mut at = HEADER;
        let txt = |off: usize, len: usize| -> Result<String> {
            let f = b
                .get(off..off + len)
                .ok_or_else(|| err!("register section: text past the end"))?;
            let l = f.iter().position(|c| *c == 0).unwrap_or(len);
            if f[l..].iter().any(|c| *c != 0) || l == len {
                return Err(err!("register section: text field not NUL-terminated"));
            }
            String::from_utf8(f[..l].to_vec()).map_err(|_| err!("register section: text"))
        };
        for _ in 0..n {
            let (kind, len) = (g(at)?, g(at + 4)? as usize);
            let rec = at + 8;
            match (kind, len) {
                (KIND_PARAM, LEN_PARAM) => reg.params.push(Param {
                    index: g(rec)?,
                    template: g(rec + 4)?,
                    slot: g(rec + 8)?,
                    min: g(rec + 12)? as i32,
                    max: g(rec + 16)? as i32,
                    default: g(rec + 20)? as i32,
                    name: txt(rec + 24, PARAM_NAME)?,
                    short: txt(rec + 24 + PARAM_NAME, PARAM_SHORT)?,
                }),
                (KIND_MACHINE, LEN_MACHINE) => {
                    let mut knobs = [0u32; KNOBS];
                    for (j, k) in knobs.iter_mut().enumerate() {
                        *k = g(rec + 20 + 4 * j)?;
                    }
                    reg.machines.push(Machine {
                        type_no: g(rec)?,
                        clone_of: g(rec + 4)?,
                        position: g(rec + 8)?,
                        own_dsp: g(rec + 12)?,
                        level: g(rec + 16)?,
                        knobs,
                        name: txt(rec + 52, MACHINE_NAME)?,
                        short: txt(rec + 52 + MACHINE_NAME, MACHINE_NAME)?,
                    })
                }
                (KIND_PAGE, LEN_PAGE) => {
                    let mut knobs = [0u32; KNOBS];
                    for (j, k) in knobs.iter_mut().enumerate() {
                        *k = g(rec + 4 + 4 * j)?;
                    }
                    reg.pages.push(Page {
                        type_no: g(rec)?,
                        knobs,
                        level: g(rec + 36)?,
                        name: txt(rec + 40, MACHINE_NAME)?,
                        title: txt(rec + 40 + MACHINE_NAME, MACHINE_NAME)?,
                    })
                }
                _ => return Err(err!("register section: record kind {kind} length {len}")),
            }
            at += len;
        }
        if at != b.len() {
            return Err(err!(
                "register section: {} bytes after the records",
                b.len() - at
            ));
        }
        reg.check()?;
        Ok(reg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Register {
        Register {
            device: 1,
            params: vec![Param {
                index: 0,
                template: 0xd1,
                slot: 1,
                min: 0,
                max: 0x7f00,
                default: 0x4000,
                name: "Decay".into(),
                short: "DECAY".into(),
            }],
            machines: vec![Machine {
                type_no: 16,
                clone_of: 6,
                position: POS_END,
                own_dsp: 0,
                level: 10,
                knobs: [0xf8, KNOB_PARAM, 0xfa, 0xfb, 0xfc, 0xfd, 0, 0xfe],
                name: "XSlice-D".into(),
                short: "XSD".into(),
            }],
            pages: vec![],
        }
    }

    fn with_page() -> Register {
        let mut r = sample();
        r.device = 2;
        r.machines[0].own_dsp = 1;
        r.pages.push(Page {
            type_no: 16,
            knobs: [KNOB_PARAM, 0, 0, 0, 0, 0, 0, 0],
            level: 10,
            name: "SYN".into(),
            title: "Syn".into(),
        });
        r
    }

    #[test]
    fn encode_parse_round_trip() {
        let r = sample();
        let b = r.encode();
        assert_eq!(b.len(), HEADER + LEN_PARAM + LEN_MACHINE);
        assert_eq!(Register::parse(&b).unwrap(), r);
    }

    #[test]
    fn page_round_trip() {
        let r = with_page();
        let b = r.encode();
        assert_eq!(b.len(), HEADER + LEN_PARAM + LEN_MACHINE + LEN_PAGE);
        assert_eq!(Register::parse(&b).unwrap(), r);
        let mut bad = with_page();
        bad.pages[0].type_no = 17;
        assert!(bad.check().unwrap_err().0.contains("no machine of type"));
        let mut bad = with_page();
        bad.pages[0].knobs[2] = 0xd5;
        assert!(bad.check().is_err());
    }

    #[test]
    fn mistakes_are_refused() {
        let mut r = sample();
        r.machines[0].knobs[1] = KNOB_PARAM | 3;
        assert!(r.check().unwrap_err().0.contains("missing param"));
        let mut r = sample();
        r.machines[0].name = "x".repeat(24);
        assert!(r.check().is_err());
        let mut b = sample().encode();
        b.push(0);
        assert!(Register::parse(&b).is_err());
        let mut b = sample().encode();
        b[0] ^= 1;
        assert!(Register::parse(&b).is_err());
    }
}
