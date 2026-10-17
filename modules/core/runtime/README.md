# core/runtime

The digikit runtime: at boot, from the main-OS task entry, loads the `.dkm` modules of the saved selection from the card module store into
a private arena, and runs their slot-0 function once per UI-loop pass. The module set is fixed per session (no run-time load or unload:
hot reload is archived, `docs/plans/digikit-hot-reload.md`). The DIGIKIT menu changes the selection and restarts. Emulator only.

- Design, C ABI, sizes, hook: `docs/design/digikit-runtime.md`. File format: `docs/design/digikit-dkm.md`. Results:
  `docs/findings/29-runtime-v0.md`.
- Build: `mise run patch:build -- modules/core/runtime` (needs `uv run python -m tools.patchkit.pk_toolchains codegen-gcc` once).
- Proof in the emulator: `uv run python -m tools.patchkit.pk_dkrt proof [--image dt2-1.16|dt2-1.17|dn2-1.11|dn2-1.12]`
  (about 25 s); tests: `tests/patchkit/test_patchkit_dkrt.py`.
- Try it by hand: `patchkit pack test/dyn-demo -o plus_drive/dt2/digikit/modules/dyn-demo.dkm`, patch with `--modules core/runtime`,
  boot with that `plus_drive/`; put `modules/dyn-demo.dkm` in `plus_drive/dt2/digikit/selection.txt` (or use DIGIKIT > MODULES), and the boot loads it.
- Layout: `sdk/` is the crate both the runtime and modules use (`Api`, result codes); `src/rs/` the runtime; `src/cf/tramp.s` the
  hook trampoline and `dkrt_flush`; `src/cf/<dev>/device.s` the device id.
