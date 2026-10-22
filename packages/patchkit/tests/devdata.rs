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
    let mut lib = Library::scan_repo(&root()).unwrap();
    let models = Models::from_dir(&root().join("devices")).unwrap();
    lib.lower(&models).unwrap();
    let facts = Facts::from_dir(&root().join("devices")).unwrap();
    let mut n = 0;
    for m in &lib.modules {
        let mut files = modgen::generate(&lib, &models, m.id()).unwrap();
        files.extend(devgen::generate(&lib, &facts, m.id()).unwrap());
        for (rel, text) in files {
            let path = m.dir.join(&rel);
            let have = std::fs::read_to_string(&path).unwrap_or_default();
            assert_eq!(
                have,
                text,
                "{} is stale: run `patchkit gen`",
                path.display()
            );
            n += 1;
        }
    }
    assert!(n > 20, "only {n} generated files");
}

#[test]
fn the_runtime_constants_are_the_device_files_numbers() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
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
        "KEEP_BASE: u32 = 0x47fe0000;",
        "KEEP_BYTES: u32 = 0x1000;",
        "LIMIT_FX_SLOTS: u32 = 0x4;",
    ] {
        assert!(rs.contains(want), "{want}");
    }
    let dn2 = get("build/gen/device-dn2.inc");
    assert!(dn2.contains(".equ DEVICE_ID, 0x2 "), "{dn2}");
    assert!(dn2.contains(".equ DL_AREA_BYTES, 0x50000 "), "{dn2}");
    // the PEEK ranges are per device (the runtime's region differs), up to four, unused ones 0, 0
    assert!(dn2.contains(".equ PEEK_N, 0x2 "), "{dn2}");
    assert!(dn2.contains(".equ PEEK_LO_1, 0x46712000 "), "{dn2}");
    assert!(dn2.contains(".equ PEEK_HI_3, 0x0 "), "{dn2}");
    let dt2 = get("build/gen/device-dt2.inc");
    assert!(
        dt2.contains(".equ PEEK_LO_1, 0x47e8b000 ") && dt2.contains(".equ PEEK_HI_1, 0x47fe1000 "),
        "{dt2}"
    );
}

#[test]
fn peek_and_keep_ranges_are_checked() {
    let dt2 = std::fs::read_to_string(root().join("devices/digitakt-ii.toml")).unwrap();
    let dn2 = std::fs::read_to_string(root().join("devices/digitone-ii.toml")).unwrap();
    for (what, bad) in [
        (
            "an empty range",
            dt2.replace("[0x40000000, 0x47e30000]", "[0x40000000, 0x40000000]"),
        ),
        (
            "an unaligned range",
            dt2.replace("[0x40000000, 0x47e30000]", "[0x40000001, 0x47e30000]"),
        ),
        (
            "five ranges",
            dt2.replace("peek = [", "peek = [[4, 8], [8, 12], [12, 16], [16, 20], "),
        ),
        (
            "a keep area that is not a page multiple",
            dt2.replace("keep_bytes = 0x1000", "keep_bytes = 0x800"),
        ),
    ] {
        assert_ne!(bad, dt2, "{what}: the test text did not change");
        assert!(
            Facts::from_texts([bad.as_str(), dn2.as_str()]).is_err(),
            "{what} is accepted"
        );
    }
}

#[test]
fn a_fact_that_devices_disagree_on_is_refused_for_the_shared_runtime() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    let dt2 = std::fs::read_to_string(root().join("devices/digitakt-ii.toml")).unwrap();
    let dn2 = std::fs::read_to_string(root().join("devices/digitone-ii.toml")).unwrap();
    // move the Digitone's store one sector up (and keep its own checks happy)
    let moved = dn2.replace("first_sector = 0x38000", "first_sector = 0x38001");
    let facts = Facts::from_texts([dt2.as_str(), moved.as_str()]).unwrap();
    let e = devgen::generate(&lib, &facts, "core/runtime")
        .unwrap_err()
        .0;
    assert!(
        e.contains("STORE_FIRST") && e.contains("one image for all devices"),
        "{e}"
    );
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

