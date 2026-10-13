//! Check and apply: resolve the module set, verify the anchors against the firmware, allocate regions,
//! link, apply the operations, repack the changed sections, seal and re-verify.

use crate::dko::Dko;
use crate::dsp::Stream;
use crate::error::*;
use crate::firmware::Firmware;
use crate::link;
use crate::module::{Library, Module, Op};
use crate::port::{Anchor, Port, Registry};
use crate::record::Record;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

pub const SHARC: &str = "sharc";
pub const CF: &str = "cf";

/// One byte-range change, with provenance.
#[derive(Clone, Debug, Serialize)]
pub struct PlanWrite {
    pub cpu: String,
    pub addr: u64,
    pub len: u64,
    /// `patch` (bytes the firmware had), `place` (new memory) or `record`
    pub kind: String,
    pub owner: String,
    pub what: String,
    pub old_sha256: Option<String>,
    pub new_sha256: String,
    pub new_hex: Option<String>,
    /// Executable byte ranges of a placed object (for `explain`)
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub code: Vec<(u64, u64)>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Plan {
    pub base: PlanBase,
    pub modules: Vec<String>,
    pub writes: Vec<PlanWrite>,
    /// region name -> bytes used / capacity
    pub regions: BTreeMap<String, (u64, u64)>,
    pub symbols: BTreeMap<String, u64>,
    /// Machine types the framework assigned: (id, type number, declaring module)
    pub machines: Vec<(String, u32, String)>,
    /// (parameter id, assigned code, declaring module)
    pub params: Vec<(String, u32, String)>,
    pub record: Record,
    pub output: Option<PlanOutput>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PlanBase {
    pub device: String,
    pub version: String,
    pub syx_sha256: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct PlanOutput {
    pub syx_sha256: String,
    pub syx_bytes: u64,
    pub container_bytes: u64,
    pub changed_sections: Vec<(u32, u64, u64)>,
}

pub struct Applied {
    pub syx: Vec<u8>,
    pub plan: Plan,
}

/// Active on this device?
fn on(devices: &[String], dev: &str) -> bool {
    devices.is_empty() || devices.iter().any(|d| d == dev)
}

fn anchor_value(a: &Anchor) -> u64 {
    if a.cpu == SHARC && a.kind == "code" {
        a.pm.unwrap_or(a.addr)
    } else {
        a.addr
    }
}

/// Memory of both CPUs as the firmware loads it.
struct Mem {
    stream: Stream,
    main: Vec<u8>,
    main_base: u64,
    stream_dirty: bool,
    main_dirty: bool,
}

impl Mem {
    fn read(&self, cpu: &str, addr: u64, len: u64) -> Result<Vec<u8>> {
        if cpu == SHARC {
            self.stream
                .read(addr as u32, len as usize)
                .ok_or_else(|| err!("DSP {addr:#x}+{len}: not loaded by the boot stream"))
        } else {
            let o = addr.checked_sub(self.main_base).map(|o| o as usize);
            match o {
                Some(o) if o + len as usize <= self.main.len() => {
                    Ok(self.main[o..o + len as usize].to_vec())
                }
                _ => Err(err!("MAIN {addr:#x}+{len}: outside the MAIN OS image")),
            }
        }
    }
    fn write(&mut self, cpu: &str, addr: u64, data: &[u8]) -> Result<()> {
        if cpu == SHARC {
            self.stream_dirty = true;
            self.stream.write(addr as u32, data)
        } else {
            self.read(cpu, addr, data.len() as u64)?;
            let o = (addr - self.main_base) as usize;
            self.main[o..o + data.len()].copy_from_slice(data);
            self.main_dirty = true;
            Ok(())
        }
    }
}

struct Alloc {
    free: Vec<(u64, u64)>,
    used: u64,
    cap: u64,
}

impl Alloc {
    fn new(ranges: Vec<(u64, u64)>) -> Alloc {
        let cap = ranges.iter().map(|(s, e)| e - s).sum();
        Alloc {
            free: ranges,
            used: 0,
            cap,
        }
    }
    fn take(&mut self, size: u64, align: u64) -> Option<u64> {
        for i in 0..self.free.len() {
            let (s, e) = self.free[i];
            let a = s.div_ceil(align) * align;
            if a + size <= e {
                self.free[i].0 = a + size;
                self.used += a + size - s;
                return Some(a);
            }
        }
        None
    }
}

pub struct Selection {
    pub order: Vec<String>,
}

/// Dependency closure check and canonical order: requires first, then id.
pub fn order_modules(lib: &Library, ids: &[String]) -> Result<Vec<String>> {
    let mut set: BTreeSet<String> = BTreeSet::new();
    for id in ids {
        if lib.get(id).is_none() {
            return Err(err!("unknown module {id}"));
        }
        set.insert(id.clone());
    }
    for id in &set {
        let m = lib.get(id).unwrap();
        for r in &m.manifest.module.requires {
            if !set.contains(r) {
                return Err(err!("{id} requires {r}, which is not selected"));
            }
        }
        for c in &m.manifest.module.conflicts {
            if set.contains(c) {
                return Err(err!("{id} conflicts with {c}"));
            }
        }
    }
    let mut out: Vec<String> = vec![];
    let mut left: BTreeSet<String> = set;
    while !left.is_empty() {
        let next = left
            .iter()
            .find(|id| {
                lib.get(id)
                    .unwrap()
                    .manifest
                    .module
                    .requires
                    .iter()
                    .all(|r| out.contains(r))
            })
            .cloned()
            .ok_or_else(|| err!("requires cycle among {:?}", left))?;
        left.remove(&next);
        out.push(next);
    }
    Ok(out)
}

/// Is the module usable on this port? Lists every missing anchor and region.
pub fn compatible(m: &Module, port: &Port) -> Result<()> {
    let dev = port.port.device.as_str();
    let ports = &m.manifest.module.ports;
    if !ports.is_empty() && !ports.iter().any(|p| *p == port.id()) {
        return Err(err!(
            "{} is not compatible with {}: it is built for {}",
            m.id(),
            port.id(),
            ports.join(", ")
        ));
    }
    let mut missing: Vec<String> = vec![];
    for (o, dko) in m.manifest.object.iter().zip(&m.objects) {
        if !on(&o.devices, dev) {
            continue;
        }
        if port.region(&o.place).is_none() {
            missing.push(format!("region {}", o.place));
        }
        for sy in dko.symbols.iter().filter(|s| s.section.is_none()) {
            let name = o.externs.get(&sy.name).unwrap_or(&sy.name);
            // an export of another object cannot be seen here; only names that look like anchors are checked
            if (name.starts_with("dsp.") || name.starts_with("cf.")) && port.anchor(name).is_none()
            {
                missing.push(format!("anchor {name}"));
            }
        }
    }
    for op in &m.manifest.op {
        if op.op != "register" && on(&op.devices, dev) && port.anchor(&op.anchor).is_none() {
            missing.push(format!("anchor {}", op.anchor));
        }
    }
    if m.id() == crate::machines::FRAMEWORK || !m.manifest.machine.is_empty() {
        for d in &m.manifest.machine {
            if let Err(e) = crate::machines::validate(d) {
                return Err(err!("{}: {e}", m.id()));
            }
        }
        missing.extend(
            crate::machines::missing_anchors(port)
                .into_iter()
                .map(|n| format!("anchor {n}")),
        );
    }
    if m.id() == crate::params::FRAMEWORK || !m.manifest.param.is_empty() {
        if !m.manifest.param.is_empty() && !on(&[String::from("dt2")], dev) {
            missing.push("parameter framework (Digitakt II only)".into());
        }
        for d in &m.manifest.param {
            if let Err(e) = crate::params::validate(d, None) {
                return Err(err!("{}: {e}", m.id()));
            }
        }
        missing.extend(
            crate::params::missing_anchors(port)
                .into_iter()
                .map(|n| format!("anchor {n}")),
        );
    }
    missing.sort();
    missing.dedup();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(err!(
            "{} is not compatible with {}: no {}",
            m.id(),
            port.id(),
            missing.join(", ")
        ))
    }
}

fn verify_anchor(mem: &Mem, a: &Anchor) -> Result<()> {
    let at = if a.kind == "value" {
        a.src
            .ok_or_else(|| err!("anchor {}: value without src", a.name))?
    } else {
        a.addr
    };
    let bytes = mem.read(&a.cpu, at, a.len)?;
    let got = sha256_hex(&bytes);
    if got != a.sha256 {
        return Err(err!(
            "anchor {} at {:#x}: bytes hash {} but the port expects {}",
            a.name,
            at,
            got,
            a.sha256
        ));
    }
    Ok(())
}

/// Switches for tests and oracles.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// Write the `DKPR` record (always on for real patches; oracles compare against images made without).
    pub record: bool,
}

impl Default for Options {
    fn default() -> Self {
        Options { record: true }
    }
}

pub fn apply(fw_bytes: &[u8], reg: &Registry, lib: &Library, ids: &[String]) -> Result<Applied> {
    apply_with(fw_bytes, reg, lib, ids, Options::default())
}

/// A linked object placed in memory.
struct Chunk {
    owner: String,
    cpu: &'static str,
    region: String,
    addr: u64,
    image: Vec<u8>,
    code: Vec<(u64, u64)>,
    /// ColdFire bss that follows the image (zeroed by the loader)
    bss: u64,
}

/// An in-place change of firmware bytes.
struct Patch {
    owner: String,
    cpu: &'static str,
    addr: u64,
    bytes: Vec<u8>,
    old_sha256: String,
    what: String,
}

/// The built-in symbols a module may name as an extern.
const SEGMENTS: &str = "pk.segments";
const EXPANSION: &str = "core/expansion";

fn uses_expansion(lib: &Library, ids: &[String], dev: &str) -> bool {
    ids.iter().filter_map(|i| lib.get(i)).any(|m| {
        !m.manifest.machine.is_empty()
            || !m.manifest.param.is_empty()
            || m.manifest
                .object
                .iter()
                .any(|o| o.place == "cf.expansion" && on(&o.devices, dev))
    })
}

pub fn apply_with(
    fw_bytes: &[u8],
    reg: &Registry,
    lib: &Library,
    ids: &[String],
    opts: Options,
) -> Result<Applied> {
    let mut fw = Firmware::parse(fw_bytes)?;
    let port = reg.find(&fw.syx_sha256).ok_or_else(|| {
        err!("no port for firmware {}: it is not a stock image patchkit knows (a patched image is refused too)", fw.syx_sha256)
    })?;
    let dev = port.port.device.clone();
    let main_sec = fw
        .section(3)
        .ok_or_else(|| err!("image has no MAIN OS section"))?;
    let dsp_sec = fw
        .section(7)
        .ok_or_else(|| err!("image has no DSP section"))?;
    if sha256_hex(&main_sec.data) != port.port.main_sha256
        || sha256_hex(&dsp_sec.data) != port.port.dsp_sha256
    {
        return Err(err!(
            "port {} does not match the sections of this image",
            port.id()
        ));
    }
    let mut mem = Mem {
        stream: Stream::parse(dsp_sec.data.clone())?,
        main: main_sec.data.clone(),
        main_base: main_sec.dest as u64,
        stream_dirty: false,
        main_dirty: false,
    };
    let stock_main_len = mem.main.len() as u64;
    let segments_addr = mem.main_base + stock_main_len;

    // The module set: the selection, plus core/expansion when anything is placed in the expansion region.
    let mut selected: Vec<String> = ids.to_vec();
    if uses_expansion(lib, &selected, &dev) && !selected.iter().any(|i| i == EXPANSION) {
        if lib.get(EXPANSION).is_none() {
            return Err(err!(
                "a module needs cf.expansion but {EXPANSION} is not in the library"
            ));
        }
        selected.push(EXPANSION.to_string());
    }
    let order = order_modules(lib, &selected)?;
    for id in &order {
        compatible(lib.get(id).unwrap(), port)?;
    }
    let in_expansion = order.iter().any(|i| i == EXPANSION);

    // Claims: one owner per resource.
    let mut claims: BTreeMap<String, String> = BTreeMap::new();
    for id in &order {
        for c in &lib.get(id).unwrap().manifest.claim {
            if let Some(prev) = claims.insert(c.resource.clone(), id.clone()) {
                return Err(err!("{id} and {prev} both claim {}", c.resource));
            }
        }
    }

    // Machine types: assigned in canonical order; the framework generates its object and patches after the anchors
    // are verified.
    let params = crate::params::declared(lib, &order)?;
    if !params.is_empty() && !order.iter().any(|i| i == crate::params::FRAMEWORK) {
        return Err(err!(
            "{} adds parameters but {} is not selected (add it to `requires`)",
            params[0].module,
            crate::params::FRAMEWORK
        ));
    }
    let param_codes = if params.is_empty() {
        BTreeMap::new()
    } else {
        let t1 = port.anchor(crate::params::T1_ANCHOR).ok_or_else(|| {
            err!(
                "port {} has no anchor {}",
                port.id(),
                crate::params::T1_ANCHOR
            )
        })?;
        crate::params::assign(&params, (t1.len / crate::params::ROW as u64) as u32)
    };
    let machines = crate::machines::declared(lib, &order, &param_codes, &port.port.device)?;
    if !machines.is_empty() && !order.iter().any(|i| i == crate::machines::FRAMEWORK) {
        return Err(err!(
            "{} adds machines but {} is not selected (add it to `requires`)",
            machines[0].module,
            crate::machines::FRAMEWORK
        ));
    }

    // Every anchor a selected module uses must hold the bytes the port recorded.
    let mut used: BTreeSet<String> = BTreeSet::new();
    if !machines.is_empty() {
        used.extend(crate::machines::anchor_names_for(&port.port.device));
    }
    if !params.is_empty() {
        used.extend(crate::params::anchor_names());
    }
    for id in &order {
        let m = lib.get(id).unwrap();
        for (o, dko) in m.manifest.object.iter().zip(&m.objects) {
            if on(&o.devices, &dev) {
                for sy in dko.symbols.iter().filter(|s| s.section.is_none()) {
                    let name = o.externs.get(&sy.name).unwrap_or(&sy.name);
                    if port.anchor(name).is_some() {
                        used.insert(name.clone());
                    }
                }
            }
        }
        for op in m
            .manifest
            .op
            .iter()
            .filter(|o| o.op != "register" && on(&o.devices, &dev))
        {
            used.insert(op.anchor.clone());
        }
    }
    for name in &used {
        let a = port.anchor(name).unwrap();
        if a.kind == "sites" {
            continue; // checked by the parameter framework against its own scan
        }
        verify_anchor(&mem, a)?;
    }
    let generated = if machines.is_empty() {
        None
    } else {
        let read = |addr: u64, len: u64| mem.read(CF, addr, len);
        let read_sharc = |addr: u64, len: u64| mem.read(SHARC, addr, len);
        Some(crate::machines::generate(
            port,
            &read,
            &read_sharc,
            &machines,
        )?)
    };
    let generated_params = if params.is_empty() {
        None
    } else {
        let read = |addr: u64, len: u64| mem.read(CF, addr, len);
        Some(crate::params::generate(port, &read, &params)?)
    };
    for (id, code, module) in generated_params.iter().flat_map(|g| g.codes.iter()) {
        for res in [format!("param.id.{id}"), format!("param.code.{code}")] {
            if let Some(prev) = claims.insert(res.clone(), module.clone()) {
                return Err(err!("{module} and {prev} both claim {res}"));
            }
        }
    }
    // One claim per machine id and per assigned type number.
    for (id, ty, module) in generated.iter().flat_map(|g| g.types.iter()) {
        for res in [format!("machine.id.{id}"), format!("machine.type.{ty}")] {
            if let Some(prev) = claims.insert(res.clone(), module.clone()) {
                return Err(err!("{module} and {prev} both claim {res}"));
            }
        }
    }
    let machine_types: BTreeMap<String, u64> = generated
        .iter()
        .flat_map(|g| g.types.iter())
        .map(|(id, ty, _)| (format!("machines.{id}.type"), *ty as u64))
        .collect();

    // Allocate every object first-fit in its region, then link with everyone's exports visible.
    let mut allocs: BTreeMap<String, Alloc> = BTreeMap::new();
    for r in &port.region {
        allocs.insert(r.name.clone(), Alloc::new(port.region_ranges(&r.name)));
    }
    struct Placed<'a> {
        owner: String,
        dko: &'a Dko,
        externs: &'a BTreeMap<String, String>,
        place: String,
        base: u64,
        cpu: &'static str,
    }
    let no_externs: BTreeMap<String, String> = BTreeMap::new();
    let mut placed: Vec<Placed> = vec![];
    for id in &order {
        let m = lib.get(id).unwrap();
        let fw_objects: &[(Dko, &'static str)] = if id == crate::machines::FRAMEWORK {
            generated.as_ref().map_or(&[], |g| &g.objects[..])
        } else if id == crate::params::FRAMEWORK {
            generated_params.as_ref().map_or(&[], |g| &g.objects[..])
        } else {
            &[]
        };
        {
            for (dko, region) in fw_objects {
                let (size, align) = link::footprint(dko);
                let al = allocs
                    .get_mut(*region)
                    .ok_or_else(|| err!("port {} has no {region} region", port.id()))?;
                let base = al.take(size.max(4), align.max(4)).ok_or_else(|| {
                    err!(
                        "{id}: {size} bytes do not fit in {region} ({} of {} bytes used)",
                        al.used,
                        al.cap
                    )
                })?;
                placed.push(Placed {
                    owner: m.tag(),
                    dko,
                    externs: &no_externs,
                    place: (*region).into(),
                    base,
                    cpu: if dko.arch == "sharc" { SHARC } else { CF },
                });
            }
        }
        for (o, dko) in m.manifest.object.iter().zip(&m.objects) {
            if !on(&o.devices, &dev) {
                continue;
            }
            let (size, align) = link::footprint(dko);
            let cpu = if dko.arch == "sharc" { SHARC } else { CF };
            let region = port.region(&o.place).unwrap();
            if region.cpu != cpu {
                return Err(err!(
                    "{id}: a {} object cannot be placed in {} ({})",
                    dko.arch,
                    o.place,
                    region.cpu
                ));
            }
            let al = allocs.get_mut(&o.place).unwrap();
            let base = al.take(size.max(4), align.max(4)).ok_or_else(|| {
                err!(
                    "{id}: {size} bytes do not fit in {} ({} of {} bytes used)",
                    o.place,
                    al.used,
                    al.cap
                )
            })?;
            placed.push(Placed {
                owner: m.tag(),
                dko,
                externs: &o.externs,
                place: o.place.clone(),
                base,
                cpu,
            });
        }
    }
    let mut globals: BTreeMap<String, u64> = BTreeMap::new();
    for p in &placed {
        let l = link::define(p.dko, p.base)?;
        for g in &l.globals {
            if globals.insert(g.clone(), l.symbols[g]).is_some() {
                return Err(err!("symbol {g} is exported twice"));
            }
        }
    }
    let mut writes: Vec<PlanWrite> = vec![];
    let mut all_symbols: BTreeMap<String, u64> = BTreeMap::new();
    let mut chunks: Vec<Chunk> = vec![];
    for p in &placed {
        let mut ext: BTreeMap<String, u64> = globals.clone();
        for sy in p.dko.symbols.iter().filter(|s| s.section.is_none()) {
            let aname = p
                .externs
                .get(&sy.name)
                .cloned()
                .unwrap_or_else(|| sy.name.clone());
            if aname == SEGMENTS {
                ext.insert(sy.name.clone(), segments_addr);
            } else if let Some(v) = machine_types.get(&aname) {
                ext.insert(sy.name.clone(), *v);
            } else if let Some(a) = port.anchor(&aname) {
                ext.insert(sy.name.clone(), anchor_value(a));
            }
        }
        let l = link::link(p.dko, p.base, &ext)?;
        for (k, v) in &l.symbols {
            all_symbols.insert(format!("{}:{k}", p.owner), *v);
        }
        chunks.push(Chunk {
            owner: p.owner.clone(),
            cpu: p.cpu,
            region: p.place.clone(),
            addr: p.base,
            image: l.image,
            code: l.text_ranges,
            bss: l.bss_size.div_ceil(4) * 4,
        });
    }

    // Registrations: rows written into the tables other modules export (before the chunks are hashed and placed).
    register_rows(lib, &order, &dev, &globals, &machine_types, &mut chunks)?;

    // Operations.
    let mut patches: Vec<Patch> = vec![];
    for id in &order {
        let m = lib.get(id).unwrap();
        for op in m
            .manifest
            .op
            .iter()
            .filter(|o| o.op != "register" && on(&o.devices, &dev))
        {
            let a = port.anchor(&op.anchor).unwrap();
            let cpu: &'static str = if a.cpu == SHARC { SHARC } else { CF };
            let addr = (a.addr as i64 + op.offset) as u64;
            let new = match op.op.as_str() {
                "retarget" => retarget(&mem, a, op, &globals)?,
                "jump" => jump_insn(a, op, &globals)?,
                "write" => write_bytes(a, op, addr, &globals)?,
                _ => unreachable!(),
            };
            let old = mem.read(cpu, addr, new.len() as u64)?;
            if op.op == "write" {
                match &op.expect_sha256 {
                    Some(e) if *e != sha256_hex(&old) => {
                        return Err(err!(
                            "{id}: write at {} +{}: old bytes hash {} but the module expects {e}",
                            op.anchor,
                            op.offset,
                            sha256_hex(&old)
                        ));
                    }
                    None if op.offset < 0 || (op.offset as u64 + new.len() as u64) > a.len => {
                        return Err(err!(
                            "{id}: write at {} +{} lies outside the anchor and has no expect_sha256",
                            op.anchor,
                            op.offset
                        ));
                    }
                    _ => {}
                }
            }
            patches.push(Patch {
                owner: m.tag(),
                cpu,
                addr,
                bytes: new,
                old_sha256: sha256_hex(&old),
                what: format!("{} {}{:+}", op.op, op.anchor, op.offset),
            });
        }
    }

    // The frameworks' patches (relocations against the generated objects).
    let framework_patches = [
        (
            crate::machines::FRAMEWORK,
            generated.as_ref().map(|g| &g.patches),
        ),
        (
            crate::params::FRAMEWORK,
            generated_params.as_ref().map(|g| &g.patches),
        ),
    ];
    for (fw_id, gps) in framework_patches {
        let Some(gps) = gps else { continue };
        let owner = lib
            .get(fw_id)
            .map(|m| m.tag())
            .unwrap_or_else(|| fw_id.into());
        for gp in gps {
            let a = port.anchor(&gp.anchor).unwrap();
            if gp.at + gp.bytes.len() as u64 > a.len {
                return Err(err!(
                    "{fw_id}: patch at {} +{} lies outside the anchor",
                    gp.anchor,
                    gp.at
                ));
            }
            // a value anchor names the instruction by `src`, its `addr` is the number the instruction holds
            let site = if a.kind == "value" {
                a.src.unwrap_or(a.addr)
            } else {
                a.addr
            };
            let cpu: &'static str = if a.cpu == SHARC { SHARC } else { CF };
            let addr = site + gp.at;
            let new = apply_relocs(gp.bytes.clone(), &gp.relocs, addr, &globals)?;
            let old = mem.read(cpu, addr, new.len() as u64)?;
            patches.push(Patch {
                owner: owner.clone(),
                cpu,
                addr,
                bytes: new,
                old_sha256: sha256_hex(&old),
                what: format!("{} {}{:+}", gp.what, gp.anchor, gp.at),
            });
        }
    }

    // The patch record: first in the segment chain when the expansion region is in use, else a DSP block.
    let rec = Record {
        base_sha256: fw.syx_sha256.clone(),
        modules: order.iter().map(|i| lib.get(i).unwrap().tag()).collect(),
    };
    let rec_bytes = rec.encode();
    let (rec_region, rec_addr) = if !opts.record {
        ("", 0)
    } else if in_expansion {
        let a = allocs
            .get_mut("cf.expansion")
            .and_then(|al| al.take(rec_bytes.len() as u64, 4))
            .ok_or_else(|| err!("no room for the patch record in cf.expansion"))?;
        ("cf.expansion", a)
    } else {
        let a = allocs
            .get_mut("dsp.l2")
            .ok_or_else(|| {
                err!(
                    "port {} has no dsp.l2 region for the patch record",
                    port.id()
                )
            })?
            .take(rec_bytes.len() as u64, 4)
            .ok_or_else(|| err!("no room for the patch record in dsp.l2"))?;
        ("dsp.l2", a)
    };

    // Overlaps among in-place patches and new memory.
    let mut ranges: Vec<(String, u64, u64, String)> = vec![];
    for p in &patches {
        ranges.push((
            p.cpu.to_string(),
            p.addr,
            p.addr + p.bytes.len() as u64,
            format!("{} {}", p.owner, p.what),
        ));
    }
    for c in &chunks {
        ranges.push((
            c.cpu.to_string(),
            c.addr,
            c.addr + c.image.len() as u64 + c.bss,
            format!("{} object in {}", c.owner, c.region),
        ));
    }
    ranges.sort();
    for w in ranges.windows(2) {
        if w[0].0 == w[1].0 && w[1].1 < w[0].2 {
            return Err(err!(
                "overlapping writes: {} [{:#x},{:#x}) and {} [{:#x},{:#x})",
                w[0].3,
                w[0].1,
                w[0].2,
                w[1].3,
                w[1].1,
                w[1].2
            ));
        }
    }

    // Apply in-place patches first (appended memory must not be visible to them).
    for p in &patches {
        mem.write(p.cpu, p.addr, &p.bytes)?;
        writes.push(PlanWrite {
            cpu: p.cpu.to_string(),
            addr: p.addr,
            len: p.bytes.len() as u64,
            kind: "patch".into(),
            owner: p.owner.clone(),
            what: p.what.clone(),
            old_sha256: Some(p.old_sha256.clone()),
            new_sha256: sha256_hex(&p.bytes),
            new_hex: (p.bytes.len() <= 32).then(|| hex(&p.bytes)),
            code: vec![],
        });
    }
    // Placed objects: DSP blocks, flash-cave code, and the expansion segment chain.
    let mut segs: Vec<(u64, Vec<u8>, u64)> = vec![]; // dest, payload, bss
    if opts.record && in_expansion {
        segs.push((rec_addr, rec_bytes.clone(), 0));
    }
    for c in &chunks {
        match (c.cpu, c.region.as_str()) {
            (SHARC, _) => {
                mem.stream.append_block(c.addr as u32, &c.image)?;
                mem.stream_dirty = true;
            }
            (_, "cf.flash_cave") => {
                let old = mem.read(CF, c.addr, c.image.len() as u64)?;
                if old.iter().any(|&b| b != 0) {
                    return Err(err!(
                        "{}: cf.flash_cave at {:#x} is not zero in this image",
                        c.owner,
                        c.addr
                    ));
                }
                mem.write(CF, c.addr, &c.image)?;
            }
            (_, "cf.expansion") => {
                let mut img = c.image.clone();
                img.resize(img.len().div_ceil(4) * 4, 0);
                segs.push((c.addr, img, c.bss));
            }
            (_, r) => return Err(err!("{}: no placement rule for region {r}", c.owner)),
        }
        writes.push(PlanWrite {
            cpu: c.cpu.to_string(),
            addr: c.addr,
            len: c.image.len() as u64 + c.bss,
            kind: "place".into(),
            owner: c.owner.clone(),
            what: format!("object in {}", c.region),
            old_sha256: None,
            new_sha256: sha256_hex(&c.image),
            new_hex: None,
            code: c.code.clone(),
        });
    }
    if opts.record {
        if rec_region == "dsp.l2" {
            mem.stream.append_block(rec_addr as u32, &rec_bytes)?;
            mem.stream_dirty = true;
        }
        writes.push(PlanWrite {
            cpu: if rec_region == "dsp.l2" { SHARC } else { CF }.into(),
            addr: rec_addr,
            len: rec_bytes.len() as u64,
            kind: "record".into(),
            owner: "patchkit".into(),
            what: format!("DKPR in {rec_region}"),
            old_sha256: None,
            new_sha256: sha256_hex(&rec_bytes),
            new_hex: None,
            code: vec![],
        });
    }
    if in_expansion {
        if !mem.main.len().is_multiple_of(4) {
            return Err(err!(
                "MAIN OS length {} is not a multiple of 4",
                mem.main.len()
            ));
        }
        for (dest, payload, bss) in &segs {
            mem.main
                .extend_from_slice(&(payload.len() as u32).to_be_bytes());
            mem.main.extend_from_slice(&(*dest as u32).to_be_bytes());
            mem.main.extend_from_slice(&(*bss as u32).to_be_bytes());
            mem.main.extend_from_slice(payload);
        }
        mem.main.extend_from_slice(&u32::MAX.to_be_bytes());
        mem.main_dirty = true;
    }

    // Limits, repack, build.
    let lim = &port.limits;
    if mem.stream_dirty {
        if mem.stream.blob.len() as u64 > lim.dsp_depacked_max {
            return Err(err!(
                "DSP stream is {} bytes, over the {} the ColdFire boot routine sends",
                mem.stream.blob.len(),
                lim.dsp_depacked_max
            ));
        }
        fw.replace(7, mem.stream.blob.clone())?;
        let stored = fw.section(7).unwrap().stored.len() as u64;
        if stored > lim.dsp_stored_max {
            return Err(err!(
                "packed DSP section is {stored} bytes, over the {} limit",
                lim.dsp_stored_max
            ));
        }
    }
    if mem.main_dirty {
        if mem.main.len() as u64 > lim.main_staged_max {
            return Err(err!(
                "MAIN OS is {} bytes, over the {} staging limit",
                mem.main.len(),
                lim.main_staged_max
            ));
        }
        fw.replace(3, mem.main.clone())?;
    }
    let syx = fw.build(Some(lim.container_max as usize))?;
    let regions = allocs
        .iter()
        .map(|(k, v)| (k.clone(), (v.used, v.cap)))
        .collect();
    let plan = Plan {
        base: PlanBase {
            device: dev,
            version: port.port.version.clone(),
            syx_sha256: fw.syx_sha256.clone(),
        },
        modules: order.iter().map(|i| lib.get(i).unwrap().tag()).collect(),
        writes,
        regions,
        symbols: all_symbols,
        machines: generated.map(|g| g.types).unwrap_or_default(),
        params: generated_params.map(|g| g.codes).unwrap_or_default(),
        record: rec,
        output: None,
    };
    let mut applied = Applied { syx, plan };
    let report = crate::verify::verify(&applied.syx, Some(reg), Some(fw_bytes))?;
    applied.plan.output = Some(PlanOutput {
        syx_sha256: sha256_hex(&applied.syx),
        syx_bytes: applied.syx.len() as u64,
        container_bytes: report.container_bytes,
        changed_sections: fw
            .sections
            .iter()
            .filter(|s| s.changed)
            .map(|s| (s.id, s.data.len() as u64, s.stored.len() as u64))
            .collect(),
    });
    Ok(applied)
}

/// Rewrite the target of a direct call or jump at an anchor.
///
/// SHARC+: only the unconditional direct CALL (`25a_direct`, opcode 0x180400) in v1; the 24-bit target is the
/// short-word pc. ColdFire: `jsr abs.l` / `jmp abs.l` (absolute target), `jsr d16(pc)`, `bsr.w` and `bsr.l`
/// (pc-relative, range checked).
fn retarget(mem: &Mem, a: &Anchor, op: &Op, globals: &BTreeMap<String, u64>) -> Result<Vec<u8>> {
    let to = op.to.as_deref().unwrap();
    let target = *globals
        .get(to)
        .ok_or_else(|| err!("retarget: module symbol {to} is not defined"))?;
    let addr = (a.addr as i64 + op.offset) as u64;
    if a.cpu == SHARC {
        let b = mem.read(SHARC, addr, 6)?;
        let p0 = u16::from_le_bytes([b[0], b[1]]);
        let p1 = u16::from_le_bytes([b[2], b[3]]);
        if p0 != 0x1804 || (p1 >> 8) != 0 {
            return Err(err!(
                "retarget {}: the instruction at {addr:#x} is not a direct CALL (words {} )",
                a.name,
                hex(&b)
            ));
        }
        if target >= 1 << 24 {
            return Err(err!(
                "retarget: {to} = {target:#x} does not fit a 24-bit target"
            ));
        }
        let n1 = (p1 & 0xff00) | ((target >> 16) as u16 & 0xff);
        let n2 = (target & 0xffff) as u16;
        let mut out = vec![];
        out.extend_from_slice(&p0.to_le_bytes());
        out.extend_from_slice(&n1.to_le_bytes());
        out.extend_from_slice(&n2.to_le_bytes());
        return Ok(out);
    }
    let b = mem.read(CF, addr, 6)?;
    let w = u16::from_be_bytes([b[0], b[1]]);
    let rel = |bits: u32, ext_at: u64| -> Result<i64> {
        let d = target as i64 - (addr + ext_at) as i64;
        let lim = 1i64 << (bits - 1);
        if !(-lim..lim).contains(&d) {
            return Err(err!(
                "retarget {}: {to} is {d} bytes from the call, out of range for {bits} bits",
                a.name
            ));
        }
        Ok(d)
    };
    match w {
        0x4eb9 | 0x4ef9 => {
            let mut out = w.to_be_bytes().to_vec();
            out.extend_from_slice(&(target as u32).to_be_bytes());
            Ok(out)
        }
        0x4eba => {
            let mut out = w.to_be_bytes().to_vec();
            out.extend_from_slice(&(rel(16, 2)? as i16).to_be_bytes());
            Ok(out)
        }
        0x6100 => {
            let mut out = w.to_be_bytes().to_vec();
            out.extend_from_slice(&(rel(16, 2)? as i16).to_be_bytes());
            Ok(out)
        }
        0x61ff => {
            let mut out = w.to_be_bytes().to_vec();
            out.extend_from_slice(&(rel(32, 2)? as i32).to_be_bytes());
            Ok(out)
        }
        _ => Err(err!(
            "retarget {}: the instruction at {addr:#x} is not a jsr/jmp/bsr with a 16- or 32-bit operand (words {})",
            a.name,
            hex(&b)
        )),
    }
}

fn write_bytes(a: &Anchor, op: &Op, addr: u64, globals: &BTreeMap<String, u64>) -> Result<Vec<u8>> {
    if a.cpu != CF && !op.reloc.is_empty() {
        return Err(err!(
            "write: relocations are only supported for ColdFire writes"
        ));
    }
    apply_relocs(unhex(op.hex.as_deref().unwrap())?, &op.reloc, addr, globals)
}

/// ColdFire relocations of a write of `bytes` at `addr`.
fn apply_relocs(
    mut bytes: Vec<u8>,
    relocs: &[crate::module::OpReloc],
    addr: u64,
    globals: &BTreeMap<String, u64>,
) -> Result<Vec<u8>> {
    for r in relocs {
        if r.rtype == "sharc.abs32" {
            // the low 32 bits of a 48-bit instruction (parcels little endian), as the linker writes them
            let v = (*globals
                .get(&r.symbol)
                .ok_or_else(|| err!("write: symbol {} is not defined", r.symbol))?
                as i64
                + r.addend) as u64
                & 0xffff_ffff;
            let at = r.offset as usize;
            if at + 6 > bytes.len() {
                return Err(err!("write: relocation at {at} runs past the bytes"));
            }
            bytes[at + 2..at + 4].copy_from_slice(&((v >> 16) as u16).to_le_bytes());
            bytes[at + 4..at + 6].copy_from_slice(&((v & 0xffff) as u16).to_le_bytes());
            continue;
        }
        let s = *globals
            .get(&r.symbol)
            .ok_or_else(|| err!("write: symbol {} is not defined", r.symbol))?
            as i64
            + r.addend;
        let (size, pcrel) = match r.rtype.as_str() {
            "cf.32" => (4usize, false),
            "cf.pc32" => (4, true),
            "cf.pc16" => (2, true),
            "cf.pc8" => (1, true),
            t => {
                return Err(err!(
                    "write: relocation type {t} is not supported in a write"
                ));
            }
        };
        let v = if pcrel {
            s - (addr + r.offset) as i64
        } else {
            s
        };
        let at = r.offset as usize;
        if at + size > bytes.len() {
            return Err(err!(
                "write: relocation at {at} runs past the {} bytes",
                bytes.len()
            ));
        }
        bytes[at..at + size].copy_from_slice(&(v as u64).to_be_bytes()[8 - size..]);
    }
    Ok(bytes)
}

/// Replace the 48-bit instruction at an anchor by an unconditional, non-delayed `JUMP` to a module symbol (SHARC+).
/// The displaced instruction is the module's job: it executes it (or its equivalent) before it continues.
fn jump_insn(a: &Anchor, op: &Op, globals: &BTreeMap<String, u64>) -> Result<Vec<u8>> {
    let to = op.to.as_deref().unwrap();
    let target = *globals
        .get(to)
        .ok_or_else(|| err!("jump: module symbol {to} is not defined"))?;
    if a.cpu != SHARC || a.len != 6 || op.offset != 0 {
        return Err(err!(
            "jump {}: needs a 6-byte SHARC anchor and offset 0",
            a.name
        ));
    }
    if target >= 1 << 24 {
        return Err(err!(
            "jump: {to} = {target:#x} does not fit a 24-bit target"
        ));
    }
    let mut out = vec![];
    out.extend_from_slice(&0x063eu16.to_le_bytes());
    out.extend_from_slice(&(((target >> 16) & 0xff) as u16).to_le_bytes());
    out.extend_from_slice(&((target & 0xffff) as u16).to_le_bytes());
    Ok(out)
}

fn register_rows(
    lib: &Library,
    order: &[String],
    dev: &str,
    globals: &BTreeMap<String, u64>,
    machine_types: &BTreeMap<String, u64>,
    chunks: &mut [Chunk],
) -> Result<()> {
    let mut next: BTreeMap<String, u64> = BTreeMap::new();
    for id in order {
        let m = lib.get(id).unwrap();
        for op in m
            .manifest
            .op
            .iter()
            .filter(|o| o.op == "register" && on(&o.devices, dev))
        {
            let tsym = op.table.as_deref().unwrap();
            let decl = order
                .iter()
                .filter_map(|i| lib.get(i))
                .flat_map(|m| m.manifest.table.iter())
                .find(|t| t.symbol == tsym)
                .ok_or_else(|| err!("{id}: no selected module declares the table {tsym}"))?;
            if op.row.len() as u64 != decl.row_words {
                return Err(err!(
                    "{id}: row of {} words for table {tsym} of {}-word rows",
                    op.row.len(),
                    decl.row_words
                ));
            }
            let base = *globals
                .get(tsym)
                .ok_or_else(|| err!("{id}: table symbol {tsym} is not defined"))?;
            let k = next.entry(tsym.to_string()).or_insert(0);
            if *k >= decl.rows {
                return Err(err!("{id}: table {tsym} is full ({} rows)", decl.rows));
            }
            let at = base + *k * decl.row_words * 4;
            let end = at + decl.row_words * 4;
            let c = chunks
                .iter_mut()
                .find(|c| c.cpu == SHARC && c.addr <= at && end <= c.addr + c.image.len() as u64)
                .ok_or_else(|| err!("{id}: table {tsym} row {k} lies outside any placed object"))?;
            let off = (at - c.addr) as usize;
            for (i, tok) in op.row.iter().enumerate() {
                let v = match tok.strip_prefix("0x") {
                    Some(h) => u64::from_str_radix(h, 16).ok(),
                    None => tok.parse::<u64>().ok(),
                }
                .or_else(|| globals.get(tok).copied())
                .or_else(|| machine_types.get(tok).copied())
                .ok_or_else(|| {
                    err!("{id}: register row word {tok:?} is neither a number nor a symbol")
                })?;
                if i == 0 && v == 0 {
                    return Err(err!(
                        "{id}: the first word of a registered row must be non-zero"
                    ));
                }
                if v >= 1 << 32 {
                    return Err(err!(
                        "{id}: register row word {tok:?} = {v:#x} does not fit 32 bits"
                    ));
                }
                let o = off + 4 * i;
                if c.image[o..o + 4] != [0, 0, 0, 0] {
                    return Err(err!("{id}: table {tsym} row {k} is not empty"));
                }
                c.image[o..o + 4].copy_from_slice(&(v as u32).to_le_bytes());
            }
            *k += 1;
        }
    }
    Ok(())
}
