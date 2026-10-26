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

`declare` prints a `mark = "O"` entry (a shared rule, or one rule per device) and with `--write` adds it to the
subsystem file in name order. Review the entry and raise the mark after a check against image bytes.

The old names stay as `aliases` until step 5 (every user has moved). The port file lists each anchor under its name and
under every alias with the same values, and a multi-site entry lists `<alias>.<i>` for each `<name>.<i>`. An alias must
not be the name or alias of another declaration. Content modules never use these declarations; only `core/*` adapters
do.