fn device_texts() -> (String, String) {
    (
        std::fs::read_to_string(root().join("devices/digitakt-ii.toml")).unwrap(),
        std::fs::read_to_string(root().join("devices/digitone-ii.toml")).unwrap(),
    )
}

#[test]
fn a_limit_above_the_contract_maximum_is_refused() {
    let max = patchkit::sdkgen::limits_max();
    let (dt2, dn2) = device_texts();
    // the committed files are within the maxima (and the maxima are the ones this test knows)
    assert_eq!(
        (
            max.modules,
            max.map_entries,
            max.fx_slots,
            max.param_rows,
            max.page_rows
        ),
        (8, 8, 4, 16, 8)
    );
    Facts::from_texts([dt2.as_str(), dn2.as_str()]).unwrap();
    for (what, from, to) in [
        ("modules", "modules = 8", "modules = 9"),
        ("fx_slots", "fx_slots = 4", "fx_slots = 5"),
        ("param_rows", "param_rows = 16", "param_rows = 17"),
        (
            "dsp_area_bytes",
            "dsp_area_bytes = 524288",
            "dsp_area_bytes = 524292",
        ),
    ] {
        let bad = dt2.replace(from, to);
        assert_ne!(bad, dt2, "{what}: the test text did not change");
        let e = Facts::from_texts([bad.as_str(), dn2.as_str()])
            .unwrap_err()
            .0;
        assert!(
            e.contains(what) && e.contains("contract maximum"),
            "{what}: {e}"
        );
    }
    let bad = dn2.replace("page_rows = 8", "page_rows = 9");
    assert!(Facts::from_texts([dt2.as_str(), bad.as_str()]).is_err());
    // a value below the maximum is a device choice and is accepted
    let less = dt2.replace("fx_slots = 4", "fx_slots = 2");
    Facts::from_texts([less.as_str(), dn2.as_str()]).unwrap();
}

#[test]
fn machine_rows_are_the_run_time_machine_types_of_types_toml() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    let (dt2, dn2) = device_texts();
    let bad = dt2.replace("machine_rows = 8", "machine_rows = 7");
    assert_ne!(bad, dt2);
    let facts = Facts::from_texts([bad.as_str(), dn2.as_str()]).unwrap();
    let e = devgen::generate(&lib, &facts, "core/runtime")
        .unwrap_err()
        .0;
    assert!(
        e.contains("machine_rows") && e.contains("types.toml"),
        "{e}"
    );
}

#[test]
fn the_reserved_parameter_and_page_rows_are_the_profile_limits() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    let dynreg = lib.get("core/dynreg").unwrap();
    let count = |registry: &str, dev: &str| {
        dynreg
            .manifest
            .entry
            .iter()
            .filter(|e| {
                e.registry == registry
                    && (e.devices.is_empty() || e.devices.iter().any(|d| d == dev))
            })
            .count() as u64
    };
    for (file, dev) in [("digitakt-ii", "dt2"), ("digitone-ii", "dn2")] {
        let t: toml::Value = toml::from_str(
            &std::fs::read_to_string(root().join(format!("devices/{file}.toml"))).unwrap(),
        )
        .unwrap();
        let l = &t["limits"];
        assert_eq!(
            count("params", dev),
            l["param_rows"].as_integer().unwrap() as u64,
            "{dev} parameter rows of core/dynreg"
        );
        assert_eq!(
            count("pages", dev),
            l["page_rows"].as_integer().unwrap() as u64,
            "{dev} page rows of core/dynreg"
        );
        assert_eq!(
            count("machines", dev),
            l["machine_rows"].as_integer().unwrap() as u64,
            "{dev} machine rows of core/dynreg"
        );
    }
}

#[test]
fn the_link_width_is_a_shared_c_header_from_the_contract_maximum() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    let facts = Facts::from_dir(&root().join("devices")).unwrap();
    let files = devgen::generate(&lib, &facts, "core/dsp-link").unwrap();
    let h = &files
        .iter()
        .find(|f| f.0 == "build/gen/dl-limits.h")
        .expect("a c-shared file")
        .1;
    // 2^17 words of 4 bytes: the offset field of a chunk; not a device's area (DN2 has 0x50000)
    assert!(h.contains("#define DL_AREA_MAX_BYTES 0x80000u"), "{h}");
    assert!(h.contains("#define DL_AREA_MAX_WORDS 0x20000u"), "{h}");
}

