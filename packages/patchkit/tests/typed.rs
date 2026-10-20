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
    // All Digitakt II pins moved on 2026-10-18 (lane r1, finding 50) with the SRC-page hook of core/machines (section 3 only); the Digitone II pin did not.
    // The pluck pins moved on 2026-10-18 with core/dsp-voice (findings 45 and 48, twice): section 7 only; section 3, which holds the typed rows, is
    // byte-identical to the previous pins (checked), and the xslice pins did not move.
    // The pluck pins moved again on 2026-10-19 (finding 49 section 10): core/dsp-voice stores the DAG1 registers through a data register on both
    // devices; section 7 only (sections 2, 3, 4, 5, 8 of the old and new patched images compared equal).
    // The six Digitakt II pins moved again on 2026-10-19 (lane srcpage, finding 50 section "One page"): the SRC-page hook is now the first test of
    // SourcePageView::vfunc_4 and cuts the page vector; section 3 only (sections 2, 4, 5, 7, 8 of the old and new dt2-1.17 + machines/pluck images compared
    // equal); the Digitone II pin did not move.
    // The five pins with machines/pluck moved on 2026-10-19 (lane b, finding 51): the runtime guard of core/dsp-voice (voice rows of 16 words, the guard code, the
    // wider diagnostic block, both devices) and the three clamps of pluck.s / pluck_dn2.s. Section 7 only: sections 1 to 6 and 8 of the old (a80274dc) and new patched
    // images compared equal for each of the five (port_resolve.sections_of); the xslice-only pins did not move.
    // The four Digitakt II pins with machines/pluck moved on 2026-10-19 (pluck.s clears its 4096-sample line at note-on): section 7 only (the other sections of the old and
    // new patched images compared equal, port_resolve.sections_of); the Digitone II pin and the xslice-only pins did not move.
    // The three pins with machines/pluck moved on 2026-10-20 (SDK P3b-1): core/dsp-voice has the machine adapter (24-word voice rows, the contract path on both devices, the
    // Digitone II guard) and machines/pluck is one contract source (pluck.s; pluck_dn2.s is gone) with a patched-in state block. Section 7 only: all other sections of the old
    // (0713be96) and new patched images compared equal for each of the three (port_resolve.sections_of); the xslice-only pin did not move.
    (
        "Digitakt_II_OS1.17.syx",
        "machines/pluck",
        "22ce1babd0c0f2319e8cf6304973435d7156caf52f110a5e3534312de26ce24d",
    ),
    (
        "Digitakt_II_OS1.17.syx",
        "machines/xslice",
        "0ab82cf272a9742d0bd6764bf741b0b1f340641235bb8c4a01b48f2611bd4a18",
    ),
    (
        "Digitakt_II_OS1.17.syx",
        "machines/pluck,machines/xslice",
        "90fb69271d6bf317775fbde42cdc4524653fc8db6b22613ef380eff87e9e3702",
    ),
    (
        "Digitone_II_OS1.12.syx",
        "machines/pluck",
        "846333968cccdfa3351e57e5c65cb2bf9f824b3d4dbb868d4fcb06c477005938",
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
