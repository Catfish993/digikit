# FX chain

**Category:** core  **Devices:** Digitakt II 1.16, 1.17; Digitone II 1.11, 1.12  **Status:** experimental (emulator only)

A framework module: it lets other modules process the final stereo block of the device with their own DSP routine (the FX contract,
`sdk/dk_fx.h`). On its own it changes nothing: a patched image with an empty chain plays exactly the stock audio.

- What changes on the device: the first instruction of a function the DSP calls once per block after the master mix and before the USB pack (finding 55:
  Digitakt II `FUN_1c80f2`, Digitone II `FUN_1c244d`) is replaced by a jump into this module. The effect runs before the USB pack and the DAC converter, so it is in the USB main output and in the
  DAC. The track buses (per-track USB outputs) are not touched.
- The chain: up to four effects, rows of the registry `fx` (module order). A row is 24 words: pc, desc, state, enabled, the eight parameter words p0 to p7
  (`DK_FX_MAX_PARAMS`), then the adapter's own words (see `module.toml`). Patched-in effect modules add a row with `[[entry]] registry = "fx"`
  (`pc`, `desc`, `state`). A row with pc `0xffffffff` is free and skipped: `core/dynfx` reserves such rows for effects that the digikit runtime loads at run time
  (a `.dkm` with a DSP section of version 4; `docs/design/digikit-dkm.md`); the runtime fills one through window 2 of `core/dsp-link`, the pc word last.
- Per block and effect: the output is checked (finite, magnitude at most 8.0); a bad block plays dry and clears the effect's state; the 8th
  fault within 1500 blocks switches the effect off until the next start. A switch on or off is a linear crossfade over one block.
- Empty chain: 16 instructions per block with no row, 21 with one free row, 31 with three free rows (a quick scan of at most four rows, about 5 instructions per free row); one effect adds
  4 to the bookkeeping of its row (steady 736, off 178 instructions per block).
- Tests: `tests/patchkit/test_fx.py` (the adapter on the concrete core), `tests/support/fx_rig.py`.
