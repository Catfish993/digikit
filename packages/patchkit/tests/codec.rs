//! Codec checks against the stock images in `firmware/` (skipped when absent).

use patchkit::firmware::Firmware;

const FILES: [&str; 4] = [
    "Digitakt_II_OS1.16.syx",
    "Digitakt_II_OS1.17.syx",
    "Digitone_II_OS1.11.syx",
    "Digitone_II_OS1.12.syx",
];

fn read(name: &str) -> Option<Vec<u8>> {
    std::fs::read(format!("{}/../../firmware/{name}", env!("CARGO_MANIFEST_DIR"))).ok()
}

#[test]
fn rebuilding_a_stock_image_reproduces_it_byte_for_byte() {
    for f in FILES {
        let Some(raw) = read(f) else { continue };
        let fw = Firmware::parse(&raw).unwrap();
        let out = fw.build(None).unwrap();
        assert!(out == raw, "{f}: rebuild differs from the stock file");
    }
}
