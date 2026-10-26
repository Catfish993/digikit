# Firmware declarations

This directory holds the firmware facts of the Digitakt II and Digitone II: one declaration per function, data object,
site or parameter list, with the rule that finds it in a stock image. One file per firmware subsystem.
Design: [docs/design/firmware-decls.md](../../docs/design/firmware-decls.md).

```toml
[[site]]
name = "cf.menu.site.settings_tail"
doc = "End of the SETTINGS item list rebuild."
devices = ["dt2", "dn2"]
rule = { kind = "cf_func", hash = "8db782f6fd735f89d3b7c856316c0b90", offset = 0x3a8, len = 8 }
mark = "D"
```

`tools/patchkit/port_resolve.py` reads these files (`mise run patch:port`) and writes `devices/ports/`. Names are
unchanged for now. They will follow `<cpu>.<subsystem>.<kind>.<name>`, and the old names stay as aliases. Content
modules never use these declarations; only `core/*` adapters do.
