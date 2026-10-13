//! `core/machines`: add machine types to the ColdFire MAIN OS (Digitakt II).
//!
//! Generalises the nine parts of `tools/machinepatch.py` (list, dispatch, group, name, rank, permit, hint, pertype,
//! clone; findings 02) from one added type to N, and adds the persistence immediate of the sound loader. Content
//! modules declare `[[machine]]` entries; this module assigns the type numbers `stock count, stock count + 1, ...`
//! in canonical module order and generates, **at patch time from the image itself**:
//!
//! * one relocatable object placed in `cf.expansion`: the dispatch, rank and clone shims (our code), the descriptor
//!   array, name strings, and the relocated copies of the stock tables (list, rank pairs, display names, permission
//!   masks, per-type bytes) with one extra row per added type. The copies are made from the image being patched and
//!   never stored anywhere;
//! * the patches that repoint the firmware's sites at that object and raise every bound.
//!
//! The sites are the anchors `cf.machines.*` (rules in `modules/core/machines/module.toml`, made by
//! `tools/pk_machine_rules.py`, resolved by `tools/port_resolve.py` into the port). Each patch is checked against the
//! shape of the bytes it replaces, so an image whose code moved differently fails with a message, not a bad boot.
//!
//! The DSP side of a content module binds to its type through the extern symbol `machines.<id>.type`
//! (`apply.rs` resolves it to the assigned number).

use crate::dko::*;
use crate::error::*;
use crate::module::{FieldRef, Library, MachineDef, OpReloc};
use crate::port::Port;
use std::collections::BTreeMap;

pub const FRAMEWORK: &str = "core/machines";
/// The stock type whose `type == N` tests (MANUAL SLICE on Digitakt II) an added clone of it must also match.
pub const CLONE_SITES_OF: u32 = 6;
/// Most added types (`moveq` immediates stay below 128 and the loader's mask keeps types below 255).
pub const MAX_ADDED: usize = 32;

/// The DSP's machine type -> selector table (a literal in `FUN_1c24e9`: `I3 = table`, `S2 = DM(I3, M2)`).
pub const SELECTOR_ANCHOR: &str = "dsp.machines.selector_table";

const NAME_ROW: usize = 12;
const NAME_ACCESSORS: usize = 3;
const PERTYPE_SITES: usize = 6;
const CLONE_SITES: usize = 7;
/// Functions that take the machine type as a stack argument and index a 7-entry table with it.
const TYPEARG_SITES: usize = 1;

pub mod dn2;

/// The sites the Digitone II framework reads (`dn2::`): no sort comparator, per-type byte table, clone tests or DSP
/// selector table; a group helper of another shape and functions that index per-type tables with the type.
pub fn anchor_names_for(device: &str) -> Vec<String> {
    if device == "dn2" {
        dn2::anchor_names()
    } else {
        anchor_names()
    }
}

/// Every anchor the DT2 framework reads.
pub fn anchor_names() -> Vec<String> {
    let mut v: Vec<String> = [
        "dispatch",
        "list",
        "list_alloc",
        "group",
        "rank",
        "permit",
        "persist",
    ]
    .iter()
    .map(|n| format!("cf.machines.{n}"))
    .collect();
    v.extend((0..NAME_ACCESSORS).map(|i| format!("cf.machines.name.{i}")));
    v.extend((0..PERTYPE_SITES).map(|i| format!("cf.machines.pertype.{i}")));
    v.extend((0..CLONE_SITES).map(|i| format!("cf.machines.clone.{i}")));
    v.extend((0..TYPEARG_SITES).map(|i| format!("cf.machines.typearg.{i}")));
    v.push(SELECTOR_ANCHOR.into());
    v
}

/// Anchors the port lacks (empty: the framework can run on it).
pub fn missing_anchors(port: &Port) -> Vec<String> {
    anchor_names_for(&port.port.device)
        .into_iter()
        .filter(|n| port.anchor(n).is_none())
        .collect()
}

/// A machine with the module that declared it.
#[derive(Clone, Debug)]
pub struct Declared {
    pub module: String,
    pub def: MachineDef,
    /// the nine descriptor ID fields with parameter names resolved to codes
    pub fields: Vec<u32>,
}

/// The machines of the selected modules in canonical order (the order of `order`, then declaration order).
pub fn declared(
    lib: &Library,
    order: &[String],
    params: &BTreeMap<String, u32>,
    device: &str,
) -> Result<Vec<Declared>> {
    let mut out: Vec<Declared> = vec![];
    for id in order {
        let m = lib.get(id).ok_or_else(|| err!("unknown module {id}"))?;
        for d in m
            .manifest
            .machine
            .iter()
            .filter(|d| d.devices.is_empty() || d.devices.iter().any(|x| x == device))
        {
            if let Some(prev) = out.iter().find(|p| p.def.id == d.id) {
                return Err(err!(
                    "machine id {} is declared by both {} and {id}",
                    d.id,
                    prev.module
                ));
            }
            let mut fields = vec![];
            for f in &d.fields {
                fields.push(match f {
                    FieldRef::Code(c) => *c,
                    FieldRef::Param(p) => *params.get(p).ok_or_else(|| {
                        err!(
                            "machine {} of {id}: field {p:?} is not a parameter of any selected module (add its module)",
                            d.id
                        )
                    })?,
                });
            }
            out.push(Declared {
                module: id.clone(),
                def: d.clone(),
                fields,
            });
        }
    }
    Ok(out)
}

fn ascii_text(what: &str, s: &str) -> Result<()> {
    if s.is_empty() || !s.is_ascii() || s.contains('\0') {
        return Err(err!("machine {what} {s:?} must be non-empty ASCII"));
    }
    Ok(())
}

