# The FX contract (`sdk/dk_fx.h`)

An effect processes the final stereo block of the device: the master, after the master mix and before the USB pack and the DAC converter
(`docs/findings/53-stock-dsp-shape.md` H1, `docs/findings/55-fx-adapter.md` for the sites). Effects are patched in (`test/fx-gain` is the example) or, on the
Digitakt II, loaded at run time from a `.dkm` (`test/dyn-fx-gain`, see "Run-time effects" below; `[contract] fx` is [1, 0]). Knobs for the parameter words and `tail_blocks`
come later.

Entry: the adapter `core/fx` enters your routine with `I13 = return address; JUMP (M13, I12)`; you return with `JUMP (M13, I13)`.
Arguments: `R4` = state, `R8` = `dk_fx_in`, `R12` = parameter words. Result in `R0`: 0 (`DK_FX_OK`) or a negative number (a fault).

```
dk_fx_in (words):  0 in (pointer to 64 floats: 32 left, then 32 right, read only)   1 out (pointer to 64 floats, yours, scratch)   2 block (blocks since the start)
params:            DK_FX_MAX_PARAMS (8) words; word i is the row's parameter i (0, or the `[[fx]] defaults` of a run-time effect)
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

## Run-time effects (Digitakt II, `.dkm`)

A module with `dynamic = true`, `type = "fx"` and a DSP object for `dt2` declares its effects in `module.toml`:

```toml
[[fx]]
id = "gain"
process = "_dg_process"   # global text symbol of the process routine
desc = "_dg_desc"         # data symbol of its dk_fx_desc (n_params <= 8, state_bytes a multiple of 4)
defaults = [0x3f000000]   # the words p0 ... of the row (at most n_params); 0 where none
devices = ["dt2"]
```

`patchkit pack MODULE --device dt2` makes a `.dkm` whose DSP section (version 4) has one record per effect and whose contract bytes say fx 1.0 (and no machine). There is no state symbol: the runtime
reserves `state_bytes` behind your image and passes the address in R4. The runtime takes a free row of the chain (the device has `fx_slots` of them, 3 on the Digitakt II: `CAPACITY` `FX_SLOTS`, the DIGIKIT menu's `FX`), sets p0 to p7 to
your defaults and `enabled` to 1, and publishes the pc last. A fourth effect is refused by the fit rule before the load and by the loader (`E_DSP_ROWS`) at boot. The effect is on from the start; there are no knobs yet
(the words stay at their defaults). Template: `tests/fixtures/modules/dyn-fx-gain`; proof: `tests/digikit/scenarios/fx-dyn-dt2.toml` (`mise run scenario`-style, `pk_digikit_scen run fx-dyn-dt2`).

Judged by `tests/modules/test_fx_conformance.py` (`mise run module:check NAME`): signals, parameter limits, modulation, switching, a long run;
checks: returns, registers, the adapter did not refuse a block, write set, instruction budget (about 23,000 for the effect, 900 for the adapter).
Template (patched in): `tests/fixtures/modules/fx-gain/src/dsp/fx_gain.s`. Emulator proof of the adapter: `tools/patchkit/pk_proof_fx.py`. The conformance suite judges patched-in effects (an `[[entry]] registry = "fx"` row); a run-time effect is judged by the scenario above only.
