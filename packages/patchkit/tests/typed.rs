//! Typed module sections (`[[machine]]`, `[[param]]`, `[[page]]`, `[[voice]]`) over the real device models, ports and
//! stock images (the image tests are skipped when the images are absent).
//!
//! The pins are the SHA-256 of the images that the hand-written `[[entry]]` rows of `machines/pluck` and
//! `machines/xslice` produced before they were re-expressed in the typed form (commit 8606b7f); the typed form must
//! lower to the same rows, so the patched images must stay byte-identical on every port the modules support.

use patchkit::apply;
use patchkit::error::sha256_hex;
use patchkit::modgen;
use patchkit::module::Library;
use patchkit::port::Registry;
use patchkit::typed::Models;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn library() -> Library {
    Library::scan(&root().join("modules")).unwrap()
}

/// (stock image, modules, sha256 of the patched image)
const PINS: &[(&str, &str, &str)] = &[
    // The pluck pins moved on 2026-10-18 with core/dsp-voice (findings 45 and 48, twice): section 7 only; section 3, which holds the typed rows, is
    // byte-identical to the previous pins (checked), and the xslice pins did not move.
    (
        "Digitakt_II_OS1.16.syx",
        "machines/pluck",
        "56dd780e8171dcdab78bf5bef5410335f8d01fd93d6d26f81aeb606199931f54",
    ),
    (
        "Digitakt_II_OS1.16.syx",
        "machines/xslice",
        "5c0268833c64ac10abb6227ee268be97fdca9712dc6dad5269eb3c711694e0bc",
    ),
    (
        "Digitakt_II_OS1.16.syx",
        "machines/pluck,machines/xslice",
        "46f26c5468586601ff472396a88eafd0d43ee9025e10e36c20524dc9d6cebd93",
    ),
    (
        "Digitakt_II_OS1.17.syx",
        "machines/pluck",
        "4355d7ac552123ee37f26c19cf684bb07c3477a52e2144557b7ea48f5b9df57c",
    ),
    (
        "Digitakt_II_OS1.17.syx",
        "machines/xslice",
        "c0948bb2ece9df0d6bf15fbb7eb97bfa5635cad3346803688ac05c48a1775a07",
    ),
    (
        "Digitakt_II_OS1.17.syx",
        "machines/pluck,machines/xslice",
        "47c4258ef79c1be3131986f5f11bfd83b00247413005a2e41e0df14646717e00",
    ),
    (
        "Digitone_II_OS1.12.syx",
        "machines/pluck",
        "e7dc4da5f7850062ef8510ff5d389a779fb94d8efbea72184c9f6795d498d0aa",
    ),
];

#[test]
fn typed_form_patches_to_the_images_of_the_hand_written_rows() {
    let lib = library();
    let reg = Registry::from_dir(&root().join("devices/ports")).unwrap();
    let mut ran = 0;
    for (file, mods, want) in PINS {
        let Ok(raw) = std::fs::read(root().join("firmware").join(file)) else {
            continue;
        };
        let ids: Vec<String> = mods.split(',').map(String::from).collect();
        let (ids, _) = lib.with_requires(&ids).unwrap();
        let out = apply::apply(&raw, &reg, &lib, &ids).unwrap();
        assert_eq!(&sha256_hex(&out.syx), want, "{file} + {mods}");
        ran += 1;
    }
    eprintln!("typed oracle: {ran} of {} pins ran", PINS.len());
}

#[test]
fn xslice_stays_off_the_digitone() {
    let lib = library();
    let reg = Registry::from_dir(&root().join("devices/ports")).unwrap();
    let Ok(raw) = std::fs::read(root().join("firmware/Digitone_II_OS1.12.syx")) else {
        return;
    };
    let ids = lib
        .with_requires(&["machines/xslice".to_string()])
        .unwrap()
        .0;
    let Err(e) = apply::apply(&raw, &reg, &lib, &ids) else {
        panic!("xslice applied to a Digitone II");
    };
    assert!(e.0.contains("not compatible"), "{e}");
}

