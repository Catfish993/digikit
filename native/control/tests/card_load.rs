//! `card.load` through the controller: a folder of WAV files becomes a card
//! and the DT2 runtime restarts on it. Without coupled-audio inputs the
//! runtime is ColdFire-only (no ready snapshot to build); the coupled cold
//! boot and the trig audio are checked by `tools/card_check.py`.

use std::{fs, path::PathBuf};

use digi_control::{Config, spawn};
use serde_json::json;

fn wav(frequency: f64) -> Vec<u8> {
    let data: Vec<u8> = (0..2400)
        .flat_map(|i| {
            let v = (0.5
                * (std::f64::consts::TAU * frequency * f64::from(i) / 48_000.0).sin()
                * 32767.0) as i16;
            v.to_le_bytes()
        })
        .collect();
    let mut out = b"RIFF".to_vec();
    out.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    for field in [16u32, 0x0001_0001, 48_000, 96_000, 0x0010_0002] {
        out.extend_from_slice(&field.to_le_bytes());
    }
    out.extend_from_slice(b"data");
    out.extend_from_slice(&(data.len() as u32).to_le_bytes());
    out.extend_from_slice(&data);
    out
}

fn dt2_syx() -> Option<PathBuf> {
    std::env::var_os("DT2_SYX").map(PathBuf::from).or_else(|| {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .map(|dir| dir.join("Digitakt_II_OS1.16.syx"))
            .find(|path| path.exists())
    })
}

#[test]
fn card_load_needs_a_firmware_and_a_dt2() {
    let (handle, join) = spawn(Config::new(None));
    let missing = handle
        .call("card.load", json!({"dir": "/nonexistent"}))
        .unwrap_err();
    assert_eq!(missing.message, "No firmware selected");
    let status = handle.call("card.status", json!({})).unwrap();
    assert_eq!(status["state"], "idle");
    drop(handle);
    join.join().unwrap();
}

#[test]
fn card_load_builds_the_card_and_restarts_the_dt2_on_it() {
    let Some(syx) = dt2_syx() else { return };
    let dir = std::env::temp_dir().join(format!("digi-card-load-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    let samples = dir.join("samples");
    fs::create_dir_all(&samples).unwrap();
    for (name, frequency) in [("a.wav", 220.0), ("b.wav", 880.0), ("c.wav", 1760.0)] {
        fs::write(samples.join(name), wav(frequency)).unwrap();
    }
    fs::write(samples.join("notes.txt"), b"ignored").unwrap();

    let mut config = Config::new(None);
    config.run_dir = dir.join("run");
    let (handle, join) = spawn(config);
    handle
        .call("firmware.load", json!({"syx": syx.display().to_string()}))
        .unwrap();

    // A missing folder and an empty one fail with a message, and nothing changes.
    let none = handle
        .call(
            "card.load",
            json!({"dir": dir.join("none").display().to_string()}),
        )
        .unwrap_err();
    assert!(none.message.contains("not a directory"), "{}", none.message);
    let status = handle.call("card.status", json!({})).unwrap();
    assert_eq!(status["state"], "error");

    let first = handle
        .call("card.load", json!({"dir": samples.display().to_string()}))
        .unwrap();
    assert_eq!(first["state"], "ready");
    assert_eq!(first["samples"], 3);
    assert_eq!(first["audio"], false);
    assert_eq!(first["card_cached"], false);
    let card = PathBuf::from(first["card"].as_str().unwrap());
    assert!(card.starts_with(dir.join("run").join("cards")));
    assert_eq!(fs::metadata(&card).unwrap().len() % 512, 0);
    assert_eq!(
        handle.call("card.status", json!({})).unwrap()["state"],
        "ready"
    );

    // The same folder again reuses the card (a changed file changes the key: cards::tests).
    let again = handle
        .call("card.load", json!({"dir": samples.display().to_string()}))
        .unwrap();
    assert_eq!(again["card_cached"], true);
    assert_eq!(again["key"], first["key"]);

    // The runtime is the DT2's, on the new card.
    let status = handle.call("status", json!({})).unwrap();
    assert_eq!(status["cf"]["device"], "dt2");
    handle.call("quit", json!({})).unwrap();
    drop(handle);
    join.join().unwrap();
    let _ = fs::remove_dir_all(&dir);
}
