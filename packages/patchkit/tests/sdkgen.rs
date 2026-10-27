//! The module contract: the files `patchkit gen` writes from `sdk/sdk.toml` are current (a stale one fails here as it fails
//! `patchkit gen --check`).

use patchkit::sdkgen;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn the_fx_chain_cap_is_the_registrys() {
    // the chain cap of the registry in core/fx is the contract's DK_FX_MAX
    let toml = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let f = sdkgen::fx(&toml).unwrap();
    let max = f
        .consts
        .iter()
        .find(|c| c.name == "DK_FX_MAX")
        .unwrap()
        .value;
    let man =
        std::fs::read_to_string(root().join("modules/core/fx/module.toml")).unwrap_or_default();
    if !man.is_empty() {
        assert!(
            man.contains(&format!("max = {max}")),
            "modules/core/fx/module.toml: the fx registry has max = {max}"
        );
    }
}

#[test]
fn the_contract_files_are_current() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let files = sdkgen::render(&text).unwrap();
    assert_eq!(files.len(), 13);
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

/// `text` with its `[api]` fields replaced by `fields` (the lines of the `fields = [...]` array).
fn with_api(text: &str, fields: &str) -> String {
    let a = text.find("[api]\n").unwrap();
    let b = a + text[a..].find("\n[api.types]\n").unwrap() + 1;
    format!("{}[api]\nfields = [\n{fields}]\n\n{}", &text[..a], &text[b..])
}

const TWO_FIELDS: &str = "    { name = \"version\", type = \"u32\", doc = \"v\" },\n    { name = \"hook\", type = \"void (u32 a, service_cmd handler)\", doc = \"h\" },\n";

#[test]
fn the_api_table_is_generated_for_c_and_rust() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let files = sdkgen::render(&with_api(&text, TWO_FIELDS)).unwrap();
    let get = |p: &str| &files.iter().find(|f| f.0 == p).unwrap().1;
    let c = get(sdkgen::C_API_OUT);
    assert!(c.contains("typedef struct dk_api {"));
    assert!(c.contains("_Static_assert(sizeof(dk_api) == 8,"));
    assert!(c.contains("_Static_assert(offsetof(dk_api, hook) == 4,"));
    // a function type name as a parameter is a nullable pointer
    assert!(c.contains("void (*hook)(uint32_t a, dk_service_cmd *handler);"));
    assert!(c.contains("typedef int32_t dk_service_cmd(uint32_t cmd, uint32_t seq,"));
    let rs = get(sdkgen::RUST_OUT);
    assert!(rs.contains("pub hook: unsafe extern \"C\" fn(a: u32, handler: Option<ServiceCmd>),"));
    assert!(rs.contains("pub type ServiceCmd = unsafe extern \"C\" fn(cmd: u32,"));
    assert!(rs.contains("pub type DkrtBlocksFn = unsafe extern \"C\" fn() -> u32;"));
}

#[test]
fn a_bad_type_or_view_is_refused() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let bad = with_api(&text, &TWO_FIELDS.replace("u32 a", "u64 a"));
    assert!(sdkgen::render(&bad).unwrap_err().0.contains("unknown type"));
    // the names of a typed C view are the contract's name list, in order
    let broken = text.replace("\"i32 pitch // ", "\"i32 pitchx // ");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).unwrap_err().0.contains("name list"));
}

#[test]
fn a_bad_descriptor_layout_is_refused() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    // text that needs padding
    let broken = text.replace("char short_name[8]", "char short_name[6]");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).unwrap_err().0.contains("multiple of 4"));
    // a type that is not a base type or an earlier record
    let broken = text.replace("float min //", "double min //");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).unwrap_err().0.contains("unknown type"));
    // a length that is not a const of [desc]
    let broken = text.replace("knob[DK_FX_KNOBS]", "knob[DK_NOPE]");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).unwrap_err().0.contains("DK_NOPE"));
}

