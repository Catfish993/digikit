# The FX contract (`sdk/dk_fx.h`)

An effect processes the final stereo block of the device: the master, after the master mix and before the USB pack and the DAC converter
(site H1, `docs/findings/53-stock-dsp-shape.md`, `docs/findings/55-fx-adapter.md`). Today effects are patched in (v1, `test/fx-gain` is the
example); run-time (`.dkm`) effects, parameters and `tail_blocks` come with P3d-1b. `[contract] fx` stays [0, 0] until then.

Entry: the adapter `core/fx` enters your routine with `I13 = return address; JUMP (M13, I12)`; you return with `JUMP (M13, I13)`.
Arguments: `R4` = state, `R8` = `dk_fx_in`, `R12` = parameter words. Result in `R0`: 0 (`DK_FX_OK`) or a negative number (a fault).

```
dk_fx_in (words):  0 in (pointer to 64 floats: 32 left, then 32 right, read only)   1 out (pointer to 64 floats, yours, scratch)   2 block (blocks since the start)
params:            DK_FX_MAX_PARAMS (4) words; word i is the row's parameter i (0 in v1)
dk_fx_desc (words): magic DK_FX_MAGIC, contract DK_FX_V1, n_params, state_bytes (multiple of 4), max_cycles_block, max_call_depth, stack_bytes
```

- `process` runs once per block (32 samples at 48 kHz) while the effect is on. Write all 64 floats to `out`. The adapter copies `out` to the master.
- The state is yours and the only memory that survives between blocks. The adapter zeroes `state_bytes` when the effect switches on. No other statics
  except constants. Declare the state with `.PKBSS name, bytes;` and name it in the row.
- A chain has up to 4 effects in module order; each sees the previous output. A row is added in `module.toml`:
  `[[entry]] registry = "fx"`, `id`, `pc` (your `process` symbol), `desc` (your `dk_fx_desc`), `state` (your state symbol). Require `core/fx`.
- Switching on or off is a linear crossfade over one block that the adapter makes; your routine never sees a click. While off it is not called, so a
  tail (reverb, delay) freezes.
- The adapter checks `out` after every call: a negative result, a sample that is not finite or above 8.0 in magnitude is a fault. The block then plays
  dry, your state is zeroed and the effect counts as off; the 8th fault within 1500 blocks switches it off until the next start.
- Include `dk_fx.inc` (not together with `dk_machine.inc`). Registers: free R0-R15, I0-I5, I13, M1-M4, B3. Keep I6-I12, I14, I15, M0, M5-M15,
  the L registers and MODE1. The adapter pins M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L5 = 0, SIMD off, RND32 set.
- Data stack: `stack_bytes` below `I7`; nothing else outside your own data may be written. The input block is read only.

Judged by `tests/modules/test_fx_conformance.py` (`mise run module:check NAME`): signals, parameter limits, modulation, switching, a long run;
checks: returns, registers, the adapter did not refuse a block, write set, instruction budget (about 23,000 for the effect, 900 for the adapter).
Template: `tests/fixtures/modules/fx-gain/src/dsp/fx_gain.s`. Emulator proof of the adapter: `tools/patchkit/pk_proof_fx.py`.