/// The committed `build/gen/` files equal fresh ones (`patchkit gen` rewrites them).
#[test]
fn generated_files_are_current() {
    let lib = library();
    let models = Models::from_dir(&root().join("devices")).unwrap();
    let mut n = 0;
    for m in &lib.modules {
        for (rel, text) in modgen::generate(&lib, &models, m.id()).unwrap() {
            let path = root().join("modules").join(m.id()).join(&rel);
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
    assert!(n >= 7, "pluck and xslice generate files, got {n}");
}

/// What pluck's DSP sources hard-code must be what the device models say (finding 04: frame words of the machine
/// slots; pluck.s: `0x6d + 48 t`, offsets 1, 2, 6, 7; pluck_dn2.s: `0x6d + 0x49 v`, offsets 0 .. 3).
#[test]
fn device_models_match_the_hard_coded_frame_layout_of_pluck() {
    let lib = library();
    let models = Models::from_dir(&root().join("devices")).unwrap();
    let files = modgen::generate(&lib, &models, "machines/pluck").unwrap();
    let g: serde_json::Value = serde_json::from_str(
        &files
            .iter()
            .find(|f| f.0.ends_with("gallery.json"))
            .unwrap()
            .1,
    )
    .unwrap();
    let dt2 = &g["machines"][0]["devices"]["dt2"];
    assert_eq!(dt2["type"], 9);
    assert_eq!(dt2["knobs"][1]["param"], "pluck_decay");
    let inc = |dev: &str| {
        files
            .iter()
            .find(|f| f.0.ends_with(&format!("{dev}.inc")))
            .unwrap()
            .1
            .clone()
    };
    let (dt2, dn2) = (inc("dt2"), inc("dn2"));
    for want in [
        "#define FRAME_BASE 109 ",
        "#define FRAME_STRIDE 48 ",
        "#define PLUCK_DECAY_FRAME_OFF 1 ",
        "#define PLUCK_PICK_FRAME_OFF 2 ",
        "#define PLUCK_BRIGHT_FRAME_OFF 6 ",
        "#define PLUCK_COLOR_FRAME_OFF 7 ",
        "#define PLUCK_DECAY_MIRROR 26 ",
        "#define PLUCK_COLOR_MIRROR 32 ",
    ] {
        assert!(dt2.contains(want), "dt2: {want}");
    }
    for want in [
        "#define FRAME_STRIDE 73 ",
        "#define PLUCK_DECAY_FRAME_OFF 0 ",
        "#define PLUCK_COLOR_FRAME_OFF 3 ",
        "#define PLUCK_DECAY_SLOT 25 ",
        "#define PLUCK_COLOR_SLOT 28 ",
    ] {
        assert!(dn2.contains(want), "dn2: {want}");
    }
}

/// The bundle path of the browser: the same lowering from texts (ports, manifests, objects, device files).
#[test]
fn bundle_lowering_matches_the_scan() {
    let mut texts = vec![];
    for f in ["digitakt-ii.toml", "digitone-ii.toml"] {
        texts.push(std::fs::read_to_string(root().join("devices").join(f)).unwrap());
    }
    let models = Models::from_texts(texts.iter().map(|s| s.as_str())).unwrap();
    let dir = root().join("modules/machines/pluck");
    let text = std::fs::read_to_string(dir.join("module.toml")).unwrap();
    let read =
        |p: &str| std::fs::read_to_string(dir.join(p)).map_err(|e| patchkit::Error(e.to_string()));
    let m = patchkit::module::Module::from_parts(&text, &read).unwrap();
    let mut lib = Library { modules: vec![m] };
    lib.lower(&models).unwrap();
    let scanned = library();
    let a: Vec<_> = lib
        .get("machines/pluck")
        .unwrap()
        .manifest
        .entry
        .iter()
        .map(|e| (&e.registry, &e.id, &e.devices, &e.fields))
        .collect();
    let b: Vec<_> = scanned
        .get("machines/pluck")
        .unwrap()
        .manifest
        .entry
        .iter()
        .map(|e| (&e.registry, &e.id, &e.devices, &e.fields))
        .collect();
    assert_eq!(a, b);
    assert_eq!(
        a.len(),
        1 + 1 + 4 + 1 + 1 + 4 + 1,
        "dt2: voice, machine, 4 params; dn2: voice, machine, 4 params, page"
    );
}