pub fn validate(d: &MachineDef) -> Result<()> {
    let mut chars = d.id.chars();
    let ok = chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
    if !ok {
        return Err(err!("machine id {:?} must match [a-z][a-z0-9_]*", d.id));
    }
    ascii_text("name", &d.name)?;
    ascii_text("short", &d.short)?;
    if let Some(n) = &d.desc_name {
        ascii_text("desc_name", n)?;
    }
    if let Some(n) = &d.desc_short {
        ascii_text("desc_short", n)?;
    }
    if d.fields.len() != 9 {
        return Err(err!(
            "machine {}: fields needs the 9 descriptor ID fields, got {}",
            d.id,
            d.fields.len()
        ));
    }
    Ok(())
}

/// A change of firmware bytes at `anchor` + `at`, with relocations against the generated object.
#[derive(Clone, Debug)]
pub struct GenPatch {
    pub anchor: String,
    pub at: u64,
    pub bytes: Vec<u8>,
    pub relocs: Vec<OpReloc>,
    pub what: String,
}

pub struct Generated {
    /// (object, region it is placed in)
    pub objects: Vec<(Dko, &'static str)>,
    pub patches: Vec<GenPatch>,
    /// (machine id, type number, declaring module)
    pub types: Vec<(String, u32, String)>,
}

// ----- a tiny ColdFire assembler: only what the shims need ---------------------------------------------------------

#[derive(Default)]
pub(crate) struct Asm {
    pub(crate) code: Vec<u8>,
    pub(crate) relocs: Vec<DkoReloc>,
    pub(crate) syms: Vec<(String, usize)>,
    pub(crate) labels: BTreeMap<String, usize>,
    pub(crate) fixups: Vec<(usize, String)>,
    pub(crate) section: String,
}

impl Asm {
    pub(crate) fn new(section: &str) -> Asm {
        Asm {
            section: section.into(),
            ..Default::default()
        }
    }
    pub(crate) fn w16(&mut self, v: u16) {
        self.code.extend_from_slice(&v.to_be_bytes());
    }
    pub(crate) fn w32(&mut self, v: u32) {
        self.code.extend_from_slice(&v.to_be_bytes());
    }
    pub(crate) fn bytes(&mut self, b: &[u8]) {
        self.code.extend_from_slice(b);
    }
    /// A 32-bit absolute reference to `symbol + addend`.
    pub(crate) fn abs32(&mut self, symbol: &str, addend: i64) {
        self.relocs.push(DkoReloc {
            section: self.section.clone(),
            offset: self.code.len() as u64,
            rtype: "cf.32".into(),
            symbol: symbol.into(),
            addend,
        });
        self.w32(0);
    }
    pub(crate) fn align(&mut self, a: usize) {
        while !self.code.len().is_multiple_of(a) {
            self.code.push(0);
        }
    }
    pub(crate) fn sym(&mut self, name: &str) {
        self.syms.push((name.into(), self.code.len()));
    }
    pub(crate) fn label(&mut self, name: &str) {
        self.labels.insert(name.into(), self.code.len());
    }
    /// `Bcc.w label` (`cc`: 4 cc, 5 cs, 6 ne, 7 eq)
    pub(crate) fn bcc_w(&mut self, cc: u16, label: &str) {
        self.w16(0x6000 | (cc << 8));
        self.fixups.push((self.code.len(), label.into()));
        self.w16(0);
    }
    pub(crate) fn jmp(&mut self, symbol: &str, addend: i64) {
        self.w16(0x4ef9);
        self.abs32(symbol, addend);
    }
    pub(crate) fn jmp_const(&mut self, target: u32) {
        self.w16(0x4ef9);
        self.w32(target);
    }
    pub(crate) fn moveq(&mut self, reg: u16, imm: i8) {
        self.w16(0x7000 | (reg << 9) | (imm as u8 as u16));
    }
    pub(crate) fn cmpi_l(&mut self, reg: u16, imm: u32) {
        self.w16(0x0c80 | reg);
        self.w32(imm);
    }
    /// Resolve the branch displacements; the extension word of `Bcc.w` is the base.
    pub(crate) fn finish(&mut self) -> Result<()> {
        for (at, label) in std::mem::take(&mut self.fixups) {
            let target = *self
                .labels
                .get(&label)
                .ok_or_else(|| err!("machines: assembler label {label} is not defined"))?;
            let d = target as i64 - at as i64;
            let d16 = i16::try_from(d).map_err(|_| err!("machines: branch to {label} too far"))?;
            self.code[at..at + 2].copy_from_slice(&d16.to_be_bytes());
        }
        Ok(())
    }
}

pub(crate) fn be32(b: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

pub(crate) fn expect(what: &str, got: &[u8], want: &[u8]) -> Result<()> {
    if got != want {
        return Err(err!(
            "machines: {what}: the bytes are {} but the framework expects {}",
            hex(got),
            hex(want)
        ));
    }
    Ok(())
}

pub(crate) fn rel(offset: u64, symbol: &str, addend: i64) -> OpReloc {
    OpReloc {
        offset,
        rtype: "cf.32".into(),
        symbol: symbol.into(),
        addend,
    }
}

pub(crate) type Reader<'a> = &'a dyn Fn(u64, u64) -> Result<Vec<u8>>;

struct Site {
    name: String,
    addr: u64,
    bytes: Vec<u8>,
}

fn site(port: &Port, read: Reader, name: &str) -> Result<Site> {
    let a = port
        .anchor(name)
        .ok_or_else(|| err!("port {} has no anchor {name}", port.id()))?;
    Ok(Site {
        name: name.into(),
        addr: a.addr,
        bytes: read(a.addr, a.len)?,
    })
}

/// A decoded `type == SLICE` test.
enum CloneTest {
    /// `<moveq; cmp.l Dy,Dx>; beq/bne`: replay the prefix, also match the added types in the register Dx.
    Eq {
        prefix: Vec<u8>,
        reg: u16,
        matched: u32,
        unmatched: u32,
        len: usize,
    },
    /// `moveq #-3,D1; and.l D1,D0; moveq #4,D1; cmp.l D0,D1; beq/bne`
    Mask {
        matched: u32,
        unmatched: u32,
        len: usize,
    },
}

const MASK_HEAD: [u8; 8] = [0x72, 0xfd, 0xc0, 0x81, 0x72, 0x04, 0xb2, 0x80];

fn decode_clone(s: &Site) -> Result<CloneTest> {
    let b = &s.bytes;
    if b.len() < 6 {
        return Err(err!("machines: {} is {} bytes, need 6", s.name, b.len()));
    }
    // `<instructions>; cmp.l Dy,Dx; beq/bne`: the compare is the first `cmp.l Dy,Dx` that a beq/bne follows
    let (plen, mask) = if b[..8.min(b.len())] == MASK_HEAD {
        (8, true)
    } else {
        let at = (0..b.len().saturating_sub(3))
            .step_by(2)
            .find(|&k| {
                u16::from_be_bytes([b[k], b[k + 1]]) & 0xf1f8 == 0xb080
                    && matches!(b[k + 2], 0x66 | 0x67)
            })
            .ok_or_else(|| {
                err!(
                    "machines: {} at {:#x} is not a cmp.l Dy,Dx test ({})",
                    s.name,
                    s.addr,
                    hex(b)
                )
            })?;
        (at + 2, false)
    };
    let at = s.addr + plen as u64;
    let (op, d8) = (b[plen], b[plen + 1]);
    if (op != 0x66 && op != 0x67) || d8 == 0xff {
        return Err(err!(
            "machines: {} at {:#x}: no beq/bne.b/.w after the compare ({})",
            s.name,
            s.addr,
            hex(&b[plen..plen + 2])
        ));
    }
    let (size, target) = if d8 == 0 {
        let disp = i16::from_be_bytes([b[plen + 2], b[plen + 3]]) as i64;
        (4, (at as i64 + 2 + disp) as u32)
    } else {
        let d = if d8 >= 0x80 {
            d8 as i64 - 256
        } else {
            d8 as i64
        };
        (2, (at as i64 + 2 + d) as u32)
    };
    let end = (at + size as u64) as u32;
    let (matched, unmatched) = if op == 0x67 {
        (target, end)
    } else {
        (end, target)
    };
    let len = plen + size;
    if len < 6 {
        return Err(err!(
            "machines: {}: {len} bytes cannot hold a jmp.l",
            s.name
        ));
    }
    if b.len() != len {
        return Err(err!(
            "machines: {}: the anchor is {} bytes, the test is {len}",
            s.name,
            b.len()
        ));
    }
    if mask {
        Ok(CloneTest::Mask {
            matched,
            unmatched,
            len,
        })
    } else {
        let cmp = u16::from_be_bytes([b[plen - 2], b[plen - 1]]);
        Ok(CloneTest::Eq {
            prefix: b[..plen].to_vec(),
            reg: cmp & 7,
            matched,
            unmatched,
            len,
        })
    }
}

/// Everything the framework changes, for the machines `defs` (canonical order), read from the image through `read`.
pub fn generate(
    port: &Port,
    read: Reader,
    read_sharc: Reader,
    defs: &[Declared],
) -> Result<Generated> {
    let n = defs.len();
    if n == 0 {
        return Err(err!("machines: nothing to generate"));
    }
    let dn2 = port.port.device == "dn2";
    if n > MAX_ADDED {
        return Err(err!("machines: {n} added types, at most {MAX_ADDED}"));
    }
    let mut asm = Asm::new("machfw.text");
    let mut data = Asm::new("machfw.data");
    let mut patches: Vec<GenPatch> = vec![];

    // Stock type count: the dispatch begins `moveq #count-1,D1; move.l 4(sp),D0`.
    let disp = site(port, read, "cf.machines.dispatch")?;
    expect(
        "dispatch head",
        &disp.bytes[2..6],
        &[0x20, 0x2f, 0x00, 0x04],
    )?;
    if disp.bytes[0] != 0x72 {
        return Err(err!("machines: dispatch does not begin with moveq #N,D1"));
    }
    let last = disp.bytes[1] as u32;
    let count = last + 1;
    for d in defs {
        validate(&d.def)?;
        if d.def.clone_of >= count {
            return Err(err!(
                "machine {}: clone_of {} is not a stock type (0..{last})",
                d.def.id,
                d.def.clone_of
            ));
        }
    }
    if (last as usize + n) > 120 {
        return Err(err!("machines: {n} added types exceed a moveq immediate"));
    }
    let new_last = (last as usize + n) as u8;
    let types: Vec<(String, u32, String)> = defs
        .iter()
        .enumerate()
        .map(|(i, d)| (d.def.id.clone(), count + i as u32, d.module.clone()))
        .collect();

    // ---- list: the order the machines are shown in
    let list = site(port, read, "cf.machines.list")?;
    expect("list builder pea", &list.bytes[0..2], &[0x48, 0x79])?;
    expect(
        "list builder",
        &list.bytes[6..12],
        &[0x41, 0xe8, 0x00, count as u8 * 4, 0x48, 0x79],
    )?;
    let (list_end, list_start) = (be32(&list.bytes, 2) as u64, be32(&list.bytes, 12) as u64);
    if list_end < list_start || (list_end - list_start) / 4 != count as u64 {
        return Err(err!(
            "machines: the list source holds {} entries, the dispatch says {count} types",
            (list_end.wrapping_sub(list_start)) / 4
        ));
    }
    let stock_list = read(list_start, count as u64 * 4)?;
    let mut order: Vec<u32> = (0..count as usize)
        .map(|i| be32(&stock_list, i * 4))
        .collect();
    for (i, d) in defs.iter().enumerate() {
        let pos = d.def.position.unwrap_or(order.len()).min(order.len());
        order.insert(pos, count + i as u32);
    }
    let total = (count as usize) + n;
    data.align(4);
    data.sym("machfw_list");
    for t in &order {
        data.w32(*t);
    }
    // The builder allocates `pea 0x1c.w` bytes for the stock list and sets the vector's end to `lea 0x1c(A0),A0`:
    // both must grow with the list, or the copy overruns the allocation.
    let alloc = site(port, read, "cf.machines.list_alloc")?;
    expect(
        "list allocation",
        &alloc.bytes,
        &[0x48, 0x78, 0x00, count as u8 * 4],
    )?;
    expect(
        "list vector end",
        &list.bytes[6..10],
        &[0x41, 0xe8, 0x00, count as u8 * 4],
    )?;
    let list_bytes = 4 * total as u16;
    patches.push(GenPatch {
        anchor: alloc.name.clone(),
        at: 2,
        bytes: list_bytes.to_be_bytes().to_vec(),
        relocs: vec![],
        what: "list allocation".into(),
    });
    patches.push(GenPatch {
        anchor: list.name.clone(),
        at: 8,
        bytes: list_bytes.to_be_bytes().to_vec(),
        relocs: vec![],
        what: "list vector end".into(),
    });
    patches.push(GenPatch {
        anchor: list.name.clone(),
        at: 2,
        bytes: vec![0; 4],
        relocs: vec![rel(0, "machfw_list", 4 * total as i64)],
        what: "list end".into(),
    });
    patches.push(GenPatch {
        anchor: list.name.clone(),
        at: 12,
        bytes: vec![0; 4],
        relocs: vec![rel(0, "machfw_list", 0)],
        what: "list start".into(),
    });

    if !dn2 {
        // ---- rank: the sort comparator's std::map gets (type, display position) pairs
        let rank = site(port, read, "cf.machines.rank")?;
        expect("rank insert jsr", &rank.bytes[0..2], &[0x4e, 0xb9])?;
        let rank_insert = be32(&rank.bytes, 2);
        data.sym("machfw_rank_table");
        for (i, t) in order.iter().enumerate() {
            data.w32(*t);
            data.w32(i as u32);
        }
        asm.align(2);
        asm.sym("machfw_rank_shim");
        // entered by the repointed jsr: 4(sp) is the map, 8(sp) begin, 0xc(sp) end (the caller discards both afterwards).
        // ColdFire instructions are at most 6 bytes, so `move.l #imm,d16(sp)` does not exist: go through D0, which a
        // callee may clobber.
        asm.w16(0x203c); // move.l #table,D0
        asm.abs32("machfw_rank_table", 0);
        asm.w16(0x2f40); // move.l D0,8(sp)
        asm.w16(0x0008);
        asm.w16(0x203c); // move.l #end,D0
        asm.abs32("machfw_rank_table", 8 * total as i64);
        asm.w16(0x2f40); // move.l D0,0xc(sp)
        asm.w16(0x000c);
        asm.jmp_const(rank_insert);
        patches.push(GenPatch {
            anchor: rank.name.clone(),
            at: 2,
            bytes: vec![0; 4],
            relocs: vec![rel(0, "machfw_rank_shim", 0)],
            what: "rank insert call".into(),
        });
    }

    if !dn2 {
        // ---- group: the UI group of a type (SLICE and its clones are group 1)
        let group = site(port, read, "cf.machines.group")?;
        expect(
            "group bound",
            &group.bytes,
            &[0x72, last as u8, 0xb2, 0x80, 0x66, 0x04],
        )?;
        patches.push(GenPatch {
            anchor: group.name.clone(),
            at: 0,
            bytes: vec![0x72, new_last, 0xb2, 0x80, 0x65, 0x04],
            relocs: vec![],
            what: "group bound".into(),
        });
    }

    if dn2 {
        dn2::group(port, read, new_last, &mut patches, &mut asm)?;
    }

    // ---- dispatch: descriptors of the added types
    asm.align(2);
    asm.sym("machfw_dispatch");
    asm.w16(0x202f);
    asm.w16(0x0004); // move.l 4(sp),D0
    asm.moveq(1, new_last as i8);
    asm.w16(0xb280); // cmp.l D0,D1
    asm.bcc_w(5, "stock"); // type above the last added: the stock code decides (fallback descriptor)
    asm.moveq(1, last as i8);
    asm.w16(0xb280);
    asm.bcc_w(4, "stock"); // a stock type
    asm.w16(0x0480);
    asm.w32(count); // subi.l #count,D0
    asm.moveq(1, 0x2c);
    asm.w16(0x4c01);
    asm.w16(0x0800); // muls.l D1,D0
    asm.w16(0x0680);
    asm.abs32("machfw_desc", 0); // addi.l #array,D0
    asm.w16(0x4e75);
    asm.label("stock");
    asm.moveq(1, last as i8);
    asm.jmp("cf.machines.dispatch", 6);
    patches.push(GenPatch {
        anchor: disp.name.clone(),
        at: 0,
        bytes: vec![0x4e, 0xf9, 0, 0, 0, 0],
        relocs: vec![rel(2, "machfw_dispatch", 0)],
        what: "dispatch entry".into(),
    });

    // descriptors: [long name chars, short name chars, 9 field IDs], stride 0x2c as the stock array
    data.align(4);
    data.sym("machfw_desc");
    for (i, d) in defs.iter().enumerate() {
        data.abs32(&format!("machfw_dn_{i}"), 0);
        data.abs32(&format!("machfw_ds_{i}"), 0);
        for f in &d.fields {
            data.w32(*f);
        }
    }
    debug_assert_eq!(data.code.len() % 4, 0);
    for (i, d) in defs.iter().enumerate() {
        let long = d
            .def
            .desc_name
            .clone()
            .unwrap_or_else(|| d.def.name.to_ascii_uppercase());
        let short = d
            .def
            .desc_short
            .clone()
            .unwrap_or_else(|| d.def.short.to_ascii_uppercase());
        for (sym, s) in [
            (format!("machfw_dn_{i}"), long),
            (format!("machfw_ds_{i}"), short),
        ] {
            // a std::string representation: length, capacity, refcount -1 (never freed), then the characters
            data.align(4);
            data.w32(s.len() as u32);
            data.w32(s.len() as u32);
            data.w32(u32::MAX);
            data.sym(&sym);
            data.bytes(s.as_bytes());
            data.bytes(&[0]);
        }
    }

    // ---- name: the display-name table (long, short, header hint per type)
    let mut names = vec![];
    for i in 0..NAME_ACCESSORS {
        names.push(site(port, read, &format!("cf.machines.name.{i}"))?);
    }
    // each accessor: moveq #last,D1; move.l 4(sp),D0; cmp.l D0,D1; bcs.b ..; <lea table / addi.l #table+col,D0>
    let mut operand_at = vec![];
    for s in &names {
        expect(
            &format!("{} head", s.name),
            &s.bytes[0..8],
            &[0x72, last as u8, 0x20, 0x2f, 0x00, 0x04, 0xb2, 0x80],
        )?;
        let off = if s.bytes[0x10..0x12] == [0x41, 0xf9] {
            0x12usize
        } else if s.bytes[0x12..0x14] == [0x06, 0x80] {
            0x14
        } else {
            return Err(err!("machines: {}: no lea/addi of the name table", s.name));
        };
        operand_at.push((off, be32(&s.bytes, off)));
    }
    let base = operand_at.iter().map(|(_, v)| *v).min().unwrap();
    let stock_names = read(base as u64, count as u64 * NAME_ROW as u64)?;
    data.align(4);
    data.sym("machfw_name_table");
    data.bytes(&stock_names);
    for (i, d) in defs.iter().enumerate() {
        data.abs32(&format!("machfw_nl_{i}"), 0);
        data.abs32(&format!("machfw_ns_{i}"), 0);
        let c = d.def.clone_of as usize;
        data.bytes(&stock_names[c * NAME_ROW + 8..c * NAME_ROW + 12]); // the clone's header hint
    }
    for (i, d) in defs.iter().enumerate() {
        data.sym(&format!("machfw_nl_{i}"));
        data.bytes(d.def.name.as_bytes());
        data.bytes(&[0]);
        data.sym(&format!("machfw_ns_{i}"));
        data.bytes(d.def.short.as_bytes());
        data.bytes(&[0]);
    }
    for (s, (off, value)) in names.iter().zip(&operand_at) {
        let col = value - base;
        if ![0, 4, 8].contains(&col) {
            return Err(err!("machines: {}: column {col} of the name table", s.name));
        }
        patches.push(GenPatch {
            anchor: s.name.clone(),
            at: 1,
            bytes: vec![new_last],
            relocs: vec![],
            what: "name bound".into(),
        });
        patches.push(GenPatch {
            anchor: s.name.clone(),
            at: *off as u64,
            bytes: vec![0; 4],
            relocs: vec![rel(0, "machfw_name_table", col as i64)],
            what: "name table".into(),
        });
    }

    // ---- permit: the per-type permission mask table
    let permit = site(port, read, "cf.machines.permit")?;
    expect(
        "permit head",
        &permit.bytes[0..4],
        &[0x2f, 0x02, 0x74, last as u8],
    )?;
    expect("permit lea", &permit.bytes[0x18..0x1a], &[0x41, 0xf9])?;
    let permit_table = be32(&permit.bytes, 0x1a) as u64;
    let stock_permit = read(permit_table, count as u64 * 4)?;
    data.align(4);
    data.sym("machfw_permit");
    data.bytes(&stock_permit);
    for d in defs {
        let c = d.def.clone_of as usize;
        data.bytes(&stock_permit[c * 4..c * 4 + 4]);
    }
    patches.push(GenPatch {
        anchor: permit.name.clone(),
        at: 3,
        bytes: vec![new_last],
        relocs: vec![],
        what: "permit bound".into(),
    });
    patches.push(GenPatch {
        anchor: permit.name.clone(),
        at: 0x1a,
        bytes: vec![0; 4],
        relocs: vec![rel(0, "machfw_permit", 0)],
        what: "permit table".into(),
    });

    if !dn2 {
        // ---- pertype: a byte per type behind six bounds
        let mut pertype_table: Option<u32> = None;
        let mut pt_patches = vec![];
        for i in 0..PERTYPE_SITES {
            let s = site(port, read, &format!("cf.machines.pertype.{i}"))?;
            if s.bytes[0] & 0xf1 != 0x70 || s.bytes[1] != last as u8 {
                return Err(err!(
                    "machines: {}: expected moveq #{last},Dn, got {}",
                    s.name,
                    hex(&s.bytes[0..2])
                ));
            }
            // candidate leas of an absolute table address
            let cands: Vec<(usize, u32)> = (2..s.bytes.len().saturating_sub(5))
                .step_by(2)
                .filter(|&o| u16::from_be_bytes([s.bytes[o], s.bytes[o + 1]]) & 0xf1ff == 0x41f9)
                .map(|o| (o, be32(&s.bytes, o + 2)))
                .collect();
            let pick = match pertype_table {
                None if cands.len() == 1 => cands[0],
                None => return Err(err!("machines: {}: {} lea candidates", s.name, cands.len())),
                Some(t) => {
                    let m: Vec<_> = cands.iter().filter(|c| c.1 == t).collect();
                    if m.len() != 1 {
                        return Err(err!(
                            "machines: {}: no single lea of the per-type table",
                            s.name
                        ));
                    }
                    *m[0]
                }
            };
            pertype_table = Some(pick.1);
            pt_patches.push((s.name.clone(), pick.0));
        }
        let pertype_table = pertype_table.unwrap() as u64;
        let stock_pt = read(pertype_table, count as u64)?;
        data.sym("machfw_pertype");
        data.bytes(&stock_pt);
        for d in defs {
            data.bytes(&[stock_pt[d.def.clone_of as usize]]);
        }
        data.align(4);
        for (name, lea) in pt_patches {
            patches.push(GenPatch {
                anchor: name.clone(),
                at: 1,
                bytes: vec![new_last],
                relocs: vec![],
                what: "per-type bound".into(),
            });
            patches.push(GenPatch {
                anchor: name,
                at: lea as u64 + 2,
                bytes: vec![0; 4],
                relocs: vec![rel(0, "machfw_pertype", 0)],
                what: "per-type table".into(),
            });
        }
    }

    if !dn2 {
        // ---- clone: every `type == SLICE` test also accepts the added clones of SLICE; the mask test maps added types to their
        // clone_of first
        let eq_types: Vec<u32> = types
            .iter()
            .zip(defs)
            .filter(|(_, d)| d.def.clone_of == CLONE_SITES_OF)
            .map(|((_, t, _), _)| *t)
            .collect();
        for i in 0..CLONE_SITES {
            let s = site(port, read, &format!("cf.machines.clone.{i}"))?;
            let c = decode_clone(&s)?;
            let (len, label) = match c {
                CloneTest::Eq {
                    prefix,
                    reg,
                    matched,
                    unmatched,
                    len,
                } => {
                    if eq_types.is_empty() {
                        continue;
                    }
                    asm.align(2);
                    asm.sym(&format!("machfw_clone_{i}"));
                    let m = format!("m{i}");
                    asm.bytes(&prefix);
                    asm.bcc_w(7, &m);
                    for t in &eq_types {
                        asm.cmpi_l(reg, *t);
                        asm.bcc_w(7, &m);
                    }
                    asm.jmp_const(unmatched);
                    asm.label(&m);
                    asm.jmp_const(matched);
                    (len, format!("machfw_clone_{i}"))
                }
                CloneTest::Mask {
                    matched,
                    unmatched,
                    len,
                } => {
                    asm.align(2);
                    asm.sym(&format!("machfw_clone_{i}"));
                    let m = format!("m{i}");
                    for (k, d) in defs.iter().enumerate() {
                        asm.cmpi_l(0, count + k as u32);
                        asm.w16(0x6602); // bne.b +2
                        asm.moveq(0, d.def.clone_of as i8);
                    }
                    asm.bytes(&MASK_HEAD);
                    asm.bcc_w(7, &m);
                    asm.jmp_const(unmatched);
                    asm.label(&m);
                    asm.jmp_const(matched);
                    (len, format!("machfw_clone_{i}"))
                }
            };
            let mut bytes = vec![0x4e, 0xf9, 0, 0, 0, 0];
            bytes.resize(len, 0);
            for k in (6..len).step_by(2) {
                bytes[k] = 0x4e;
                bytes[k + 1] = 0x71;
            }
            patches.push(GenPatch {
                anchor: s.name.clone(),
                at: 0,
                bytes,
                relocs: vec![rel(2, &label, 0)],
                what: "clone test".into(),
            });
        }

        // ---- typearg: `FUN_400da3a2(idx, type, filter)` answers which parameter code a (mirror index, machine type) pair
        // stands for: the owner-keyed map built at boot from the parameter table (7 machine types x 10 mirror slots). It
        // feeds the default values of a new sound and machine change, parameter change events and the parameter lookup
        // of the engine slots. An added type answers with its own descriptor's field for the slot (0 where the field is
        // empty), so the defaults, ranges and names of the machine's own rows apply (core/params); the two slots without
        // a descriptor field (29, 30) answer as the clone does, and every other index falls through to the stock code.
        // The shim runs between `movea.l 4(sp),A0` (idx) and `movea.l 0xc(sp),A1`.
        for i in 0..TYPEARG_SITES {
            let s = site(port, read, &format!("cf.machines.typearg.{i}"))?;
            expect(
                &format!("{} loads", s.name),
                &[s.bytes[0], s.bytes[1], s.bytes[4], s.bytes[5]],
                &[0x20, 0x2f, 0x22, 0x6f],
            )?;
            if s.bytes.len() != 8 {
                return Err(err!(
                    "machines: {} is {} bytes, need 8",
                    s.name,
                    s.bytes.len()
                ));
            }
            let before = read(s.addr - 4, 4)?;
            expect(
                &format!("{} index load", s.name),
                &[before[0], before[1]],
                &[0x20, 0x6f],
            )?;
            // the epilogue `move.l (sp)+,D2; movea.l (sp)+,A2; rts` the function ends in
            let tail = read(s.addr + 8, 0x80)?;
            let hits: Vec<usize> = (0..tail.len() - 6)
                .step_by(2)
                .filter(|&k| tail[k..k + 6] == [0x24, 0x1f, 0x24, 0x5f, 0x4e, 0x75])
                .collect();
            if hits.len() != 1 {
                return Err(err!(
                    "machines: {}: {} function epilogues after the site",
                    s.name,
                    hits.len()
                ));
            }
            let epilogue = (s.addr + 8 + hits[0] as u64) as u32;
            asm.align(2);
            asm.sym(&format!("machfw_typearg_{i}"));
            asm.bytes(&s.bytes[0..4]); // move.l 8(sp),D0: the type
            asm.cmpi_l(0, count);
            asm.bcc_w(5, "ta_resume"); // a stock type
            asm.cmpi_l(0, count + n as u32);
            asm.bcc_w(4, "ta_resume"); // not one of ours
            asm.w16(0x2408); // move.l A0,D2
            asm.w16(0x0482);
            asm.w32(25); // subi.l #25,D2
            asm.w16(0x0c82);
            asm.w32(9); // cmpi.l #9,D2
            asm.bcc_w(2, "ta_resume"); // hi: not an engine slot
            asm.w16(0xe58a); // lsl.l #2,D2
            asm.w16(0x0682);
            asm.abs32("machfw_slotmap", 0); // addi.l #slotmap,D2
            asm.w16(0x2442); // movea.l D2,A2
            asm.w16(0x2412); // move.l (A2),D2
            asm.w16(0x4a82); // tst.l D2
            asm.bcc_w(0xd, "ta_clone"); // lt: a slot without a descriptor field (29, 30)
            asm.w16(0x0480);
            asm.w32(count); // subi.l #count,D0
            asm.moveq(1, 0x2c);
            asm.w16(0x4c01);
            asm.w16(0x0800); // muls.l D1,D0
            asm.w16(0xd082); // add.l D2,D0
            asm.w16(0x0680);
            asm.abs32("machfw_desc", 0); // addi.l #desc,D0
            asm.w16(0x2440); // movea.l D0,A2
            asm.w16(0x2012); // move.l (A2),D0
            asm.jmp_const(epilogue);
            // the slots without a field (29 and 30: MANUAL SLICE's `Sample Bank` and `Sample Slot` rows, which the stock
            // map holds for its owner but no descriptor lists) answer as the clone does: the type counts as its clone
            asm.label("ta_clone");
            for (j, d) in defs.iter().enumerate() {
                asm.cmpi_l(0, count + j as u32);
                asm.w16(0x6602); // bne.b +2
                asm.moveq(0, d.def.clone_of as i8);
            }
            asm.label("ta_resume");
            asm.bytes(&s.bytes[4..8]); // movea.l 0xc(sp),A1
            asm.jmp_const((s.addr + 8) as u32);
            let mut bytes = vec![0x4e, 0xf9, 0, 0, 0, 0, 0x4e, 0x71];
            bytes.truncate(8);
            patches.push(GenPatch {
                anchor: s.name.clone(),
                at: 0,
                bytes,
                relocs: vec![rel(2, &format!("machfw_typearg_{i}"), 0)],
                what: "type argument".into(),
            });
        }
        // engine slot of each mirror index 25..34 as the byte offset of its descriptor field (-1: none)
        data.align(4);
        data.sym("machfw_slotmap");
        for off in [8i32, 12, 16, 20, -1, -1, 24, 28, 32, 36] {
            data.w32(off as u32);
        }
    }

    if dn2 {
        dn2::typemaps(port, read, defs, count, &mut asm, &mut patches)?;
        dn2::frametype(port, read, defs, count, &mut asm, &mut patches)?;
        dn2::reader(port, read, defs, count, &mut asm, &mut patches)?;
    }

    // ---- persist: the sound loader keeps a stored machine type when (u8)(type + 1) < imm
    let persist = site(port, read, "cf.machines.persist")?;
    expect(
        "persistence immediate",
        &persist.bytes,
        &[0x74, last as u8 + 2],
    )?;
    patches.push(GenPatch {
        anchor: persist.name.clone(),
        at: 1,
        bytes: vec![last as u8 + 2 + n as u8],
        relocs: vec![],
        what: "persistence bound".into(),
    });

    asm.finish()?;
    data.finish()?;

    // ---- DSP: the type -> selector table, relocated to L2 with one word per added type; the literal that loads its
    // address is repointed. The DSP indexes it with the type from the frame, so a type past the stock table would read
    // other data.
    let selector: Option<Dko> = if dn2 {
        None
    } else {
        let sel = port
            .anchor(SELECTOR_ANCHOR)
            .ok_or_else(|| err!("port {} has no anchor {SELECTOR_ANCHOR}", port.id()))?;
        let sel_src = sel
            .src
            .ok_or_else(|| err!("anchor {SELECTOR_ANCHOR} has no src"))?;
        let sel_insn = read_sharc(sel_src, 6)?;
        let sel_lit = ((u16::from_le_bytes([sel_insn[2], sel_insn[3]]) as u32) << 16)
            | u16::from_le_bytes([sel_insn[4], sel_insn[5]]) as u32;
        if sel_lit as u64 != sel.addr {
            return Err(err!(
                "machines: the selector literal reads {sel_lit:#x}, the anchor says {:#x}",
                sel.addr
            ));
        }
        let stock_sel = read_sharc(
            crate::dsp::SW_ALIAS_BASE as u64 + sel.addr,
            count as u64 * 4,
        )?;
        let stock_word =
            |i: usize| u32::from_le_bytes(stock_sel[i * 4..i * 4 + 4].try_into().unwrap());
        let mut sel_words: Vec<u8> = stock_sel.clone();
        for d in defs {
            let w = d
                .def
                .selector
                .unwrap_or_else(|| stock_word(d.def.clone_of as usize));
            sel_words.extend_from_slice(&w.to_le_bytes());
        }
        let selector = Dko {
            format: FORMAT,
            arch: "sharc".into(),
            source_sha256: sha256_hex(b"core/machines generated"),
            toolchain: BTreeMap::from([("generator".into(), "patchkit machines.rs".into())]),
            sections: vec![DkoSection {
                name: "machfw.sel".into(),
                kind: "data".into(),
                align: 4,
                size: sel_words.len() as u32,
                hex: hex(&sel_words),
            }],
            symbols: vec![DkoSymbol {
                name: "machfw_selector".into(),
                section: Some("machfw.sel".into()),
                value: 0,
                global: true,
            }],
            relocs: vec![],
        };
        patches.push(GenPatch {
            anchor: SELECTOR_ANCHOR.into(),
            at: 0,
            bytes: sel_insn,
            relocs: vec![OpReloc {
                offset: 0,
                rtype: "sharc.abs32".into(),
                symbol: "machfw_selector".into(),
                addend: 0,
            }],
            what: "DSP selector table".into(),
        });
        Some(selector)
    };

    // ---- the objects: the shims and the tables both go to the expansion region (the native core executes from DDR);
    // the flash cave keeps the expansion loader only
    let obj = |sections: Vec<DkoSection>, symbols: Vec<DkoSymbol>, relocs: Vec<DkoReloc>| Dko {
        format: FORMAT,
        arch: "cf".into(),
        source_sha256: sha256_hex(b"core/machines generated"),
        toolchain: BTreeMap::from([("generator".into(), "patchkit machines.rs".into())]),
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
    let mut text_syms: Vec<DkoSymbol> = asm
        .syms
        .iter()
        .map(|(n, o)| sym(n, "machfw.text", *o))
        .collect();
    text_syms.push(DkoSymbol {
        name: "cf.machines.dispatch".into(),
        section: None,
        value: 0,
        global: false,
    });
    let code = obj(
        vec![DkoSection {
            name: "machfw.text".into(),
            kind: "text".into(),
            align: 2,
            size: asm.code.len() as u32,
            hex: hex(&asm.code),
        }],
        text_syms,
        asm.relocs.clone(),
    );
    let tables = obj(
        vec![DkoSection {
            name: "machfw.data".into(),
            kind: "data".into(),
            align: 4,
            size: data.code.len() as u32,
            hex: hex(&data.code),
        }],
        data.syms
            .iter()
            .map(|(n, o)| sym(n, "machfw.data", *o))
            .collect(),
        data.relocs.clone(),
    );
    Ok(Generated {
        objects: vec![(code, "cf.expansion"), (tables, "cf.expansion")]
            .into_iter()
            .chain(selector.map(|d| (d, "dsp.l2")))
            .collect(),
        patches,
        types,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assembler_resolves_forward_branches_from_the_extension_word() {
        let mut a = Asm::new("t");
        a.bcc_w(7, "x"); // 6700 dddd at 0..4, extension word at 2
        a.w16(0x4e71);
        a.label("x");
        a.w16(0x4e75);
        a.finish().unwrap();
        // target 6, extension word at 2: displacement 4
        assert_eq!(a.code, [0x67, 0x00, 0x00, 0x04, 0x4e, 0x71, 0x4e, 0x75]);
    }

    #[test]
    fn moveq_and_cmpi_encodings() {
        let mut a = Asm::new("t");
        a.moveq(1, 6);
        a.moveq(0, 4);
        a.cmpi_l(3, 7);
        assert_eq!(
            a.code,
            [0x72, 0x06, 0x70, 0x04, 0x0c, 0x83, 0x00, 0x00, 0x00, 0x07]
        );
    }

    #[test]
    fn clone_decoder_reads_both_branch_senses_and_the_mask_test() {
        // moveq #6,D1; cmp.l D1,D0; beq.b +0x10 at 0x1000
        let eq = Site {
            name: "x".into(),
            addr: 0x1000,
            bytes: vec![0x72, 0x06, 0xb0, 0x81, 0x67, 0x10],
        };
        match decode_clone(&eq).unwrap() {
            CloneTest::Eq {
                reg,
                matched,
                unmatched,
                len,
                ..
            } => {
                assert_eq!((reg, len), (1, 6));
                assert_eq!(matched, 0x1000 + 4 + 2 + 0x10);
                assert_eq!(unmatched, 0x1006);
            }
            _ => panic!("eq"),
        }
        let ne = Site {
            name: "x".into(),
            addr: 0x1000,
            bytes: vec![0x72, 0x06, 0xb0, 0x81, 0x66, 0x00, 0x00, 0x20],
        };
        match decode_clone(&ne).unwrap() {
            CloneTest::Eq {
                matched,
                unmatched,
                len,
                ..
            } => {
                assert_eq!(len, 8);
                assert_eq!(matched, 0x1008);
                assert_eq!(unmatched, 0x1000 + 4 + 2 + 0x20);
            }
            _ => panic!("eq"),
        }
        let mut m = MASK_HEAD.to_vec();
        m.extend_from_slice(&[0x67, 0x08]);
        let mask = Site {
            name: "x".into(),
            addr: 0x2000,
            bytes: m,
        };
        assert!(matches!(
            decode_clone(&mask).unwrap(),
            CloneTest::Mask { len: 10, .. }
        ));
    }

    #[test]
    fn definitions_are_validated() {
        let mut d = MachineDef {
            id: "xslice".into(),
            name: "XSlice".into(),
            short: "XSL".into(),
            clone_of: 6,
            position: None,
            fields: vec![FieldRef::Code(0); 9],
            selector: None,
            desc_name: None,
            desc_short: None,
            own_dsp: false,
            devices: vec![],
        };
        assert!(validate(&d).is_ok());
        d.id = "X-slice".into();
        assert!(validate(&d).is_err());
        d.id = "ok".into();
        d.fields.pop();
        assert!(validate(&d).unwrap_err().0.contains("9 descriptor"));
    }
}
