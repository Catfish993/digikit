//! Device data: the constants `patchkit gen` writes from the device files agree with them (the files are committed, and a
//! stale one fails here as it fails `patchkit gen --check`), a fact that devices disagree on is refused for the shared runtime,
//! and the device list of the code is the device files' list.

use patchkit::devdata;
use patchkit::devgen::{self, Facts};
use patchkit::modgen;
use patchkit::module::Library;
use patchkit::port::Registry;
use patchkit::typed::Models;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn every_generated_file_is_current() {
    let mut lib = Library::scan(&root().join("modules")).unwrap();
    let models = Models::from_dir(&root().join("devices")).unwrap();
    lib.lower(&models).unwrap();
    let facts = Facts::from_dir(&root().join("devices")).unwrap();
    let mut n = 0;
    for m in &lib.modules {
        let mut files = modgen::generate(&lib, &models, m.id()).unwrap();
        files.extend(devgen::generate(&lib, &facts, m.id()).unwrap());
        for (rel, text) in files {
            let path = root().join("modules").join(m.id()).join(&rel);
            let have = std::fs::read_to_string(&path).unwrap_or_default();
            assert_eq!(have, text, "{} is stale: run `patchkit gen`", path.display());
            n += 1;
        }
    }
    assert!(n > 20, "only {n} generated files");
}

#[test]
fn the_runtime_constants_are_the_device_files_numbers() {
    let mut lib = Library::scan(&root().join("modules")).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap()).unwrap();
    let facts = Facts::from_dir(&root().join("devices")).unwrap();
    let files = devgen::generate(&lib, &facts, "core/runtime").unwrap();
    let get = |p: &str| &files.iter().find(|f| f.0 == p).unwrap().1;
    let rs = get("sdk/src/device.rs");
    // the numbers of devices/*.toml [card.store] (both devices) and [board]
    for want in [
        "STORE_FIRST: u32 = 0x38000;",
        "STORE_SECTORS: u32 = 0x6000;",
        "STORE_HEADER_B: u32 = 0x5ffd;",
        "STORE_SEL_A: u32 = 0x3dffe;",
        "STORE_SEL_B: u32 = 0x3dfff;",
        "DYN_TYPE_FIRST: u32 = 0x10;",
        "DYN_TYPE_LAST: u32 = 0x17;",
        "SAFE_KEY_CODE: u32 = 0x13;",
        "DSP_BLOCK_CYCLES: u32 = 0xa2c2b;",
    ] {
        assert!(rs.contains(want), "{want}");
    }
    let dn2 = get("build/gen/device-dn2.inc");
    assert!(dn2.contains(".equ DEVICE_ID, 0x2 "), "{dn2}");
    assert!(dn2.contains(".equ DL_AREA_BYTES, 0x50000 "), "{dn2}");
}

#[test]
fn a_fact_that_devices_disagree_on_is_refused_for_the_shared_runtime() {
    let mut lib = Library::scan(&root().join("modules")).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap()).unwrap();
    let dt2 = std::fs::read_to_string(root().join("devices/digitakt-ii.toml")).unwrap();
    let dn2 = std::fs::read_to_string(root().join("devices/digitone-ii.toml")).unwrap();
    // move the Digitone's store one sector up (and keep its own checks happy)
    let moved = dn2.replace("first_sector = 0x38000", "first_sector = 0x38001");
    let facts = Facts::from_texts([dt2.as_str(), moved.as_str()]).unwrap();
    let e = devgen::generate(&lib, &facts, "core/runtime").unwrap_err().0;
    assert!(e.contains("STORE_FIRST") && e.contains("one image for all devices"), "{e}");
    // per-device files (the device objects) are still fine for numbers that differ
    let ok = Facts::from_texts([dt2.as_str(), dn2.as_str()]).unwrap();
    assert!(devgen::generate(&lib, &ok, "core/dsp-link").is_ok());
}

#[test]
fn the_code_knows_the_devices_of_the_device_files() {
    // every port names a device the code knows, and the limits a port records are the device's `[patch]` limits
    let reg = Registry::from_dir(&root().join("devices/ports")).unwrap();
    assert!(!reg.ports.is_empty());
    for p in &reg.ports {
        let d = devdata::by_short(&p.port.device).unwrap_or_else(|| panic!("{}", p.id()));
        assert_eq!(p.limits.container_max, d.container_max, "{}", p.id());
        assert_eq!(p.limits.dsp_stored_max, d.dsp_stored_max, "{}", p.id());
        assert_eq!(p.limits.dsp_depacked_max, d.dsp_depacked_max, "{}", p.id());
        assert_eq!(p.limits.main_staged_max, d.main_staged_max, "{}", p.id());
    }
    // ids are unique per kind
    let all = devdata::all();
    for (i, a) in all.iter().enumerate() {
        for b in &all[i + 1..] {
            assert_ne!((a.syx_id, a.digikit_id), (b.syx_id, b.digikit_id));
            assert_ne!(a.digikit_id, b.digikit_id);
        }
    }
}
