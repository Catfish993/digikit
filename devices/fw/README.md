# Firmware declarations

This directory holds the firmware facts of the Digitakt II and Digitone II: one declaration per function, data object,
site or parameter list, with the rule that finds it in a stock image. One file per firmware subsystem.
Design: [docs/design/firmware-decls.md](../../docs/design/firmware-decls.md).

```toml
[[site]]
name = "cf.menu.site.settings_tail"
aliases = ["cf.menu.settings_tail"]
doc = "End of the SETTINGS item list rebuild."
devices = ["dt2", "dn2"]
rule = { kind = "cf_func", hash = "8db782f6fd735f89d3b7c856316c0b90", offset = 0x3a8, len = 8 }
mark = "D"
```

`tools/patchkit/port_resolve.py` reads these files (`mise run patch:port`) and writes `devices/ports/`. Names follow
`<cpu>.<subsystem>.<kind>.<name>`: `cpu` is `cf` or `dsp`, `subsystem` is a word from [subsystems.toml](subsystems.toml)
(each has one file: `<subsystem>.toml`, or `dsp/<subsystem>.toml` for the DSP), and `kind` is the table of the entry
(`fn`, `data`, `value`, `sites`, `site`). `port_resolve.py` refuses a name that breaks the rule.

To declare a function, use `tools/firmware/catalog.py` on a stock image (SHARC if the address is below 0x1000000). Both
commands compute the rule from the function that contains ADDR, check that it resolves back to ADDR, and with `--other`
check the other device against the atlas catalogue pair:

```sh
uv run python -m tools.firmware.catalog emit-rule dt2-1.17 0x40113dc8 --other dn2-1.12
uv run python -m tools.firmware.catalog declare dt2-1.17 0x40113dc8 cf.text.fn.draw_fmt --other dn2-1.12 --doc "..." [--write]
```

A virtual function whose hash is shared by more than four functions gets a `cf_vtable_slot` rule instead
(`{ kind = "cf_vtable_slot", class = "ConfirmWindow", slot = 4, offset = 0, len = 8 }`; `offset_to_top` defaults to 0):
it names the RTTI class and the vtable slot, and a class with another name on the other device gets its own rule.

`declare` prints a `mark = "O"` entry (a shared rule, or one rule per device) and with `--write` adds it to the
subsystem file in name order. Review the entry and raise the mark after a check against image bytes.

An `fn` may have `sig = "u32 (void *p, const char *fmt, ...)"`: a return type and typed, named arguments, `...` last.
Types are `u8 u16 u32 i8 i16 i32 bool void`, `char` behind a pointer, a struct short name, `const` and `*`. A pointer
return is allowed (the firmware sets d0). A `site` with `bind = "static"` is also written as a static (for a module that
takes only its address); no other `bind` value exists. A `[[struct]]` has `name`, `size`, `mark`, `users` and `fields = [{ name, off, type }]`;
`type` is a scalar, `ptr` or a struct short name. `patchkit gen` writes the `cf` bindings to `modules/core/fw/`:
`rs/src/lib.rs` (crate `dkfw`), `include/dk_fw_<subsystem>.h` and `inc/dk_fw_<subsystem>.inc` (GAS).

The old names stay as `aliases` until step 5 (every user has moved). The port file lists each anchor under its name and
under every alias with the same values, and a multi-site entry lists `<alias>.<i>` for each `<name>.<i>`. An alias must
not be the name or alias of another declaration. Content modules never use these declarations; only `core/*` adapters
do.
