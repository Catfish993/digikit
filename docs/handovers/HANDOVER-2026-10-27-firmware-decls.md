# Handover 2026-10-27: firmware declarations and the module SDK

Branch `sdk/firmware-decls` (23 commits on top of main 2d3f53a5, not pushed). Plan and full status:
[docs/plans/firmware-decls-and-sdk.md](../plans/firmware-decls-and-sdk.md) — start at "Status at end of session" and
the step 5 list. Design: [docs/design/firmware-decls.md](../design/firmware-decls.md).

## Where things are

- Firmware facts: `devices/fw/<subsystem>.toml` (143 declarations, names `<cpu>.<subsystem>.<kind>.<name>`, word list
  in `devices/fw/subsystems.toml`). `port_resolve.py` reads them and writes `devices/ports/{dt2-1.17,dn2-1.12}.toml`.
- Bindings: `patchkit gen` writes `modules/core/fw/` (Rust crate `dkfw`, C headers, GAS includes).
  `core/digikit-menu` and `core/runtime` use them.
- Catalogue: `uv run python -m tools.coldfire.atlas catalog dt2-1.17 dn2-1.12`, DSP:
  `uv run python -m tools.sharc_dev.sharc_catalog dt2-1.17 dn2-1.12`, query `fw.py catalog`, rules
  `uv run python -m tools.firmware.catalog emit-rule|declare`. Review page generator `tools/firmware/catalog_page.py`.
- Findings from this work: 70 (Syntakt 1.42), 71 (UI components), 72 (SHARC kernels); style guide
  `docs/sharc/style.md`.
- Local, not in git: `out/firmware-decls/` (rename codemod `rename2.py`, `proposals-v2.json`, `mapping2.tsv`,
  `apply_all.sh` for the byte-identity proof), `out/atlas/labels/dt2-1.17/` (agent labels for the review page).

## Next

1. Done: 4c, `aliases` -> `was`, port files without alias anchors, design-doc examples fixed.
2. Done: hook declarations with register contracts and generated trampolines (see the plan).
3. Done: reference data and page (`mise run fw:reference`).
4. Plan step 6 (C SDK for content modules), then 7 (SHARC kernels and C pilot), 8 (distribution), 9 (UI services).

## Rules Em set in this session

- Stop before every commit: show the summary, diff stat and proposed message, wait for OK. Ask before push/PR/merge.
- Content modules: C ABI, C first-class; core/* stays Rust; SHARC stays assembly (C only for glue after the pilot).
- Syntakt: ColdFire images only; never touch the FPGA. Lower priority (after step 9).
- Do not cite the external loader project Em mentioned anywhere in the repo.
- A rename tool (`fwrename`) is deferred.