/// The modules of the rt021 flash images (`out/patch/rt021-dt2-1.17.plan.json`, `modules`), by id.
const RT021_MODULES: [&str; 10] = [
    "core/digikit-menu",
    "core/dsp-link",
    "core/dsp-voice",
    "core/dyndsp",
    "core/dynreg",
    "core/expansion",
    "core/health",
    "core/machines",
    "core/params",
    "core/runtime",
];

#[test]
fn the_patch_sets_are_the_rt021_flash_modules() {
    let lib = Library::scan_repo(&root()).unwrap();
    for dev in devdata::all() {
        // the device set is the flash modules and core/input (the REMOTE switch of the DIGIKIT menu starts off); the emulator set adds core/remote-open (it starts on)
        for (set, extra) in [
            ("device", vec!["core/input"]),
            ("emulator", vec!["core/input", "core/remote-open"]),
        ] {
            let roots = dev.patch_set(set).unwrap();
            let (mut all, _) = lib.with_requires(roots).unwrap();
            all.sort();
            let mut want: Vec<&str> = RT021_MODULES.to_vec();
            want.extend(extra);
            want.sort();
            assert_eq!(all, want, "{} set {set}", dev.short);
        }
        assert!(dev.patch_set("nope").is_err());
    }
}

#[test]
fn the_input_facts_are_the_device_files_panel() {
    let mut lib = Library::scan_repo(&root()).unwrap();
    lib.lower(&Models::from_dir(&root().join("devices")).unwrap())
        .unwrap();
    let facts = Facts::from_dir(&root().join("devices")).unwrap();
    let files = devgen::generate(&lib, &facts, "core/input").unwrap();
    let get = |p: &str| &files.iter().find(|f| f.0 == p).unwrap().1;
    // DT2 lists 49 = [6, 1] and 50 = [6, 0]; DN2 adds 51 to 54 = [6, 4 to 7]
    for (dev, wants) in [
        (
            "dt2",
            vec![
                "PANEL_EXC_N, 0x2 ",
                "PANEL_EXC_CODE_0, 0x31 ",
                "PANEL_EXC_BIT_0, 0x1 ",
                "PANEL_EXC_CODE_1, 0x32 ",
                "PANEL_EXC_BIT_1, 0x0 ",
            ],
        ),
        (
            "dn2",
            vec![
                "PANEL_EXC_N, 0x6 ",
                "PANEL_EXC_CODE_5, 0x36 ",
                "PANEL_EXC_BIT_5, 0x7 ",
            ],
        ),
    ] {
        let text = get(&format!("build/gen/input-{dev}.inc"));
        for want in [
            "INPUT_BACKEND, 0x1 ",
            "PANEL_LINEAR, 0x6 ",
            "PANEL_ENCODERS, 0x9 ",
            "PANEL_BUTTON_CHANNELS, 0x7 ",
        ]
        .into_iter()
        .chain(wants)
        {
            assert!(
                text.contains(&format!(".equ {want}")),
                "{dev}: {want}\n{text}"
            );
        }
    }
}

#[test]
fn an_unknown_backend_or_a_bad_panel_exception_is_refused() {
    let (dt2, dn2) = device_texts();
    for (what, from, to) in [
        (
            "backend",
            "backend = \"panel-uart\"",
            "backend = \"carrier-pigeon\"",
        ),
        ("exceptions", "49 = [6, 1]", "49 = [3, 1]"),
        ("exceptions", "49 = [6, 1]", "49 = [6, 9]"),
        // a channel at or above button_channels is outside the decoder's tables
        ("exceptions", "49 = [6, 1]", "49 = [7, 1]"),
        (
            "button_channels",
            "button_channels = 7",
            "button_channels = 5",
        ),
    ] {
        let bad = dt2.replace(from, to);
        assert_ne!(bad, dt2, "{what}: the test text did not change");
        let e = Facts::from_texts([bad.as_str(), dn2.as_str()])
            .unwrap_err()
            .0;
        assert!(e.contains(what), "{what}: {e}");
    }
}
