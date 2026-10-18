//! Typed module sections and their lowering.
//!
//! A machine module declares what it adds in four small sections instead of hand-written registry rows:
//!
//! ```toml
//! [[param]]    id, name, short, template, min, max, default      (one per parameter; [param.dn2] overrides)
//! [[machine]]  id, name, short, clone_of, knobs = [...], level   (up to the device's knob count; [machine.dn2] overrides)
//! [[page]]     id, machine, name, title                           (only on devices whose knobs live in a page)
//! [[voice]]    machine, entry                                     (machine id -> DSP entry symbol)
//! ```
//!
//! `lower` turns them, per device, into the `[[entry]]` rows the frameworks (`core/machines`, `core/params`,
//! `core/dsp-voice`) take. What a knob means on a device is device data: `[modules.knobs]` and `[modules.frame]` in
//! `devices/<device>.toml` ([`DeviceModel`]). Nothing here names a device. The same resolved view feeds the
//! generators (`gen.rs`: Rust constants, a SHARC include, gallery JSON).

use crate::error::*;
use crate::module::{EntryDef, Manifest};
use serde::Deserialize;
use std::collections::BTreeMap;

// ---- device data ---------------------------------------------------------------------------------------------

/// `[modules.knobs]` of a device file: where a machine's knobs go.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KnobModel {
    /// Knobs a machine can have (positions 0 ..).
    pub count: usize,
    /// Engine or sound-array slot of knob 0.
    pub slot_base: i64,
    /// `machine`: the knobs are fields of the machine row. `page`: they are fields of a page row (`[[page]]`).
    pub to: String,
    /// Field prefix of knob `i` (`f` gives `f0`, `f1` ...).
    pub knob_field: String,
    /// `page` only: field prefix of the slot of knob `i`.
    #[serde(default)]
    pub slot_field: Option<String>,
    /// Field that holds the level code, and its default.
    pub level_field: String,
    pub level_default: i64,
    /// May a machine render with its own DSP routine (`own_dsp`)?
    #[serde(default)]
    pub own_dsp: bool,
}

/// `[modules.frame]`: where the DSP finds a knob's word in its copy of the ColdFire frame.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FrameModel {
    /// Halfword index of knob 0 of the first track (voice).
    pub base: i64,
    /// Halfwords from one track (voice) to the next.
    pub stride: i64,
    /// A gap of `gap` halfwords follows every `group` knobs (0: no gap).
    #[serde(default)]
    pub group: i64,
    #[serde(default)]
    pub gap: i64,
    /// Index the parameter table gives knob 0 (the "mirror" index); knob `j` is `mirror_base + offset(j)`.
    pub mirror_base: i64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModulesSection {
    pub knobs: KnobModel,
    pub frame: FrameModel,
}

#[derive(Clone, Debug)]
pub struct DeviceModel {
    pub short: String,
    pub knobs: KnobModel,
    pub frame: FrameModel,
}

impl DeviceModel {
    /// Halfword offset of knob `j` from knob 0 of the same track.
    pub fn frame_offset(&self, j: usize) -> i64 {
        let j = j as i64;
        let f = &self.frame;
        if f.group > 0 {
            j + (j / f.group) * f.gap
        } else {
            j
        }
    }
}

#[derive(Deserialize)]
struct DeviceFile {
    device: Option<DeviceHead>,
    modules: Option<ModulesSection>,
}
#[derive(Deserialize)]
struct DeviceHead {
    short: String,
}

/// The device models found in the device files (`devices/*.toml`); a file without `[modules]` is skipped.
#[derive(Clone, Debug, Default)]
pub struct Models {
    pub by_device: BTreeMap<String, DeviceModel>,
}

impl Models {
    pub fn from_texts<'a>(texts: impl IntoIterator<Item = &'a str>) -> Result<Models> {
        let mut by_device = BTreeMap::new();
        for t in texts {
            let f: DeviceFile = toml::from_str(t)?;
            let (Some(d), Some(m)) = (f.device, f.modules) else {
                continue;
            };
            if by_device
                .insert(
                    d.short.clone(),
                    DeviceModel {
                        short: d.short.clone(),
                        knobs: m.knobs,
                        frame: m.frame,
                    },
                )
                .is_some()
            {
                return Err(err!("two device files describe [modules] of {}", d.short));
            }
        }
        Ok(Models { by_device })
    }

    #[cfg(not(target_arch = "wasm32"))]
    pub fn from_dir(dir: &std::path::Path) -> Result<Models> {
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
        Models::from_texts(texts.iter().map(|s| s.as_str()))
    }
}

