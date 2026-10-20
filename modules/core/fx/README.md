# FX chain

**Category:** core  **Devices:** Digitakt II 1.16, 1.17; Digitone II 1.11, 1.12  **Status:** experimental (emulator only)

A framework module: it lets other modules process the final stereo block of the device with their own DSP routine (the FX contract,
`sdk/dk_fx.h`). On its own it changes nothing: a patched image with an empty chain plays exactly the stock audio.

- What changes on the device: the first instruction of the function the DSP calls once per block after the master mix (finding 53, H1) is
  replaced by a jump into this module. The effect runs before the USB pack and the DAC converter, so it is in the USB main output and in the
  DAC. The track buses (per-track USB outputs) are not touched.
- The chain: up to four effects, rows of the registry `fx` (module order). Effect modules add a row with `[[entry]] registry = "fx"`
  (`pc`, `desc`, `state`; see `module.toml`).
- Per block and effect: the output is checked (finite, magnitude at most 8.0); a bad block plays dry and clears the effect's state; the 8th
  fault within 1500 blocks switches the effect off until the next start. A switch on or off is a linear crossfade over one block.
- Empty chain: about 16 instructions per block.
- Tests: `tests/patchkit/test_fx.py` (the adapter on the concrete core), `tests/support/fx_rig.py`.
