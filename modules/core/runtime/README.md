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
- Module upload (finding 43, `docs/design/digikit-upload.md`): `src/rs/src/upload.rs` handles the SysEx messages that start `F0 7D 64 6B` from a hook in the firmware's MIDI task (`dkrt.hook.midi_rx`, `dkrt_midi_hook` in `tramp.s`); the host is `tools/device/dkupload.py`. `DKRT_UPLOAD_CUT` is the power-cut test knob.
- Safe mode (finding 37): the boot counter is cleared after a stable period (45,000 DSP blocks = 30 s of UI loop, clock: the firmware's block counter,
  anchor `dkrt.fw.block_counter_lit`), not at the first UI pass; a hang or a fault before then counts as a bad boot (a CPU fault halts the device).
  `DKRT_STABLE_BLOCKS` and the record's `stable_blocks` shorten it for tests. Upload safety (finding 43): replies are dropped when the firmware's TX
  ring has no room (`dkrt.fw.tx_inflight_lit`), BEGIN makes the two store headers equal before staging, `DKRT_UPLOAD_TEAR` is a test knob.
- Health (finding 41): `dkrt_health(DkrtHealth *)` (CPU load, the DSP's block time, heap, arena, DSP area; the first three need `core/health`), `dkrt_blocks()` (a 1500 Hz
  clock), `dkrt_poll_set(fn)` (a function the UI-loop tick calls once per pass; the DIGIKIT HEALTH page refreshes itself with it), mailbox command `CMD_HEALTH`.
