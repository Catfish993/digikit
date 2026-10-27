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
    let t = fn_entry("cf.view.fn.view_controller_push", "u32 (void *v, const char *name, u8 flag)");
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("#[link_name = \"cf.view.fn.view_controller_push\"]"));
    assert!(rs.contains(
        "pub fn view_controller_push(v: *mut core::ffi::c_void, name: *const u8, flag: u8) -> u32;"
    ));
    let c = out(&t, "include/dk_fw_view.h");
    assert!(c.contains(
        "uint32_t dk_fw_view_view_controller_push(void *v, const char *name, uint8_t flag) __asm__(\"cf.view.fn.view_controller_push\");"
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
fn pointer_return_renders_in_rust_and_c() {
    assert!(POINTER_RETURNS_ALLOWED);
    let t = format!(
        "{}{}",
        fn_entry("cf.mem.fn.new", "void *(u32 n)"),
        fn_entry("cf.mem.fn.bytes", "const u8 *(u32 n)")
    );
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("pub fn new(n: u32) -> *mut core::ffi::c_void;"));
    assert!(rs.contains("pub fn bytes(n: u32) -> *const u8;"));
    let c = out(&t, "include/dk_fw_mem.h");
    assert!(c.contains("void * dk_fw_mem_new(uint32_t n) __asm__(\"cf.mem.fn.new\");"));
    assert!(c.contains("const uint8_t * dk_fw_mem_bytes(uint32_t n) __asm__(\"cf.mem.fn.bytes\");"));
}

#[test]
fn site_bind_static_makes_a_static() {
    let site = |extra: &str| {
        format!("[[site]]\nname = \"cf.menu.site.settings_list.system_icon_ref\"\n{RULE}{extra}")
    };
    let rs = out(&site("bind = \"static\"\n"), "rs/src/lib.rs");
    assert!(rs.contains("#[link_name = \"cf.menu.site.settings_list.system_icon_ref\"]"));
    assert!(rs.contains("pub static SETTINGS_LIST_SYSTEM_ICON_REF: u8;"));
    let plain = render(&[("t.toml", site("").as_str())]).expect("renders");
    assert!(!plain[0].1.contains("SETTINGS_LIST_SYSTEM_ICON_REF"));
    assert!(render(&[("t.toml", site("bind = \"extern\"\n").as_str())]).is_err());
}

#[test]
fn unknown_key_is_an_error() {
    let t = format!("{}colour = \"red\"\n", fn_entry("cf.view.fn.view_controller_push", "void ()"));
    assert!(render(&[("t.toml", t.as_str())]).is_err());
}

/// A site `cf.view.site.loop` and a hook `cf.view.hook.loop` on `site`; `regs` is a TOML inline table.
fn hook_text(site: &str, mode: &str, regs: &str, extra: &str) -> String {
    format!(
        "[[site]]\nname = \"cf.view.site.loop\"\n{RULE}[[hook]]\nname = \"cf.view.hook.loop\"\nsite = \"{site}\"\ncpu = \"cf\"\nmode = \"{mode}\"\nregs = {regs}\n{extra}"
    )
}

fn hook_error(site: &str, mode: &str, regs: &str, extra: &str) -> String {
    let t = hook_text(site, mode, regs, extra);
    let e = render(&[("t.toml", t.as_str())]).err().expect("error");
    assert!(e.0.contains("cf.view.hook.loop"), "{}", e.0);
    e.0
}

#[test]
fn before_hook_with_a_register_arg() {
    let t = hook_text(
        "cf.view.site.loop",
        "before",
        "{ in = [\"a2\"], clobbers = [\"d0-d1\", \"a0-a1\", \"ccr\"] }",
        "handler = { sig = \"void (u8 *menu)\", args = [\"a2\"] }\ndoc = \"Top of the loop.\"\n",
    );
    let inc = out(&t, "inc/dk_fw_view.inc");
    assert!(inc.contains(
        "/* cf.view.hook.loop: Top of the loop. */\n.macro dk_fw_view_loop_hook handler, next\n    move.l  %a2,-(%sp)\n    jsr     \\handler\n    addq.l  #4,%sp\n    jmp     \\next\n.endm\n"
    ), "{inc}");
    let c = out(&t, "include/dk_fw_view.h");
    assert!(c.contains("/* Top of the loop. */\ntypedef void dk_fw_view_loop_hook(uint8_t *menu);"), "{c}");
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("/// Top of the loop.\n    pub type LoopHook = unsafe extern \"C\" fn(menu: *mut u8);"), "{rs}");
}

#[test]
fn retarget_hook_with_stack_args_saves_registers() {
    let t = hook_text(
        "cf.view.site.loop",
        "retarget",
        "{ in = [], clobbers = [\"ccr\"] }",
        "handler = { sig = \"i32 (i32 a, i32 b, i32 c, i32 d)\", args = [\"sp+4\", \"sp+8\", \"sp+12\", \"sp+16\"] }\n",
    );
    let inc = out(&t, "inc/dk_fw_view.inc");
    let push = "    move.l  32(%sp),-(%sp)\n".repeat(4);
    let want = format!(
        ".macro dk_fw_view_loop_hook handler, next\n    lea     -16(%sp),%sp\n    movem.l %d0-%d1/%a0-%a1,(%sp)\n{push}    jsr     \\handler\n    lea     16(%sp),%sp\n    movem.l (%sp),%d0-%d1/%a0-%a1\n    lea     16(%sp),%sp\n    jmp     \\next\n.endm\n"
    );
    assert!(inc.contains(&want), "{inc}");
    let rs = out(&t, "rs/src/lib.rs");
    assert!(rs.contains("pub type LoopHook = unsafe extern \"C\" fn(a: i32, b: i32, c: i32, d: i32) -> i32;"), "{rs}");
}

#[test]
fn bad_hooks_are_errors() {
    let regs = "{ in = [\"a2\"], clobbers = [\"ccr\"] }";
    let handler = |args: &str| format!("handler = {{ sig = \"void (u8 *menu)\", args = {args} }}\n");
    assert!(hook_error("cf.view.site.loop", "after", regs, "").contains("mode"));
    assert!(
        hook_error("cf.view.site.loop", "before", "{ in = [], clobbers = [\"d9\"] }", "")
            .contains("unknown register")
    );
    assert!(
        hook_error("cf.view.site.loop", "replace", regs, &handler("[\"a2\"]")).contains("handler")
    );
    assert!(hook_error("cf.view.site.loop", "before", regs, &handler("[]")).contains("args"));
    assert!(hook_error("cf.view.site.loop", "before", regs, &handler("[\"sp+2\"]")).contains("sp+N"));
    assert!(
        hook_error("cf.view.site.loop", "before", regs, &handler("[\"a3\"]")).contains("not in regs.in")
    );
    assert!(
        hook_error(
            "cf.view.site.loop",
            "before",
            "{ in = [], clobbers = [], modes = { simd = false } }",
            ""
        )
        .contains("modes")
    );
    assert!(hook_error("cf.view.site.missing", "before", regs, "").contains("site"));
}

#[test]
fn sharc_entries_make_no_output() {
    let t = "[[fn]]\nname = \"dsp.fx.fn.send\"\ncpu = \"sharc\"\nrule = { kind = \"sharc_func\" }\nsig = \"void ()\"\n";
    let files = render(&[("devices/fw/dsp/fx.toml", t)]).expect("renders");
    assert_eq!(files.len(), 1);
    assert_eq!(files[0].0, RUST_OUT);
    assert!(!files[0].1.contains("pub mod"));
}
