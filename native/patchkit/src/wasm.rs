//! A C ABI over the same `apply` and `verify` the command line uses, for the browser (`wasm32`). No
//! wasm-bindgen: the page allocates buffers with `pk_alloc`, passes the stock `.syx`, a JSON bundle of
//! ports and modules, and a comma-separated module list, and reads the results back by pointer and length.
//!
//! Bundle: `{"ports": ["<port toml>", ...], "modules": [{"manifest": "<module.toml>", "files": {"build/x.dko.json": "<json>"}}]}`.
//! (`tools/patchkit_bundle.py` and the gallery's build step make it from `devices/ports/` and `modules/`.)

use crate::apply;
use crate::error::{Error, Result, sha256_hex};
use crate::module::{Library, Module};
use crate::port::Registry;
use serde::Deserialize;
use std::cell::RefCell;
use std::collections::BTreeMap;

#[derive(Deserialize)]
struct Bundle {
    ports: Vec<String>,
    modules: Vec<BundleModule>,
}

#[derive(Deserialize)]
struct BundleModule {
    manifest: String,
    files: BTreeMap<String, String>,
}

#[derive(Default)]
struct Out {
    syx: Vec<u8>,
    plan: String,
    err: String,
}

thread_local! {
    static OUT: RefCell<Out> = RefCell::new(Out::default());
}

fn load(bundle: &[u8]) -> Result<(Registry, Library)> {
    let b: Bundle = serde_json::from_slice(bundle)?;
    let reg = Registry::from_texts(b.ports.iter().map(|s| s.as_str()))?;
    let mut modules = vec![];
    for m in &b.modules {
        let read = |p: &str| -> Result<String> {
            m.files
                .get(p)
                .cloned()
                .ok_or_else(|| Error(format!("bundle: missing file {p}")))
        };
        modules.push(Module::from_parts(&m.manifest, &read)?);
    }
    Ok((reg, Library { modules }))
}

/// Apply the modules and return the patched image and the plan (for tests and the C ABI).
pub fn apply_bundle(fw: &[u8], bundle: &[u8], ids: &str) -> Result<(Vec<u8>, String)> {
    let (reg, lib) = load(bundle)?;
    let ids: Vec<String> = ids
        .split(',')
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();
    let out = apply::apply(fw, &reg, &lib, &ids)?;
    let plan = serde_json::to_string_pretty(&out.plan)? + "\n";
    Ok((out.syx, plan))
}

/// Identify an image against the bundle's ports: JSON `{device, sha256, port, record}`.
pub fn inspect_bundle(fw: &[u8], bundle: &[u8]) -> Result<String> {
    let (reg, _) = load(bundle)?;
    let f = crate::firmware::Firmware::parse(fw)?;
    let rep = crate::verify::verify(fw, Some(&reg), None)?;
    Ok(serde_json::json!({
        "device": f.device.short(),
        "sha256": f.syx_sha256,
        "port": reg.find(&f.syx_sha256).map(|p| p.id()),
        "record": rep.record,
        "sections": rep.sections,
    })
    .to_string())
}

/// Which modules of the bundle are usable on this image: JSON `{device, sha256, port, modules: [{id, ok, error}]}`.
/// `port` is null when no port has the image's hash (then every module is refused).
pub fn check_bundle(fw: &[u8], bundle: &[u8]) -> Result<String> {
    let (reg, lib) = load(bundle)?;
    let f = crate::firmware::Firmware::parse(fw)?;
    let port = reg.find(&f.syx_sha256);
    let modules: Vec<_> = lib
        .modules
        .iter()
        .map(|m| {
            let r = match port {
                Some(p) => apply::compatible(m, p),
                None => Err(err!("no port for firmware {}", f.syx_sha256)),
            };
            serde_json::json!({"id": m.id(), "ok": r.is_ok(), "error": r.err().map(|e| e.0)})
        })
        .collect();
    Ok(serde_json::json!({
        "device": f.device.short(),
        "sha256": f.syx_sha256,
        "port": port.map(|p| p.id()),
        "modules": modules,
    })
    .to_string())
}

