//! The runtime's C ABI: the files `patchkit gen` writes from `modules/core/runtime/abi.toml` are current (a stale one fails here as it fails
//! `patchkit gen --check`), and a spec with padding or an unknown type is refused.

use patchkit::{abigen, sdkgen};
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn texts() -> (String, String) {
    (
        std::fs::read_to_string(root().join(abigen::SOURCE)).unwrap(),
        std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap(),
    )
}

#[test]
fn the_abi_files_are_current() {
    let (abi, sdk) = texts();
    let files = abigen::render_with(&abi, &sdk).unwrap();
    assert_eq!(files.len(), 3);
    for (rel, text) in files {
        let path = root().join(&rel);
        let have = std::fs::read_to_string(&path).unwrap_or_default();
        assert_eq!(
            have,
            text,
            "{} is stale: run `patchkit gen`",
            path.display()
        );
    }
}

#[test]
fn every_export_is_in_the_header_and_the_check() {
    let (abi, sdk) = texts();
    let files = abigen::render_with(&abi, &sdk).unwrap();
    let get = |p: &str| &files.iter().find(|(r, _)| r == p).unwrap().1;
    let h = get(abigen::C_OUT);
    assert!(h.contains("int32_t dkrt_fit(const DkrtScan *entries, uint32_t n, const uint8_t *ticked, DkrtFitOut *out);"));
    assert!(h.contains("void dkrt_poll_set(uintptr_t f);"));
    assert!(h.contains("#define DKRT_E_NOT_FOUND (-1)"));
    assert!(h.contains("_Static_assert(sizeof(DkrtScan) == 116, \"DkrtScan size\");"));
    let c = get(abigen::CHECK_OUT);
    assert!(c.contains("const _: sig::dkrt_scan = crate::scan::dkrt_scan;"));
    assert!(c.contains("const _: sig::dkrt_status = crate::dkrt_status;"));
    let r = get(abigen::RUST_OUT);
    assert!(r.contains("pub results: [i32; SEL_MAX],"));
    assert!(r.contains("pub paths: [[u8; SEL_PATH]; SEL_MAX],"));
}

/// `abi` with its first `[[struct]]` replaced by `body`.
fn with_struct(abi: &str, body: &str) -> String {
    let a = abi.find("[[struct]]").unwrap();
    let b = a + 1 + abi[a + 1..].find("[[struct]]").unwrap();
    format!("{}{body}\n{}", &abi[..a], &abi[b..])
}

#[test]
fn a_struct_with_padding_or_an_unknown_type_is_refused() {
    let (abi, sdk) = texts();
    let head = "[[struct]]\nname = \"DkrtStatus\"\ndoc = \"d\"\nfields = [\n";
    // a u32 at offset 1
    let bad = with_struct(&abi, &format!("{head}  \"u8 a\",\n  \"u32 b\",\n]\n"));
    assert!(
        abigen::render_with(&bad, &sdk)
            .unwrap_err()
            .0
            .contains("padding")
    );
    // a size that is not a multiple of 4
    let bad = with_struct(&abi, &format!("{head}  \"u32 a\",\n  \"u8 b\",\n]\n"));
    assert!(
        abigen::render_with(&bad, &sdk)
            .unwrap_err()
            .0
            .contains("multiple of 4")
    );
    let bad = with_struct(&abi, &format!("{head}  \"u64 a\",\n]\n"));
    assert!(
        abigen::render_with(&bad, &sdk)
            .unwrap_err()
            .0
            .contains("unknown type")
    );
    let bad = with_struct(&abi, &format!("{head}  \"u8 a[NO_SUCH]\",\n]\n"));
    assert!(
        abigen::render_with(&bad, &sdk)
            .unwrap_err()
            .0
            .contains("NO_SUCH")
    );
}

#[test]
fn a_function_with_an_unknown_type_is_refused() {
    let (abi, sdk) = texts();
    let bad = abi.replace(
        "void dkrt_status(DkrtStatus *out)",
        "void dkrt_status(Nope *out)",
    );
    assert!(
        abigen::render_with(&bad, &sdk)
            .unwrap_err()
            .0
            .contains("unknown type")
    );
}
