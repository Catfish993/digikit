//! Run-time modules: `dynamic = true` modules, the `.dkm` register records, the reserved rows of `core/dynreg`
//! (docs/design/digikit-registry.md). The image tests are skipped when the stock images are absent.

use patchkit::apply;
use patchkit::dkm;
use patchkit::module::{AllocTable, Library};
use patchkit::port::Registry;
use patchkit::register::{KNOB_PARAM, Register};
use patchkit::typed::{Models, resolve};
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn library() -> Library {
    let mut lib = Library::scan(&root().join("modules")).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    lib
}

fn dynamic_types(
    lib: &Library,
    dev: &str,
    module: &str,
) -> std::collections::BTreeMap<String, i64> {
    lib.get("core/machines").unwrap().alloc["machines"].devices[dev]
        .dynamic
        .iter()
        .filter_map(|(k, v)| {
            k.strip_prefix(&format!("{module}:"))
                .map(|n| (n.to_string(), *v))
        })
        .collect()
}

#[test]
fn a_dynamic_module_is_not_lowered_and_not_patched() {
    let lib = library();
    let m = lib.get("test/dyn-xslice").unwrap();
    assert!(m.manifest.module.dynamic);
    assert!(
        m.manifest.entry.is_empty(),
        "no registry rows are made for a run-time module"
    );
    let reg = Registry::from_dir(&root().join("devices/ports")).unwrap();
    for file in ["Digitakt_II_OS1.16.syx", "Digitakt_II_OS1.17.syx"] {
        let Ok(raw) = std::fs::read(root().join("firmware").join(file)) else {
            continue;
        };
        let e = apply::apply(&raw, &reg, &lib, &["test/dyn-xslice".to_string()])
            .err()
            .expect("refused");
        assert!(e.0.contains("run-time module"), "{e}");
    }
}

#[test]
fn the_register_records_are_the_lowered_typed_sections() {
    let lib = library();
    let m = lib.get("test/dyn-xslice").unwrap();
    let models = Models::from_dir(&root().join("devices")).unwrap();
    let res = resolve(&m.manifest, &models).unwrap();
    // only the Digitakt: the module says `devices = ["dt2"]`
    assert_eq!(
        res.iter().map(|r| r.dev.as_str()).collect::<Vec<_>>(),
        ["dt2"]
    );
    let reg = Register::lower(&res[0], &dynamic_types(&lib, "dt2", "test/dyn-xslice")).unwrap();
    assert_eq!(reg.device, 1);
    assert_eq!(reg.machines.len(), 1);
    let mach = &reg.machines[0];
    assert_eq!(
        (mach.type_no, mach.clone_of, mach.position, mach.level),
        (16, 6, 255, 10)
    );
    // knobs: XSLICE's stock codes, knob 6 the module's own parameter (index 0)
    assert_eq!(
        mach.knobs,
        [0xf8, 0xf9, 0xfa, 0xfb, 0xfc, 0xfd, KNOB_PARAM, 0xfe]
    );
    let p = &reg.params[0];
    assert_eq!(
        (p.template, p.slot, p.min, p.max, p.default),
        (0xd1, 6, 0, 0x7f00, 0x2000)
    );
    // the file: a stub image, the records, and the same bytes every time
    let opts = dkm::Options {
        name: "test/dyn-xslice".into(),
        version: 0x000100,
        runtime_min: 1,
    };
    let a = dkm::pack_with(None, &reg.encode(), &opts).unwrap();
    assert_eq!(a, dkm::pack_with(None, &reg.encode(), &opts).unwrap());
    let d = dkm::Dkm::parse(&a).unwrap();
    assert_eq!(d.init, Some(0));
    assert_eq!(Register::parse(&d.register).unwrap(), reg);
    // a machine on a device without a run-time type number is refused, with the way to fix it
    let e = Register::lower(&res[0], &Default::default()).unwrap_err();
    assert!(e.0.contains("dynamic"), "{e}");
}

#[test]
fn the_run_time_type_range_is_checked() {
    let ok = "[dt2]\nlast = 23\ndynamic_first = 16\n[dt2.alloc]\n\"a:x\" = 7\n\"core/dynreg:d\" = 16\n[dt2.dynamic]\n\"m:y\" = 17\n";
    assert!(AllocTable::parse(ok).is_ok());
    let below = ok.replace("\"m:y\" = 17", "\"m:y\" = 9");
    assert!(
        AllocTable::parse(&below)
            .unwrap_err()
            .0
            .contains("outside 16..=23")
    );
    let twice = format!("{ok}\"n:z\" = 17\n");
    assert!(
        AllocTable::parse(&twice)
            .unwrap_err()
            .0
            .contains("both have number 17")
    );
    let static_in_range = ok.replace("\"a:x\" = 7", "\"a:x\" = 18");
    assert!(
        AllocTable::parse(&static_in_range)
            .unwrap_err()
            .0
            .contains("run-time range")
    );
    let no_first = "[dt2]\nlast = 23\n[dt2.dynamic]\n\"m:y\" = 17\n";
    assert!(
        AllocTable::parse(no_first)
            .unwrap_err()
            .0
            .contains("dynamic_first")
    );
}

/// The framework reserves 8 machine types (16 .. 23) and 16 parameter codes, with the runtime's table of addresses; the
/// runtime alone still patches (its link to the table is optional).
#[test]
fn dynreg_reserves_rows_and_the_runtime_links_optionally() {
    let lib = library();
    let reg = Registry::from_dir(&root().join("devices/ports")).unwrap();
    let mut ran = 0;
    for file in ["Digitakt_II_OS1.16.syx", "Digitakt_II_OS1.17.syx"] {
        let Ok(raw) = std::fs::read(root().join("firmware").join(file)) else {
            continue;
        };
        let want = |ids: &[&str]| -> Vec<String> {
            lib.with_requires(&ids.iter().map(|s| s.to_string()).collect::<Vec<_>>())
                .unwrap()
                .0
        };
        let out = apply::apply(&raw, &reg, &lib, &want(&["core/runtime", "core/dynreg"])).unwrap();
        let plan = serde_json::to_value(&out.plan).unwrap();
        let sym = |n: &str| -> u64 {
            plan["symbols"]
                .as_object()
                .unwrap()
                .iter()
                .find(|(k, _)| k.split(':').nth(1) == Some(n))
                .unwrap_or_else(|| panic!("no symbol {n}"))
                .1
                .as_u64()
                .unwrap()
        };
        assert_eq!(sym("machines.dyn16.index"), 16);
        assert_eq!(sym("machines.dyn23.index"), 23);
        assert_eq!(sym("machines.count"), 17, "types 7 .. 23");
        assert_eq!(sym("params.dp15.index"), sym("params.dp0.index") + 15);
        assert!(sym("dynreg_info") > 0x4000_0000);
        // the runtime alone: the optional link resolves to 0, nothing else changes
        let alone = apply::apply(&raw, &reg, &lib, &want(&["core/runtime"])).unwrap();
        let alone_plan = serde_json::to_value(&alone.plan).unwrap();
        assert!(
            alone_plan["symbols"]
                .as_object()
                .unwrap()
                .keys()
                .all(|k| !k.contains("dynreg"))
        );
        ran += 1;
    }
    eprintln!("dynreg: {ran} images");
}
