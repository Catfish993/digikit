# Handover 2026-10-27: firmware declarations and the module SDK

Branch `sdk/firmware-decls` (39 commits on top of main 2d3f53a5, not pushed). Plan and full status:
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

1. Done: plan steps 5 and 6 (declarations, hooks, the reference page `mise run fw:reference`, the content-module SDK:
   `dk_api.h`, `module.h`, device-profile placement, `module:new machine|fx|service`, one guide).
2. Done in step 7: the kernel library `sdk/dsp/` (step macros, biquad, delay, half-band; tests in
   `tests/patchkit/test_dsp_kernels.py`) and the RFRAME / enhanced MODIFY core fix (cores regenerated r5).
3. Open decisions for Em: drop the pluck rebuild and the C glue pilot (Em wants to replace Selache, `selcc`
   included); use the kernels in the machine and fx templates; then our own SHARC assembler (plan step 7).
4. Then plan steps 8 (distribution and Transfer tabs) and 9 (UI services); Syntakt S3 after step 9.

Open items: the full gate (`mise run verify -- --base main --explain` first) has not run on this branch; the emulator's
generated cores still come from DT2 1.16 / DN2 1.11 profiles (recipe r4/r5); `sharc-native` `flat_access_*` tests
abort with a Cranelift panic (also before step 7); the fast-vs-core vectors never set `assume_nw32`; `selas`'s
register-modifier `MODIFY` decodes as a 48-bit form in our decoder (table gap or mis-encoding); where module objects
live once CI builds them (git or Pages assets). Cleanup Em may run: the `gen-pre-rframe-20261027` and
`gen-r4-replaced-20261027` dirs under `out/native/*/`, `out/native/dt2-audio/gen-check`, `out/r5rframe/`.

## Rules Em set in this session

- Stop before every commit: show the summary, diff stat and proposed message, wait for OK. Ask before push/PR/merge.
- Content modules: C ABI, C first-class on ColdFire; core/* stays Rust; SHARC stays assembly. Long term: replace
  Selache (assembler and compiler) as far as possible.
- Syntakt: ColdFire images only; never touch the FPGA. Lower priority (after step 9).
- Do not cite the external loader project Em mentioned anywhere in the repo.
- A rename tool (`fwrename`) is deferred.
