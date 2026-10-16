# Run-time registry (core/dynreg)

Reserves machine and parameter rows that modules loaded at run time by the digikit runtime fill: 8 machine types (16 to 23) and 16
parameter codes on the Digitakt II. It changes nothing by itself: the reserved rows read as gap rows, and the machine list leaves them out until a
module has filled them. Needs `core/runtime`, `core/machines` and `core/params` (added automatically). Digitakt II OS 1.16 and 1.17, emulator only.

How it works and its limits: `docs/design/digikit-registry.md`. Test modules: `test/dyn-xslice`, `test/dyn-params`.
