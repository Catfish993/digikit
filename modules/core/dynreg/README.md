# Run-time registry (core/dynreg)

Reserves machine, parameter and (Digitone II) page rows that modules loaded at run time by the digikit runtime fill: 8 machine types (16 to 23),
16 parameter codes and, on the Digitone II, 8 page rows. It changes nothing by itself: the reserved rows read as gap rows, and the machine list
leaves them out until a module has filled them. Needs `core/runtime`, `core/machines` and `core/params` (added automatically). Digitakt II OS 1.16
and 1.17, Digitone II OS 1.11 and 1.12, emulator only.

It also anchors three firmware functions (`cf.dynreg.project`, `kit_of`, `track_type`) with which the runtime refuses to unload a machine a track of
the active kit uses.

How it works and its limits: `docs/design/digikit-registry.md`. Test modules: `test/dyn-xslice`, `test/dyn-params` (Digitakt II), `test/dyn-dn2`
(Digitone II). Anchor rules: `uv run python -m tools.patchkit.pk_dynreg_rules --device dt2|dn2`.
