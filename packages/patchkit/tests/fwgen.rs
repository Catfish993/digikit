//! The firmware bindings: signatures, struct layout and the rules that refuse bad declarations.

use patchkit::fwgen::{POINTER_RETURNS_ALLOWED, RUST_OUT, render};

const RULE: &str = "cpu = \"cf\"\nrule = { kind = \"cf_func\", hash = \"00\", offset = 0, len = 4 }\n";

fn fn_entry(name: &str, sig: &str) -> String {
    format!("[[fn]]\nname = \"{name}\"\n{RULE}sig = \"{sig}\"\n")
}

fn out(text: &str, suffix: &str) -> String {
    let files = render(&[("t.toml", text)]).expect("renders");
    files
        .into_iter()
        .find(|(p, _)| p.ends_with(suffix))
        .unwrap_or_else(|| panic!("no output {suffix}"))
        .1
}

#[test]
fn sig_with_pointers_and_const() {
    let t = fn_entry("cf.view.fn.push", "u32 (void *v, const char *name, u8 flag)");
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("#[link_name = \"cf.view.fn.push\"]"));
    assert!(rs.contains(
        "pub fn push(v: *mut core::ffi::c_void, name: *const u8, flag: u8) -> u32;"
    ));
    let c = out(&t, "include/dk_fw_view.h");
    assert!(c.contains(
        "uint32_t dk_fw_view_push(void *v, const char *name, uint8_t flag) __asm__(\"cf.view.fn.push\");"
    ));
}

#[test]
fn varargs_and_empty_list() {
    let t = format!(
        "{}{}",
        fn_entry("cf.text.fn.fmt", "void (u32 x, ...)"),
        fn_entry("cf.text.fn.none", "void ()")
    );
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("pub fn fmt(x: u32, ...);"));
    assert!(rs.contains("pub fn none();"));
    let c = out(&t, "include/dk_fw_text.h");
    assert!(c.contains("void dk_fw_text_fmt(uint32_t x, ...)"));
    assert!(c.contains("void dk_fw_text_none(void)"));
}

const GAP: &str = "[[struct]]\nname = \"cf.menu.struct.Item\"\nsize = 0x10\nfields = [\n  { name = \"id\", off = 0x4, type = \"u32\" },\n  { name = \"cb\", off = 0xc, type = \"ptr\" },\n]\nmark = \"D\"\n";

#[test]
fn struct_gap_gets_padding_and_asserts() {
    let rs = out(GAP, "rs/src/lib.rs");
    assert!(rs.contains("pub _pad0: [u8; 4],"));
    assert!(rs.contains("pub _pad1: [u8; 4],"));
    assert!(rs.contains("pub cb: u32,"));
    assert!(rs.contains("size_of::<Item>() == 0x10"));
    assert!(rs.contains("offset_of!(Item, cb) == 0xc"));
    let c = out(GAP, "include/dk_fw_menu.h");
    assert!(c.contains("_Static_assert(offsetof(Item, id) == 0x4"));
    let inc = out(GAP, "inc/dk_fw_menu.inc");
    assert!(inc.contains(".equ Item_size, 0x10"));
    assert!(inc.contains(".equ Item_cb, 0xc"));
}

#[test]
fn overlapping_field_is_an_error() {
    let t = "[[struct]]\nname = \"cf.menu.struct.Bad\"\nsize = 0x10\nfields = [\n  { name = \"a\", off = 0x0, type = \"u32\" },\n  { name = \"b\", off = 0x2, type = \"u32\" },\n]\n";
    let e = render(&[("t.toml", t)]).err().expect("error");
    assert!(e.0.contains("cf.menu.struct.Bad"), "{}", e.0);
}

#[test]
fn pointer_return_waits_for_the_flag() {
    let t = fn_entry("cf.mem.fn.new", "void *(u32 n)");
    let r = render(&[("t.toml", t.as_str())]);
    if POINTER_RETURNS_ALLOWED {
        assert!(r.is_ok());
    } else {
        let e = r.err().expect("error");
        assert!(e.0.contains("pointer return types wait"), "{}", e.0);
        assert!(e.0.contains("cf.mem.fn.new"), "{}", e.0);
    }
}

#[test]
fn unknown_key_is_an_error() {
    let t = format!("{}colour = \"red\"\n", fn_entry("cf.view.fn.push", "void ()"));
    assert!(render(&[("t.toml", t.as_str())]).is_err());
}

#[test]
fn sharc_entries_make_no_output() {
    let t = "[[fn]]\nname = \"dsp.fx.fn.send\"\ncpu = \"sharc\"\nrule = { kind = \"sharc_func\" }\nsig = \"void ()\"\n";
    let files = render(&[("devices/fw/dsp/fx.toml", t)]).expect("renders");
    assert_eq!(files.len(), 1);
    assert_eq!(files[0].0, RUST_OUT);
    assert!(!files[0].1.contains("pub mod"));
}