// ---- the typed sections --------------------------------------------------------------------------------------

/// One knob of a machine: a stock parameter code (`0` = none) or a parameter of this module (its id).
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum Knob {
    Code(i64),
    Param(String),
}

#[derive(Clone, Debug, Deserialize)]
pub struct ParamDef {
    pub id: String,
    pub name: Option<String>,
    pub short: Option<String>,
    /// Stock code whose row is copied (owner, flag, kind, formatter).
    pub template: Option<i64>,
    pub min: Option<i64>,
    pub max: Option<i64>,
    pub default: Option<i64>,
    #[serde(default)]
    pub devices: Vec<String>,
    /// `[param.<device>]` overrides.
    #[serde(flatten)]
    pub per_device: BTreeMap<String, toml::Value>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct ParamOver {
    name: Option<String>,
    short: Option<String>,
    template: Option<i64>,
    min: Option<i64>,
    max: Option<i64>,
    default: Option<i64>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct MachineDef {
    pub id: String,
    pub name: Option<String>,
    pub short: Option<String>,
    pub clone_of: Option<i64>,
    pub position: Option<i64>,
    pub own_dsp: Option<bool>,
    pub knobs: Option<Vec<Knob>>,
    pub level: Option<i64>,
    #[serde(default)]
    pub devices: Vec<String>,
    /// `[machine.<device>]` overrides.
    #[serde(flatten)]
    pub per_device: BTreeMap<String, toml::Value>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct MachineOver {
    name: Option<String>,
    short: Option<String>,
    clone_of: Option<i64>,
    position: Option<i64>,
    own_dsp: Option<bool>,
    knobs: Option<Vec<Knob>>,
    level: Option<i64>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PageDef {
    pub id: String,
    pub machine: String,
    pub name: String,
    pub title: String,
    /// Default: the machine's knobs on that device.
    pub knobs: Option<Vec<Knob>>,
    #[serde(default)]
    pub devices: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VoiceDef {
    pub machine: String,
    /// The routine's symbol in the module's DSP object.
    pub entry: String,
    #[serde(default)]
    pub devices: Vec<String>,
}

pub fn has_typed(m: &Manifest) -> bool {
    !(m.machine.is_empty() && m.param.is_empty() && m.page.is_empty() && m.voice.is_empty())
}

// ---- resolution ----------------------------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct RMachine {
    pub id: String,
    pub name: String,
    pub short: String,
    pub clone_of: i64,
    pub position: Option<i64>,
    pub own_dsp: bool,
    pub knobs: Vec<Knob>,
    pub level: i64,
}

#[derive(Clone, Debug)]
pub struct RParam {
    pub id: String,
    pub name: String,
    pub short: String,
    pub template: i64,
    pub min: i64,
    pub max: i64,
    pub default: i64,
    /// Knob position, slot (`slot_base + knob`), halfword offset in the frame, parameter-table mirror index.
    pub knob: usize,
    pub slot: i64,
    pub frame_off: i64,
    pub mirror: i64,
}

#[derive(Clone, Debug)]
pub struct RPage {
    pub id: String,
    pub machine: String,
    pub name: String,
    pub title: String,
    pub knobs: Vec<Knob>,
    pub level: i64,
}

#[derive(Clone, Debug)]
pub struct RVoice {
    pub machine: String,
    pub entry: String,
}

/// The typed sections of one module as one device sees them.
#[derive(Clone, Debug)]
pub struct DevResolved {
    pub dev: String,
    pub model: DeviceModel,
    pub machines: Vec<RMachine>,
    pub params: Vec<RParam>,
    pub pages: Vec<RPage>,
    pub voices: Vec<RVoice>,
}

fn on(devices: &[String], dev: &str) -> bool {
    devices.is_empty() || devices.iter().any(|d| d == dev)
}

/// Split the flattened per-device tables: the base keys that are device names, decoded as `T`.
fn over<T: Default + for<'de> Deserialize<'de>>(
    who: &str,
    what: &str,
    map: &BTreeMap<String, toml::Value>,
    dev: &str,
) -> Result<T> {
    match map.get(dev) {
        None => Ok(T::default()),
        Some(v) => v
            .clone()
            .try_into()
            .map_err(|e| err!("{who}: {what} [{dev}]: {e}")),
    }
}

fn check_keys(
    who: &str,
    what: &str,
    map: &BTreeMap<String, toml::Value>,
    models: &Models,
) -> Result<()> {
    for k in map.keys() {
        if !models.by_device.contains_key(k) {
            return Err(err!(
                "{who}: {what}: unknown key {k:?} (a device table needs a [modules] section in devices/{k}.toml)"
            ));
        }
    }
    Ok(())
}

fn need<T: Clone>(who: &str, what: &str, key: &str, v: &Option<T>) -> Result<T> {
    v.clone()
        .ok_or_else(|| err!("{who}: {what}: no {key} (on this device)"))
}

/// Resolve the typed sections against the device models: one [`DevResolved`] per device that has something.
pub fn resolve(m: &Manifest, models: &Models) -> Result<Vec<DevResolved>> {
    let who = m.module.id.as_str();
    for d in m
        .machine
        .iter()
        .map(|x| &x.devices)
        .chain(m.param.iter().map(|x| &x.devices))
    {
        for x in d {
            if !models.by_device.contains_key(x) {
                return Err(err!(
                    "{who}: device {x:?} has no [modules] section in devices/"
                ));
            }
        }
    }
    for p in &m.param {
        check_keys(who, &format!("param {}", p.id), &p.per_device, models)?;
    }
    for x in &m.machine {
        check_keys(who, &format!("machine {}", x.id), &x.per_device, models)?;
    }
    let mut out = vec![];
    for (dev, model) in &models.by_device {
        let kn = &model.knobs;
        // machines
        let mut machines = vec![];
        for d in m.machine.iter().filter(|d| on(&d.devices, dev)) {
            let what = format!("machine {}", d.id);
            let o: MachineOver = over(who, &what, &d.per_device, dev)?;
            let name = need(who, &what, "name", &o.name.or(d.name.clone()))?;
            let short = need(who, &what, "short", &o.short.or(d.short.clone()))?;
            let clone_of = need(who, &what, "clone_of", &o.clone_of.or(d.clone_of))?;
            let knobs = o.knobs.or(d.knobs.clone()).unwrap_or_default();
            if knobs.len() > kn.count {
                return Err(err!(
                    "{who}: {what}: {} knobs, but {dev} has {}",
                    knobs.len(),
                    kn.count
                ));
            }
            let own_dsp = o.own_dsp.or(d.own_dsp).unwrap_or(false);
            if own_dsp && !kn.own_dsp {
                return Err(err!("{who}: {what}: {dev} has no own_dsp machines"));
            }
            machines.push(RMachine {
                id: d.id.clone(),
                name,
                short,
                clone_of,
                position: o.position.or(d.position),
                own_dsp,
                knobs,
                level: o.level.or(d.level).unwrap_or(kn.level_default),
            });
        }
        // slots of the module's parameters: the knob position in the machines that use them
        let mut slot_of: BTreeMap<String, usize> = BTreeMap::new();
        let mut knob_lists: Vec<(&str, &Vec<Knob>)> =
            machines.iter().map(|x| (x.id.as_str(), &x.knobs)).collect();
        let page_knobs: Vec<(String, Vec<Knob>)> = m
            .page
            .iter()
            .filter(|p| on(&p.devices, dev))
            .filter_map(|p| p.knobs.clone().map(|k| (p.id.clone(), k)))
            .collect();
        for (id, k) in &page_knobs {
            knob_lists.push((id.as_str(), k));
        }
        for (owner, ks) in &knob_lists {
            for (j, k) in ks.iter().enumerate() {
                let Knob::Param(p) = k else { continue };
                if !m.param.iter().any(|d| &d.id == p && on(&d.devices, dev)) {
                    return Err(err!(
                        "{who}: {owner}: knob {j} names {p:?}, which is not a param of {dev}"
                    ));
                }
                if let Some(prev) = slot_of.insert(p.clone(), j)
                    && prev != j
                {
                    return Err(err!(
                        "{who}: param {p} sits at knob {prev} and knob {j} ({owner}); one param has one slot"
                    ));
                }
            }
        }
        // params
        let mut params = vec![];
        for d in m.param.iter().filter(|d| on(&d.devices, dev)) {
            let what = format!("param {}", d.id);
            let Some(&knob) = slot_of.get(&d.id) else {
                if d.devices.iter().any(|x| x == dev) {
                    return Err(err!("{who}: {what}: no machine of {dev} uses it"));
                }
                continue;
            };
            let o: ParamOver = over(who, &what, &d.per_device, dev)?;
            params.push(RParam {
                id: d.id.clone(),
                name: need(who, &what, "name", &o.name.or(d.name.clone()))?,
                short: need(who, &what, "short", &o.short.or(d.short.clone()))?,
                template: need(who, &what, "template", &o.template.or(d.template))?,
                min: need(who, &what, "min", &o.min.or(d.min))?,
                max: need(who, &what, "max", &o.max.or(d.max))?,
                default: need(who, &what, "default", &o.default.or(d.default))?,
                knob,
                slot: kn.slot_base + knob as i64,
                frame_off: model.frame_offset(knob),
                mirror: model.frame.mirror_base + model.frame_offset(knob),
            });
        }
        // pages
        let mut pages = vec![];
        for p in m.page.iter().filter(|p| on(&p.devices, dev)) {
            let what = format!("page {}", p.id);
            if kn.to != "page" {
                return Err(err!(
                    "{who}: {what}: {dev} keeps its knobs in the machine row, not in a page \
                     (a second SRC page for a Digitakt II machine is not supported yet: an added machine has one SRC page, \
                     the engine has ten parameter slots and the page reads eight; docs/findings/50)"
                ));
            }
            let Some(mach) = machines.iter().find(|x| x.id == p.machine) else {
                return Err(err!("{who}: {what}: no machine {:?} on {dev}", p.machine));
            };
            let knobs = p.knobs.clone().unwrap_or_else(|| mach.knobs.clone());
            for (j, k) in knobs.iter().enumerate() {
                if matches!(k, Knob::Code(c) if *c != 0) {
                    return Err(err!(
                        "{who}: {what}: knob {j}: a stock code on a page is not supported yet (name a param)"
                    ));
                }
            }
            if knobs.len() > kn.count {
                return Err(err!(
                    "{who}: {what}: {} knobs, but {dev} has {}",
                    knobs.len(),
                    kn.count
                ));
            }
            pages.push(RPage {
                id: p.id.clone(),
                machine: p.machine.clone(),
                name: p.name.clone(),
                title: p.title.clone(),
                knobs,
                level: mach.level,
            });
        }
        // voices
        let mut voices = vec![];
        for v in m.voice.iter().filter(|v| on(&v.devices, dev)) {
            if !machines.iter().any(|x| x.id == v.machine) {
                if v.devices.iter().any(|x| x == dev) {
                    return Err(err!(
                        "{who}: voice {}: no machine of that id on {dev}",
                        v.machine
                    ));
                }
                continue;
            }
            voices.push(RVoice {
                machine: v.machine.clone(),
                entry: v.entry.clone(),
            });
        }
        if machines.is_empty() && params.is_empty() && pages.is_empty() && voices.is_empty() {
            continue;
        }
        if kn.to != "machine" && kn.to != "page" {
            return Err(err!(
                "devices/{dev}.toml: [modules.knobs] to = {:?} is neither machine nor page",
                kn.to
            ));
        }
        out.push(DevResolved {
            dev: dev.clone(),
            model: model.clone(),
            machines,
            params,
            pages,
            voices,
        });
    }
    Ok(out)
}

// ---- lowering ------------------------------------------------------------------------------------------------

fn kv(k: &str, v: toml::Value) -> (String, toml::Value) {
    (k.to_string(), v)
}

fn ent(id: &str, registry: &str, dev: &str, fields: Vec<(String, toml::Value)>) -> EntryDef {
    EntryDef {
        registry: registry.into(),
        id: id.into(),
        devices: vec![dev.into()],
        fields: fields.into_iter().collect(),
    }
}

fn int(i: i64) -> toml::Value {
    toml::Value::Integer(i)
}
fn s(t: &str) -> toml::Value {
    toml::Value::String(t.into())
}

/// The value a knob puts in a row field: a code, or the symbol `params.<id>.index`.
fn knob_value(k: Option<&Knob>) -> toml::Value {
    match k {
        None => int(0),
        Some(Knob::Code(c)) => int(*c),
        Some(Knob::Param(p)) => s(&format!("params.{p}.index")),
    }
}

/// The `[[entry]]` rows of the typed sections of one module, in the order the hand-written form had them:
/// voices, machines, params, pages, per device.
pub fn entries(resolved: &[DevResolved]) -> Vec<EntryDef> {
    let mut out = vec![];
    for r in resolved {
        let kn = &r.model.knobs;
        let dev = r.dev.as_str();
        for v in &r.voices {
            out.push(ent(
                &v.machine,
                "voices",
                dev,
                vec![
                    kv("type", s(&format!("machines.{}.index", v.machine))),
                    kv("pc", s(&v.entry)),
                ],
            ));
        }
        for m in &r.machines {
            let mut f = vec![
                kv("name", s(&m.name)),
                kv("short", s(&m.short)),
                kv("clone_of", int(m.clone_of)),
            ];
            if let Some(p) = m.position {
                f.push(kv("position", int(p)));
            }
            if m.own_dsp {
                f.push(kv("own_dsp", int(1)));
            }
            if kn.to == "machine" {
                let names: Vec<String> = (0..kn.count)
                    .map(|i| format!("{}{i}", kn.knob_field))
                    .collect();
                for (i, n) in names.iter().enumerate() {
                    f.push(kv(n, knob_value(m.knobs.get(i))));
                }
                f.push(kv(&kn.level_field, int(m.level)));
            }
            out.push(ent(&m.id, "machines", dev, f));
        }
        for p in &r.params {
            out.push(ent(
                &p.id,
                "params",
                dev,
                vec![
                    kv("name", s(&p.name)),
                    kv("short", s(&p.short)),
                    kv("template", int(p.template)),
                    kv("slot", int(p.slot)),
                    kv("min", int(p.min)),
                    kv("max", int(p.max)),
                    kv("default", int(p.default)),
                ],
            ));
        }
        for g in &r.pages {
            let mut f = vec![
                kv("machine", s(&format!("machines.{}.index", g.machine))),
                kv("name", s(&g.name)),
                kv("title", s(&g.title)),
            ];
            let c: Vec<String> = (0..kn.count)
                .map(|i| format!("{}{i}", kn.knob_field))
                .collect();
            let sl: Vec<String> = (0..kn.count)
                .map(|i| format!("{}{i}", kn.slot_field.as_deref().unwrap_or("s")))
                .collect();
            for (i, k) in g.knobs.iter().enumerate() {
                f.push(kv(&c[i], knob_value(Some(k))));
                if matches!(k, Knob::Param(_)) {
                    f.push(kv(&sl[i], int(kn.slot_base + i as i64)));
                }
            }
            f.push(kv(&kn.level_field, int(g.level)));
            out.push(ent(&g.id, "pages", dev, f));
        }
    }
    out
}

/// Lower the typed sections of a manifest into `[[entry]]` rows (once). A manifest with typed sections that is
/// never lowered is refused when it is applied (`prim.rs`).
pub fn lower(m: &mut Manifest, models: &Models) -> Result<()> {
    if m.lowered || !has_typed(m) {
        return Ok(());
    }
    if m.module.dynamic {
        // loaded at run time: the packer lowers it into `.dkm` records, nothing is patched
        m.lowered = true;
        return Ok(());
    }
    if models.by_device.is_empty() {
        return Err(err!(
            "{}: has [[machine]]/[[param]] sections, but no device file with [modules] is available",
            m.module.id
        ));
    }
    let rows = entries(&resolve(m, models)?);
    m.entry.extend(rows);
    m.lowered = true;
    crate::prim::validate_manifest(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    pub const DT2: &str = r#"
[device]
short = "dt2"
[modules.knobs]
count = 8
slot_base = 0
to = "machine"
knob_field = "f"
level_field = "f8"
level_default = 10
[modules.frame]
base = 0x6d
stride = 48
group = 4
gap = 2
mirror_base = 25
"#;
    pub const DN2: &str = r#"
[device]
short = "dn2"
[modules.knobs]
count = 8
slot_base = 25
to = "page"
knob_field = "c"
slot_field = "s"
level_field = "c8"
level_default = 10
own_dsp = true
[modules.frame]
base = 0x6d
stride = 0x49
mirror_base = 25
"#;
    const MOD: &str = r#"
[module]
id = "machines/t"
version = "0.1.0"
name = "T"
category = "machines"
status = "experimental"
summary = "t."

[[param]]
id = "t_a"
name = "A"
short = "A"
template = 0xd1
min = 0
max = 100
default = 50
[param.dn2]
template = 0xd5

[[param]]
id = "t_b"
name = "B"
short = "B"
template = 0xd1
min = 0
max = 100
default = 1

[[machine]]
id = "t"
name = "T"
short = "T"
clone_of = 0
knobs = [0xca, "t_a", 0, 0, "t_b"]
[machine.dn2]
position = 4
own_dsp = true
knobs = ["t_a", "t_b"]

[[page]]
id = "t_syn"
machine = "t"
name = "T"
title = "T"
devices = ["dn2"]

[[voice]]
machine = "t"
entry = "_t_render"
"#;

    fn models() -> Models {
        Models::from_texts([DT2, DN2]).unwrap()
    }
    fn manifest(t: &str) -> Manifest {
        toml::from_str(t).unwrap()
    }
    fn field<'a>(e: &'a EntryDef, k: &str) -> Option<&'a toml::Value> {
        e.fields.get(k)
    }

    #[test]
    fn dt2_lowering_puts_knobs_in_the_machine_row() {
        let mut m = manifest(MOD);
        lower(&mut m, &models()).unwrap();
        let on = |dev: &str, reg: &str| -> Vec<&EntryDef> {
            m.entry
                .iter()
                .filter(|e| e.devices == [dev] && e.registry == reg)
                .collect()
        };
        let mach = on("dt2", "machines");
        assert_eq!(mach.len(), 1);
        let e = mach[0];
        assert_eq!(field(e, "f0"), Some(&int(0xca)));
        assert_eq!(field(e, "f1"), Some(&s("params.t_a.index")));
        assert_eq!(field(e, "f3"), Some(&int(0)));
        assert_eq!(field(e, "f4"), Some(&s("params.t_b.index")));
        assert_eq!(field(e, "f8"), Some(&int(10)));
        assert!(field(e, "position").is_none() && field(e, "own_dsp").is_none());
        // slots are the knob positions; the dt2 frame skips two halfwords after every four knobs
        let p = on("dt2", "params");
        assert_eq!(field(p[0], "slot"), Some(&int(1)));
        assert_eq!(field(p[0], "template"), Some(&int(0xd1)));
        assert_eq!(field(p[1], "slot"), Some(&int(4)));
        assert!(on("dt2", "pages").is_empty());
        let v = on("dt2", "voices");
        assert_eq!(field(v[0], "type"), Some(&s("machines.t.index")));
        assert_eq!(field(v[0], "pc"), Some(&s("_t_render")));
    }

    #[test]
    fn dn2_lowering_puts_knobs_in_a_page_and_slots_after_25() {
        let mut m = manifest(MOD);
        lower(&mut m, &models()).unwrap();
        let on = |reg: &str| -> Vec<&EntryDef> {
            m.entry
                .iter()
                .filter(|e| e.devices == ["dn2"] && e.registry == reg)
                .collect()
        };
        let mach = on("machines");
        assert_eq!(field(mach[0], "own_dsp"), Some(&int(1)));
        assert_eq!(field(mach[0], "position"), Some(&int(4)));
        assert!(field(mach[0], "f0").is_none());
        let p = on("params");
        assert_eq!(field(p[0], "slot"), Some(&int(25)));
        assert_eq!(field(p[0], "template"), Some(&int(0xd5)));
        assert_eq!(field(p[1], "template"), Some(&int(0xd1)));
        assert_eq!(field(p[1], "slot"), Some(&int(26)));
        let g = on("pages");
        assert_eq!(field(g[0], "machine"), Some(&s("machines.t.index")));
        assert_eq!(field(g[0], "c0"), Some(&s("params.t_a.index")));
        assert_eq!(field(g[0], "s1"), Some(&int(26)));
        assert_eq!(field(g[0], "c8"), Some(&int(10)));
    }

    #[test]
    fn frame_offsets_follow_the_device_model() {
        let ms = models();
        let dt2 = &ms.by_device["dt2"];
        let off: Vec<i64> = (0..8).map(|j| dt2.frame_offset(j)).collect();
        assert_eq!(off, [0, 1, 2, 3, 6, 7, 8, 9]);
        let dn2 = &ms.by_device["dn2"];
        assert_eq!(dn2.frame_offset(3), 3);
        let r = resolve(&manifest(MOD), &ms).unwrap();
        let p = |dev: &str, id: &str| {
            r.iter()
                .find(|x| x.dev == dev)
                .unwrap()
                .params
                .iter()
                .find(|p| p.id == id)
                .unwrap()
                .clone()
        };
        assert_eq!((p("dt2", "t_b").frame_off, p("dt2", "t_b").mirror), (6, 31));
        assert_eq!((p("dn2", "t_b").frame_off, p("dn2", "t_b").mirror), (1, 26));
    }

    #[test]
    fn mistakes_are_refused() {
        let ms = models();
        let bad = |t: String| -> String {
            let mut m = manifest(&t);
            match lower(&mut m, &ms) {
                Err(e) => e.0,
                Ok(()) => panic!("accepted:\n{t}"),
            }
        };
        // too many knobs
        let e = bad(MOD.replace("[0xca, \"t_a\", 0, 0, \"t_b\"]", "[0,0,0,0,0,0,0,0,0]"));
        assert!(e.contains("9 knobs"), "{e}");
        // a typo in a device table
        let e = bad(MOD.replace("[machine.dn2]", "[machine.dm2]"));
        assert!(e.contains("unknown key \"dm2\""), "{e}");
        // an unknown field of an override
        let e = bad(MOD.replace("position = 4", "postion = 4"));
        assert!(e.contains("postion"), "{e}");
        // one param at two knob positions (a page that orders the knobs differently from its machine)
        let e = bad(MOD.replace(
            "devices = [\"dn2\"]\n\n[[voice]]",
            "knobs = [\"t_b\", \"t_a\"]\ndevices = [\"dn2\"]\n\n[[voice]]",
        ));
        assert!(e.contains("one param has one slot"), "{e}");
        // a page on a device whose knobs live in the machine row
        let e = bad(MOD.replace("devices = [\"dn2\"]", "devices = [\"dt2\"]"));
        assert!(
            e.contains("not in a page") && e.contains("second SRC page"),
            "{e}"
        );
        // own_dsp where the device has none
        let e = bad(MOD.replace("clone_of = 0\nknobs", "clone_of = 0\nown_dsp = true\nknobs"));
        assert!(e.contains("no own_dsp"), "{e}");
        // a param pinned to a device where no machine uses it
        let e = bad(MOD
            .replace("default = 1", "default = 1\ndevices = [\"dt2\"]")
            .replace(
                "knobs = [0xca, \"t_a\", 0, 0, \"t_b\"]",
                "knobs = [0xca, \"t_a\"]",
            )
            .replace("knobs = [\"t_a\", \"t_b\"]", "knobs = [\"t_a\"]"));
        assert!(e.contains("no machine of dt2 uses it"), "{e}");
    }

    #[test]
    fn lowering_runs_once_and_needs_models() {
        let mut m = manifest(MOD);
        assert!(lower(&mut m, &Models::default()).is_err());
        lower(&mut m, &models()).unwrap();
        let n = m.entry.len();
        lower(&mut m, &models()).unwrap();
        assert_eq!(m.entry.len(), n);
    }
}
