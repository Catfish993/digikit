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
    /// Resolution rules for `tools/port_resolve.py`; patchkit does not read them.
    #[serde(default)]
    pub anchor_rule: Vec<toml::Value>,
    #[serde(default)]
    pub op: Vec<Op>,
    #[serde(default)]
    pub claim: Vec<Claim>,
    /// Machine types this module adds (core/machines assigns the type numbers).
    #[serde(default)]
    pub machine: Vec<MachineDef>,
    /// Tables of rows that other modules fill with `register` ops.
    #[serde(default)]
    pub table: Vec<TableDecl>,
    /// Parameter descriptors this module adds (core/params assigns the codes).
    #[serde(default)]
    pub param: Vec<ParamDef>,
}

/// One of a machine's nine descriptor ID fields: a stock parameter code, or a parameter a selected module adds.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum FieldRef {
    Code(u32),
    /// `id` of a `[[param]]` of any selected module
    Param(String),
}

/// One parameter descriptor a content module adds: a row of the firmware's parameter table (60 bytes, finding 02)
/// copied from a stock row, with its own names, range and default. `core/params` (`params.rs`) gives it the next
/// free parameter code in canonical module order (a machine's `fields` may name it) and relocates the table.
#[derive(Clone, Debug, Deserialize)]
pub struct ParamDef {
    /// Symbol-safe name (`[a-z][a-z0-9_]*`), unique across the selection.
    pub id: String,
    /// Long name (the page header when the knob is touched).
    pub name: String,
    /// Short name (the label under the knob).
    pub short: String,
    /// Stock parameter code whose row is copied: owner, flag, kind, mask, formatter and the value-class object
    /// the UI keeps for the code (a template of the same class: plain 0..127 values copy a level row).
    pub template: u32,
    /// Descriptor field (engine slot) 0..7 the parameter is used in: it fixes the mirror index (25, 26, 27, 28,
    /// 31, 32, 33, 34, finding 02) and the slot-bound numbers of the row.
    pub slot: u32,
    pub min: u32,
    pub max: u32,
    pub default: u32,
    /// Stock parameter code whose value formatter this row uses (default: the template's).
    #[serde(default)]
    pub formatter_from: Option<u32>,
}

/// One machine type a content module adds. The framework (`core/machines`, `machines.rs`) gives it the next
/// free type number in canonical module order and exports it as the symbol `machines.<id>.type`.
#[derive(Clone, Debug, Deserialize)]
pub struct MachineDef {
    /// Symbol-safe name (`[a-z][a-z0-9_]*`), unique across the selection.
    pub id: String,
    /// Display name of the machine list (the UI upper-cases it).
    pub name: String,
    /// Short label (the SRC page header).
    pub short: String,
    /// Stock machine type whose per-type rows (permission, per-type byte, name hint) the new type copies, and
    /// whose `type == 6` tests it also matches when that is MANUAL SLICE.
    pub clone_of: u32,
    /// Index in the displayed list (0 = first); default: the end.
    #[serde(default)]
    pub position: Option<usize>,
    /// The nine descriptor ID fields (parameter descriptor IDs of the SRC page), in descriptor order.
    pub fields: Vec<FieldRef>,
    /// DSP selector word for this type (the table at `dsp.machines.selector_table`); default: the clone's.
    #[serde(default)]
    pub selector: Option<u32>,
    /// Names stored in the descriptor (default: `name` and `short` upper-cased).
    #[serde(default)]
    pub desc_name: Option<String>,
    #[serde(default)]
    pub desc_short: Option<String>,
    /// Digitone II: the DSP is told this machine's own type number (a registered voice routine renders it; finding
    /// 06). Default: the ColdFire sends the clone's type, so the FM engine renders the clone like its original.
    #[serde(default)]
    pub own_dsp: bool,
    /// Restrict to these devices (`dt2`, `dn2`); empty = all.
    #[serde(default)]
    pub devices: Vec<String>,
}

/// A table a module exports: a data symbol of `rows` rows of `row_words` 32-bit words each, all zero, in which
/// other modules register rows (`op = "register"`). A row whose first word is zero is free.
#[derive(Clone, Debug, Deserialize)]
pub struct TableDecl {
    pub symbol: String,
    pub row_words: u64,
    pub rows: u64,
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
    /// `write`, `retarget`, `jump` or `register`
    pub op: String,
    #[serde(default)]
    pub anchor: String,
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
    /// register: the exported table symbol the row goes into
    pub table: Option<String>,
    /// register: the row's words, each an integer (`7`, `0x10`) or a symbol (a module export: the pc of a code
    /// symbol, the byte address of a data symbol); the first word must be non-zero
    #[serde(default)]
    pub row: Vec<String>,
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
}

#[derive(Clone, Debug)]
pub struct Module {
    pub manifest: Manifest,
    pub objects: Vec<Dko>,
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
        for op in &manifest.op {
            match op.op.as_str() {
                "write" if op.hex.is_none() => {
                    return Err(err!("{}: write needs hex", manifest.module.id));
                }
                "retarget" | "jump" if op.to.is_none() => {
                    return Err(err!("{}: {} needs to", manifest.module.id, op.op));
                }
                "register" if op.table.is_none() || op.row.is_empty() => {
                    return Err(err!("{}: register needs table and row", manifest.module.id));
                }
                "write" | "retarget" | "jump" | "register" => {}
                o => return Err(err!("{}: unknown op {o}", manifest.module.id)),
            }
        }
        Ok(Module {
            manifest,
            objects,
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