#[test]
fn the_record_format_constants_come_from_the_contract_maxima() {
    // SEL_MAX and MAP_N are generated from `[limits.max]` (the selection record's 8 paths and 8 map entries); the generated file is current
    // (`the_contract_files_are_current`), so the constants are the maxima. The effect chain cap is the contract's DK_FX_MAX.
    let max = sdkgen::limits_max();
    let gen_sdk = std::fs::read_to_string(root().join(sdkgen::RUST_OUT)).unwrap();
    assert!(gen_sdk.contains(&format!("pub const SEL_MAX: usize = {};", max.modules)));
    assert!(gen_sdk.contains(&format!("pub const MAP_N: usize = {};", max.map_entries)));
    let fx = sdkgen::fx(&std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap()).unwrap();
    let dk_fx_max = fx
        .consts
        .iter()
        .find(|c| c.name == "DK_FX_MAX")
        .unwrap()
        .value;
    assert_eq!(dk_fx_max as u64, max.fx_slots);
}

#[test]
fn the_capacity_kinds_are_generated_for_every_host() {
    let text = std::fs::read_to_string(root().join(sdkgen::SOURCE)).unwrap();
    let files = sdkgen::render(&text).unwrap();
    let get = |p: &str| &files.iter().find(|f| f.0 == p).unwrap().1;
    assert!(get(sdkgen::PROTO_PY_OUT).contains("\"ARENA\": 3,"));
    assert!(get(sdkgen::PROTO_PY_OUT).contains("CMD_CAPACITY = 0x04"));
    assert!(get(sdkgen::PROTO_RS_OUT).contains("pub const CAPK_DSP_AREA: u8 = 5;"));
    assert!(get(sdkgen::PROTO_TS_OUT).contains("  VERIFY: 0x17,"));
    assert!(
        get(sdkgen::PROTO_TS_OUT).contains("  [-59, 'E_NOFILE', 'that path is not in the store'],")
    );
    assert!(get(sdkgen::PROTO_TS_OUT).contains("  ARENA: 3,"));
    // the command ranges a module may register: in every host's numbers, and a bad one is refused
    assert!(get(sdkgen::PROTO_TS_OUT).contains("  remote: [0x38, 0x3d],"));
    assert!(get(sdkgen::PROTO_TS_OUT).contains("GUARDED: string[] = ['remote'];"));
    assert!(get(sdkgen::PROTO_PY_OUT).contains("\"remote\": (0x38, 0x3D),"));
    assert!(get(sdkgen::PROTO_PY_OUT).contains("GUARDED: tuple[str, ...] = (\"remote\",)"));
    assert!(get(sdkgen::PROTO_RS_OUT).contains("pub const RANGE_REMOTE: (u8, u8) = (0x38, 0x3d);"));
    assert!(get(sdkgen::PROTO_RS_OUT).contains("(0x38, 0x3d, true), // remote"));
    assert!(get(sdkgen::PROTO_RS_OUT).contains("(0x3e, 0x3f, false), // remote_state"));
    for bad in [
        "remote = [0x38, 0x40]",
        "remote = [0x10, 0x18]",
        "remote = [0x3f, 0x38]",
        "remote = [0, 0x08]",
        "remote = [0x38, 0x3e]",
    ] {
        let broken = text.replace("remote = [0x38, 0x3d]", bad);
        assert_ne!(broken, text);
        assert!(sdkgen::render(&broken).is_err(), "{bad}");
    }
    // the commands of the service modules, in every host's numbers; one outside every range is refused
    assert!(get(sdkgen::PROTO_TS_OUT).contains("  REMOTE_KEY: 0x38,"));
    assert!(get(sdkgen::PROTO_PY_OUT).contains("\"REMOTE_STATE\": 0x3E,"));
    assert!(get(sdkgen::PROTO_RS_OUT).contains("pub const SVC_REMOTE_TURN: u8 = 0x39;"));
    let broken = text.replace("REMOTE_KEY = 0x38 ", "REMOTE_KEY = 0x10 ");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).is_err());
    // a guarded range must exist
    let broken = text.replace("guarded = [\"remote\"]", "guarded = [\"nope\"]");
    assert_ne!(broken, text);
    assert!(sdkgen::render(&broken).is_err());
    assert!(get(sdkgen::RUST_OUT).contains("pub const CAP_CAPACITY: u32 = 1 << 6;"));
    // two kinds with one number, or kind 0, are refused
    let dup = text.replace("ARENA = 3 ", "ARENA = 2 ");
    assert_ne!(dup, text);
    assert!(
        sdkgen::render(&dup)
            .unwrap_err()
            .0
            .contains("capacity kind")
    );
}
