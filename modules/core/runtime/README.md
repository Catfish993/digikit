# core/runtime

The digikit runtime v0: loads `.dkm` modules from the card module store into a private arena at run time, runs their slot-0
function once per UI-loop pass, unloads them. Emulator only; no menu yet (the DIGIKIT menu module will call `dkrt_load`).

- Design, C ABI, sizes, hook: `docs/design/digikit-runtime.md`. File format: `docs/design/digikit-dkm.md`. Results:
  `docs/findings/29-runtime-v0.md`.
- Build: `mise run patch:build -- modules/core/runtime` (needs `uv run python -m tools.patchkit.pk_toolchains codegen-gcc` once).
- Proof in the emulator: `uv run python -m tools.patchkit.pk_dkrt proof [--image dt2-1.16|dt2-1.17|dn2-1.11|dn2-1.12]`
  (about 25 s); tests: `tests/patchkit/test_patchkit_dkrt.py`.
- Try it by hand: `patchkit pack test/dyn-demo -o plus_drive/dt2/digikit/modules/dyn-demo.dkm`, patch with `--modules core/runtime`,
  boot with that `plus_drive/`, then poke the mailbox (`DKRT_MAILBOX` in the plan's symbols): path at +12, `1` at +0.
- Layout: `sdk/` is the crate both the runtime and modules use (`Api`, result codes); `src/rs/` the runtime; `src/cf/tramp.s` the
  hook trampoline and `dkrt_flush`; `src/cf/<dev>/device.s` the device id.
