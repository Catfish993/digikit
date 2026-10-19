//! The module contract: the files `patchkit gen` writes from `sdk/sdk.toml` are current (a stale one fails here as it fails
//! `patchkit gen --check`).

use patchkit::sdkgen;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn the_contract_files_are_current() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let files = sdkgen::render(&text).unwrap();
    assert_eq!(files.len(), 2);
    for (rel, text) in files {
        let path = root().join(&rel);
        let have = std::fs::read_to_string(&path).unwrap_or_default();
        assert_eq!(have, text, "{} is stale: run `patchkit gen`", path.display());
    }
}
