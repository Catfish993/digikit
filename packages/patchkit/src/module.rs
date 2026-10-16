//! Module manifests (`module.toml`) and the library of modules found under `modules/`.

use crate::dko::Dko;
use crate::error::*;
use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize)]
pub struct Manifest {
    pub module: ModuleInfo,
    #[serde(default)]
    pub targets: BTreeMap<String, String>,
    #[serde(default)]
    pub object: Vec<ObjectRef>,
    /// Resolution rules for `tools/patchkit/port_resolve.py`; patchkit does not read them.
    #[serde(default)]
    pub anchor_rule: Vec<toml::Value>,
    #[serde(default)]
    pub op: Vec<Op>,
    #[serde(default)]
    pub claim: Vec<Claim>,
    /// P1: sections copied from the user's image (and extended) at apply time.
    #[serde(default)]
    pub section: Vec<SectionDef>,
    /// P2: registries this module declares (a framework) ...
    #[serde(default)]
    pub registry: Vec<RegistryDef>,
    /// ... and entries it adds to registries (a content module).
    #[serde(default)]
    pub entry: Vec<EntryDef>,
}

/// A number or an expression (`crate::expr`) written either as a TOML integer or as a string.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum ExprSrc {
    Int(i64),
    Text(String),
}

impl ExprSrc {
    pub fn text(&self) -> String {
        match self {
            ExprSrc::Int(i) => i.to_string(),
            ExprSrc::Text(t) => t.clone(),
        }
    }
}