fn set(r: Result<(Vec<u8>, String)>) -> i32 {
    OUT.with(|o| {
        let mut o = o.borrow_mut();
        match r {
            Ok((syx, plan)) => {
                *o = Out {
                    syx,
                    plan,
                    err: String::new(),
                };
                0
            }
            Err(e) => {
                *o = Out {
                    err: e.0,
                    ..Out::default()
                };
                1
            }
        }
    })
}

/// # Safety
/// `p..p+n` must be a readable range.
unsafe fn slice<'a>(p: *const u8, n: usize) -> &'a [u8] {
    if n == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(p, n) }
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn pk_alloc(n: usize) -> *mut u8 {
    let mut v = Vec::<u8>::with_capacity(n.max(1));
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `p` and `n` must be a pointer from `pk_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pk_free(p: *mut u8, n: usize) {
    drop(unsafe { Vec::from_raw_parts(p, 0, n.max(1)) });
}

/// Apply. 0 on success (read `pk_out_*` and `pk_plan_*`), 1 on failure (read `pk_err_*`).
///
/// # Safety
/// The three ranges must be readable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pk_apply(
    fw: *const u8,
    fw_len: usize,
    bundle: *const u8,
    bundle_len: usize,
    ids: *const u8,
    ids_len: usize,
) -> i32 {
    let (fw, bundle, ids) = unsafe {
        (
            slice(fw, fw_len),
            slice(bundle, bundle_len),
            slice(ids, ids_len),
        )
    };
    let ids = String::from_utf8_lossy(ids).to_string();
    set(apply_bundle(fw, bundle, &ids))
}

/// Identify an image; the JSON is in `pk_plan_*`.
///
/// # Safety
/// The two ranges must be readable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pk_inspect(
    fw: *const u8,
    fw_len: usize,
    bundle: *const u8,
    bundle_len: usize,
) -> i32 {
    let (fw, bundle) = unsafe { (slice(fw, fw_len), slice(bundle, bundle_len)) };
    set(inspect_bundle(fw, bundle).map(|j| (vec![], j)))
}

/// Per-module compatibility with an image; the JSON is in `pk_plan_*`.
///
/// # Safety
/// The two ranges must be readable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pk_check(
    fw: *const u8,
    fw_len: usize,
    bundle: *const u8,
    bundle_len: usize,
) -> i32 {
    let (fw, bundle) = unsafe { (slice(fw, fw_len), slice(bundle, bundle_len)) };
    set(check_bundle(fw, bundle).map(|j| (vec![], j)))
}

/// Verify an image (and, if `base_len` is not 0, against that stock image); the report JSON is in `pk_plan_*`.
///
/// # Safety
/// The ranges must be readable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pk_verify(
    syx: *const u8,
    syx_len: usize,
    bundle: *const u8,
    bundle_len: usize,
    base: *const u8,
    base_len: usize,
) -> i32 {
    let (syx, bundle, base) = unsafe {
        (
            slice(syx, syx_len),
            slice(bundle, bundle_len),
            slice(base, base_len),
        )
    };
    let r = load(bundle).and_then(|(reg, _)| {
        let rep = crate::verify::verify(syx, Some(&reg), (!base.is_empty()).then_some(base))?;
        Ok((vec![], serde_json::to_string(&rep)?))
    });
    set(r)
}

macro_rules! getter {
    ($ptr:ident, $len:ident, $field:ident) => {
        #[unsafe(no_mangle)]
        pub extern "C" fn $ptr() -> *const u8 {
            OUT.with(|o| o.borrow().$field.as_ptr())
        }
        #[unsafe(no_mangle)]
        pub extern "C" fn $len() -> usize {
            OUT.with(|o| o.borrow().$field.len())
        }
    };
}
getter!(pk_out_ptr, pk_out_len, syx);
getter!(pk_plan_ptr, pk_plan_len, plan);
getter!(pk_err_ptr, pk_err_len, err);

/// sha256 of the bytes in `pk_out_*`, hex (for the page to show without hashing again).
pub fn out_sha256() -> String {
    OUT.with(|o| sha256_hex(&o.borrow().syx))
}