/// P1. `[[section]] name, region, from = { anchor, offset, len, expect_sha256 }, append = { registry, row }`.
/// The section's bytes are copied from the user's image when the patch is applied (firmware is never stored in the
/// repository), one row per entry of the registry is appended, and the section is placed in `region`. The symbol
/// `<name>` is its address and `<name>.size` its length in bytes.
#[derive(Clone, Debug, Deserialize)]
pub struct SectionDef {
    pub name: String,
    pub region: String,
    #[serde(default)]
    pub devices: Vec<String>,
    /// Alignment of the placed section in bytes (default 4).
    #[serde(default)]
    pub align: Option<u64>,
    pub from: SectionFrom,
    #[serde(default)]
    pub append: Option<SectionAppend>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SectionFrom {
    pub anchor: String,
    #[serde(default)]
    pub offset: i64,
    /// Bytes to copy (expression, static: anchors and image reads only). Default: the rest of the anchor.
    #[serde(default)]
    pub len: Option<ExprSrc>,
    /// sha256 of the copied bytes, needed when the range reaches outside the anchor.
    #[serde(default)]
    pub expect_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SectionAppend {
    pub registry: String,
    /// One expression per word of the row; `u8:EXPR` and `u16:EXPR` emit narrower values (default `u32`). The
    /// fields of the entry are `$name`, its index is `$index`.
    pub row: Vec<ExprSrc>,
}

/// P2. A framework declares a registry; patchkit builds its row table.
#[derive(Clone, Debug, Deserialize)]
pub struct RegistryDef {
    pub name: String,
    /// Index of the first entry (expression, static).
    pub first: ExprSrc,
    /// Most entries the framework can take.
    pub max: u32,
    pub region: String,
    #[serde(default)]
    pub devices: Vec<String>,
    /// One all-zero row behind the entries (not counted in `<reg>.count`): a table a routine scans until a zero row.
    #[serde(default)]
    pub terminator: bool,
    #[serde(default)]
    pub field: Vec<FieldDef>,
    /// Permanent entry numbers: a TOML file of the framework (path relative to the module) with, per device, the
    /// highest number the framework supports (`last`) and one number per entry, keyed `"<module id>:<entry id>"`
    /// (`[dt2.alloc]`). An entry's index is its allocated number, not `first + k`. The registry then spans
    /// `first ..= highest selected number`; a number nobody selected is a gap row built from `[registry.gap]`.
    #[serde(default)]
    pub index_table: Option<String>,
    /// Field values of a gap row (the registry's field defaults fill the rest).
    #[serde(default)]
    pub gap: Option<BTreeMap<String, toml::Value>>,
}

/// The parsed `index_table` of a registry: device -> numbers.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct AllocTable {
    #[serde(flatten)]
    pub devices: BTreeMap<String, DevAlloc>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct DevAlloc {
    /// The highest index the framework supports on this device.
    pub last: i64,
    #[serde(default)]
    pub alloc: BTreeMap<String, i64>,
}

impl AllocTable {
    pub fn parse(text: &str) -> Result<AllocTable> {
        let t: AllocTable = toml::from_str(text)?;
        for (dev, d) in &t.devices {
            if !matches!(dev.as_str(), "dt2" | "dn2") {
                return Err(err!("allocation table: unknown device [{dev}]"));
            }
            let mut seen: BTreeMap<i64, &str> = BTreeMap::new();
            for (k, v) in &d.alloc {
                if *v < 0 || *v > d.last {
                    return Err(err!(
                        "allocation table [{dev}]: {k} = {v} is outside 0..={}",
                        d.last
                    ));
                }
                if let Some(prev) = seen.insert(*v, k) {
                    return Err(err!(
                        "allocation table [{dev}]: {prev} and {k} both have number {v}"
                    ));
                }
            }
        }
        Ok(t)
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct FieldDef {
    pub name: String,
    /// `u8`, `u16`, `u32`, `cstr` (a pointer to a string placed behind the rows) or `sym` (an expression; a symbol
    /// of any module, or `<registry>.<id>.index`)
    pub kind: String,
    #[serde(default)]
    pub max_len: Option<usize>,
    #[serde(default)]
    pub default: Option<toml::Value>,
}

/// P2. A content module adds an entry: `[[entry]] registry = "machines", id = "pluck", <field> = value ...`.
#[derive(Clone, Debug, Deserialize)]
pub struct EntryDef {
    pub registry: String,
    pub id: String,
    #[serde(default)]
    pub devices: Vec<String>,
    #[serde(flatten)]
    pub fields: BTreeMap<String, toml::Value>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ModuleInfo {
    /// `category/name`, the path under `modules/`
    pub id: String,
    pub version: String,
    pub name: String,
    pub category: String,
    pub status: String,
    pub summary: String,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub conflicts: Vec<String>,
    /// Restrict to these ports (`dt2-1.16`); empty = any firmware whose anchors resolve.
    #[serde(default)]
    pub ports: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ObjectRef {
    pub path: String,
    /// Port region the object is placed in: `dsp.l2`, `cf.expansion`, `cf.flash_cave`.
    pub place: String,
    /// Restrict to these devices (`dt2`, `dn2`); empty = all.
    #[serde(default)]
    pub devices: Vec<String>,
    /// Undefined symbol -> anchor name (an undefined symbol named like an anchor needs no entry).
    #[serde(default)]
    pub externs: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Op {
    /// `write`, `retarget` or `hook`
    pub op: String,
    #[serde(default)]
    pub anchor: String,
    /// write: `PREFIX.*` repeats the op for every anchor named `PREFIX.<number>` (a multi-site rule of
    /// `tools/patchkit/port_resolve.py`), in numeric order; `this` names the current anchor in expressions.
    #[serde(default)]
    pub anchors: Option<String>,
    /// write: the new value (expression) and its width in bytes (1, 2 or 4), range checked
    #[serde(default)]
    pub value: Option<ExprSrc>,
    #[serde(default)]
    pub width: Option<u32>,
    /// hook: `jump` (default) or `call` (ColdFire `jsr.l`; the hook returns to the end of the site)
    #[serde(default)]
    pub kind: Option<String>,
    /// hook: `run` (default; the displaced instructions run in `hook.<anchor>.tail`) or `drop`
    #[serde(default)]
    pub displaced: Option<String>,
    /// hook: `cf` or `sharc`; optional, must agree with the anchor
    #[serde(default)]
    pub cpu: Option<String>,
    /// hook: region of the tail (default `cf.expansion` / `dsp.l2`)
    #[serde(default)]
    pub place: Option<String>,
    #[serde(default)]
    pub devices: Vec<String>,
    /// byte offset from the anchor address
    #[serde(default)]
    pub offset: i64,
    /// retarget: the module symbol the call or jump now goes to
    pub to: Option<String>,
    /// write: the new bytes
    pub hex: Option<String>,
    /// write: sha256 of the bytes being replaced (never the bytes themselves)
    pub expect_sha256: Option<String>,
    #[serde(default)]
    pub reloc: Vec<OpReloc>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct OpReloc {
    pub offset: u64,
    #[serde(rename = "type")]
    pub rtype: String,
    pub symbol: String,
    #[serde(default)]
    pub addend: i64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Claim {
    pub resource: String,
    #[serde(default)]
    pub devices: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Module {
    pub manifest: Manifest,
    pub objects: Vec<Dko>,
    /// parsed `index_table` files, by registry name
    pub alloc: BTreeMap<String, AllocTable>,
    /// sha256 of the manifest text and the object texts, in order (for the plan)
    pub digest: String,
}

impl Module {
    /// Build a module from its manifest text and a reader for the object paths it names.
    pub fn from_parts(
        manifest_text: &str,
        read: &dyn Fn(&str) -> Result<String>,
    ) -> Result<Module> {
        let manifest: Manifest = toml::from_str(manifest_text)?;
        let mut objects = vec![];
        let mut all = manifest_text.to_string();
        for o in &manifest.object {
            let t = read(&o.path)?;
            all.push_str(&t);
            objects.push(Dko::parse(&t).map_err(|e| err!("{}: {e}", o.path))?);
        }
        let mut alloc = BTreeMap::new();
        for r in &manifest.registry {
            if let Some(path) = &r.index_table {
                let t = read(path)?;
                all.push_str(&t);
                let a = AllocTable::parse(&t).map_err(|e| err!("{path}: {e}"))?;
                alloc.insert(r.name.clone(), a);
            }
        }
        crate::prim::validate_manifest(&manifest)?;
        Ok(Module {
            manifest,
            objects,
            alloc,
            digest: sha256_hex(all.as_bytes()),
        })
    }

    pub fn id(&self) -> &str {
        &self.manifest.module.id
    }

    pub fn tag(&self) -> String {
        format!(
            "{}@{}",
            self.manifest.module.id, self.manifest.module.version
        )
    }
}

#[derive(Clone, Debug, Default)]
pub struct Library {
    pub modules: Vec<Module>,
}

impl Library {
    pub fn get(&self, id: &str) -> Option<&Module> {
        self.modules.iter().find(|m| m.id() == id)
    }

    /// The selection plus everything it requires, transitively. Returns the full list (the selection first, in the
    /// order given, then the added modules in the order found) and the added ones. Refuses a module that is not in
    /// the library, a requirement that is missing, and a `requires` cycle.
    pub fn with_requires(&self, ids: &[String]) -> Result<(Vec<String>, Vec<String>)> {
        fn visit(
            lib: &Library,
            id: &str,
            stack: &mut Vec<String>,
            done: &mut Vec<String>,
        ) -> Result<()> {
            if stack.iter().any(|s| s == id) {
                return Err(err!("requires cycle: {} -> {id}", stack.join(" -> ")));
            }
            if done.iter().any(|d| d == id) {
                return Ok(());
            }
            let m = lib.get(id).ok_or_else(|| match stack.last() {
                Some(by) => err!("{by} requires {id}, which is not in the module library"),
                None => err!("unknown module {id}"),
            })?;
            stack.push(id.to_string());
            for r in &m.manifest.module.requires {
                visit(lib, r, stack, done)?;
            }
            stack.pop();
            done.push(id.to_string());
            Ok(())
        }
        let mut done: Vec<String> = vec![];
        for id in ids {
            visit(self, id, &mut vec![], &mut done)?;
        }
        let mut all: Vec<String> = vec![];
        for id in ids {
            if !all.contains(id) {
                all.push(id.clone());
            }
        }
        let added: Vec<String> = done.into_iter().filter(|d| !all.contains(d)).collect();
        all.extend(added.iter().cloned());
        Ok((all, added))
    }
}

#[cfg(not(target_arch = "wasm32"))]
impl Library {
    /// Every `module.toml` under `root` (a `modules/` directory).
    pub fn scan(root: &std::path::Path) -> Result<Library> {
        fn walk(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
            let Ok(rd) = std::fs::read_dir(dir) else {
                return;
            };
            let mut es: Vec<_> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
            es.sort();
            for p in es {
                if p.is_dir() {
                    walk(&p, out);
                } else if p.file_name().is_some_and(|n| n == "module.toml") {
                    out.push(p);
                }
            }
        }
        let mut found = vec![];
        walk(root, &mut found);
        let mut modules = vec![];
        for p in found {
            let dir = p.parent().unwrap().to_path_buf();
            let text = std::fs::read_to_string(&p).map_err(|e| err!("{}: {e}", p.display()))?;
            let read = |rel: &str| -> Result<String> {
                std::fs::read_to_string(dir.join(rel))
                    .map_err(|e| err!("{}: {e}", dir.join(rel).display()))
            };
            let m = Module::from_parts(&text, &read).map_err(|e| err!("{}: {e}", p.display()))?;
            let want = dir
                .strip_prefix(root)
                .ok()
                .map(|d| d.to_string_lossy().replace('\\', "/"));
            if want.as_deref() != Some(m.id()) {
                return Err(err!(
                    "{}: id {} does not match its directory {:?}",
                    p.display(),
                    m.id(),
                    want
                ));
            }
            modules.push(m);
        }
        Ok(Library { modules })
    }
}
