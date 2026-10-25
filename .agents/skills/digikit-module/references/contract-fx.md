# The FX contract (`sdk/dk_fx.h`)

An effect processes the final stereo block of the device: the master, after the master mix and before the USB pack and the DAC converter
(`docs/findings/53-stock-dsp-shape.md` H1, `docs/findings/55-fx-adapter.md` for the sites). Effects are patched in (`test/fx-gain` is the example) or, on the
Digitakt II or Digitone II, loaded at run time from a `.dkm` (`test/dyn-fx-gain`, see "Run-time effects" below; `[contract] fx` is [1, 0]). Knobs for the parameter words are an FX page of the effect ("Define an effect with its own page" below); `tail_blocks` comes later.

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

## Run-time effects (Digitakt II and Digitone II, `.dkm`)

A module with `dynamic = true`, `type = "fx"` and a DSP object for `dt2` and `dn2` declares its effects in `module.toml`:

```toml
[[fx]]
id = "gain"
process = "_dg_process"   # global text symbol of the process routine
desc = "_dg_desc"         # data symbol of its dk_fx_desc (n_params <= 8, state_bytes a multiple of 4)
defaults = [0x3f000000]   # the words p0 ... of the row (at most n_params); 0 where none
devices = ["dt2", "dn2"]
```

`patchkit pack MODULE --device dt2` (or `dn2`) makes a `.dkm` whose DSP section (version 4) has one record per effect and whose contract bytes say fx 1.0 (and no machine). There is no state symbol: the runtime
reserves `state_bytes` behind your image and passes the address in R4. The runtime takes a free row of the chain (the device has `fx_slots` of them, 3 on both devices: `CAPACITY` `FX_SLOTS`, the DIGIKIT menu's `FX`), sets p0 to p7 to
your defaults and `enabled` to 1, and publishes the pc last. A fourth effect is refused by the fit rule before the load and by the loader (`E_DSP_ROWS`) at boot. The effect is on from the start; with a page its words follow the knobs. Template: `tests/fixtures/modules/dyn-fx-gain`; proof: `tests/digikit/scenarios/fx-dyn-dt2.toml` and `fx-dyn-dn2.toml` (`pk_digikit_scen run fx-dyn-dt2`). The Digitone II's image needs `core/dynfx` and `core/fxpages` named in the module list (they are in its `device` patch set, not in `emulator`: the effect hook costs the emulator about x2.4 DSP CPU there, finding 55).

## Define an effect with its own page (Digitakt II and Digitone II, `.dkm`)

An effect with a `tab` gets a page after CHO in the FX view (FUNC + FX, then UP/DOWN): DEL, REV, CHO, then the pages of the loaded effects. You write the DSP code and the manifest; there is no
ColdFire code and no effect-specific code anywhere else. Example (`tests/fixtures/modules/dyn-fx-gain/module.toml`):

```toml
[[fx]]
id = "gain"
process = "_dg_process"
desc = "_dg_desc"
tab = "GAN"             # the tab label of the page: 1 to 3 characters
title = "Test Gain"     # the page title: 1 to 23 characters, mixed case like stock ("Delay (1/4)"); the view adds (n/N)
mix = 127               # the default of the MIX knob, 0 to 127 (default 127 = wet)
devices = ["dt2", "dn2"]

[[fx.knob]]             # up to 7; knob i is the parameter word p_i your process routine reads
name = "Gain"           # the long name shown while the knob turns ("Gain=98"): 1 to 23 characters
short = "GAIN"          # the label under the knob: 1 to 6 characters
min = 0.0               # the float your routine gets at the bottom of the knob ...
max = 1.27              # ... and at the top; the value is linear between them
default = 0.5           # inside min .. max; it is also the word's value at load (`defaults` is not allowed with a page)
display = "percent"     # "percent" (0 .. 127, the default) or "bipolar" (-64 .. +63, for a knob that is centred)
position = 0            # 0 to 7: the place on the page (default: the knob's place in the list)
```

- **Limits.** 3 effects at a time (`fx_slots`), each with its own page; 7 knobs and MIX. Knob i is p_i, so the effect reads at most p0 to p6 from knobs (`n_params` in your `dk_fx_desc` must cover the
  knobs; p7 is free for a constant). Text is printable ASCII. Write titles in mixed case like the stock pages ("Freq Shifter", not "FREQ SHIFTER"); the firmware font draws a capital Q like an O.
- **MIX is the framework's.** The MIX knob sits on the page's level slot (label MIX, long name "Mix"). Your routine writes only the wet signal; the adapter makes
  `master = dry + mix * (wet - dry)` per sample, mix = raw / 127 in the display's steps (127 is exactly the wet signal, 0 is exactly the dry one, bit for bit). It costs about 7 DSP instructions a block at
  mix 1.0 and about 400 at another mix.
- **How the values arrive.** In `params` (R12) as the IEEE float `min + (max - min) * raw / 0x7f00`, about 3 ms after a turn (the runtime polls the kit once per UI-loop pass and writes one word per
  transfer). At load every word is its knob's `default`, and after a Load Kit the stored value (below). The range is fixed at 0x7f00 steps, so a knob has about 127 detents' worth of travel at its resolution; choose the range so that a turn is audible.
- **Values are saved with the kit.** Save Kit, a project save and a pattern's kit keep the knobs' and MIX's values of each loaded effect together with an effect ID (a number the packer
  makes from your module id and the effect's `id`: `register::effect_id`; do not rename either once users have kits). Load Kit gives an effect its stored values only when the ID stored for
  its row is its own; a different effect in that row, an empty row, a kit saved without the effect and a kit saved by stock firmware all give it the defaults of your manifest (each knob's `default`, MIX
  `mix`). Your effect never sees a zero in between. A new kit gets the defaults. A kit saved with effects also loads on stock firmware (it ignores the extra bytes). Effects count by row: the same two
  effects loaded in the other order (the selection's order sets the rows) get defaults, not each other's values.
- **Not supported.** P-locks, LFO and other modulation on a knob, MIDI CC, a second page for one effect, a knob that does not reach the DSP as a float. An image without `core/fxpages` (the `emulator` and `device` patch sets of both devices have it) refuses a module with
  a page (`E_NO_REGISTRY`). The pages are the same on both devices (the Digitone II's effect pages are numbers 37 to 39, its FX view lists DEL, REV, CHO as there).
- **Check it.** `patchkit pack MODULE --device dt2 -o x.dkm` refuses a bad page (a text too long, a default outside the range, two knobs at one position); the scenarios `tests/digikit/scenarios/fx-pages-dt2.toml` and `fx-pages-dn2.toml`
  (`pk_digikit_scen run fx-pages-dt2`) is the proof for the fixture: the page's screenshots, the PCM at the knobs' values, MIX 0 equal to the dry sound.
  `tests/digikit/scenarios/fx-persist-dt2.toml` and `fx-persist-dn2.toml` (`pk_digikit_scen run fx-persist-dt2`) prove the saved values: Save Kit and Load Kit, a kit without the effect, the effects in the other rows, a project, another pattern's kit.

- **Insert or send (the framework's choice; you give the default).** An effect runs on the master chain (an insert) or on a send bus; the `process` routine is the same for both, and on a send it gets the sum of the tracks' send amounts as its input block, and its
  wet output times MIX is added to the FX return. Your manifest says where the effect starts and which routes make sense for it; the user decides, in DIGIKIT > FX ROUTES (a setting, `fx/route/<module id>`, which wins over your default when the effect allows it):

  ```toml
  [[fx]]
  id = "reverb"
  ...
  route = "send"                  # the default route: "master" (the default) or "send"
  routes = ["send", "master"]     # the routes the user may choose; left out: see the rule below
  ```

  *The rule.* `route` left out is `"master"`. `routes` left out is `["master"]`, or `["send", "master"]` when `route = "send"` (a send effect can always fall back to the master: the master has no slot limit). A send-only effect says `routes = ["send"]` (with
  `route = "send"`), a master-only one says nothing (the modules before these fields keep their bytes: `route`/`routes` default to what the old record meant). `patchkit pack` refuses a name that is not `master` or `send`, an empty `routes`, a route listed twice and a `route` that
  `routes` does not list, and any send route on an effect without a `tab` (the send knob and the FX ROUTES row are labelled with the page's tab: an effect without a page is an insert). A stored route that the effect does not allow (a module updated to be master only) is ignored: the default holds. Choose `routes = ["master"]` for an effect that makes no sense on a send (a gain, a limiter, an effect with a dry path of its own), `["send"]` for one that is only a
  return (a reverb that has no dry signal), both for the rest. The record carries both (flags of the effect record, `docs/design/digikit-dkm.md`); a runtime that does not know them runs the effect on the master.

  **The send amounts are framework-owned**: one knob for each send effect on a second page of the track FX key (the label is your `tab`, `<tab> Send=40` while it turns), one amount for each track, kept in the settings store and the DSP row, never in your state or parameter words
  and never in a kit or pattern file. The effect never sees an amount and has no way to read or set it; MIX is the return level of a send. The device allows a fixed number of send effects at a time (`send_slots`: two on the Digitakt II, one on the Digitone II); an effect beyond that stays an insert (FX ROUTES does not let the user choose send
  when the slots are taken, and a default beyond the limit runs on the master). Function + FX lists DEL, REV, CHO, then the send effects, then the master effects (each in the order they were loaded). A changed route takes effect after a restart (FX ROUTES offers it).
  Author's rules: do not assume you are the last in a chain, and write the wet signal only (a send effect has no dry path). Both devices (Digitone II: finding 67; its send bus is the sum of the voice slots of the track). See `docs/findings/62-fx-send-route.md`, `64-fx-send-page.md`, `65-fx-routes.md` and `67-fx-routes-dn2.md`. Fixtures: `tests/fixtures/modules/dyn-fx-gain-send` (`route = "send"`) and `dyn-fx-gain-sendonly`
  (`routes = ["send"]`); proof `tests/digikit/scenarios/fx-routes-dt2.toml` and `fx-routes-dn2.toml`.

Judged by `tests/modules/test_fx_conformance.py` (`mise run module:check NAME`): signals, parameter limits, modulation, switching, a long run;
checks: returns, registers, the adapter did not refuse a block, write set, instruction budget (`pk_conform.fx_budget()`, about 31,000 for the effect today, and 900 for the adapter; `devices.md`, "Effects").
Template (patched in): `tests/fixtures/modules/fx-gain/src/dsp/fx_gain.s`. Emulator proof of the adapter: `tools/patchkit/pk_proof_fx.py`. The conformance suite judges patched-in effects (an `[[entry]] registry = "fx"` row) and run-time effects (a `[[fx]]` record: the rig reserves the state the runtime would, and a page knob's range gives the float of each parameter word the cases use); the scenarios above prove the page and the loading.

## The reference effect: `digikit/freqshift`

`modules/fx/freqshift` is the effect to copy for a real one: a run-time module with a page (tab FSH, one bipolar knob, `route = "send"` with MIX default 127), state in the descriptor (664 bytes: filter states, a delay buffer, an oscillator), a float32 reference model that defines the
arithmetic (`tools/patchkit/freqshift_ref.py`), a test that holds the routine on the rig to the model (`tests/patchkit/test_freqshift.py`) and an emulator scenario for the page and the sound (`tests/digikit/scenarios/fx-freqshift-dt2.toml` and `fx-freqshift-dn2.toml`). Its cookbook entry (finding 54) lists what was hard. Patterns worth taking:

- A knob that is centred: `display = "bipolar"`, `min = -64.0`, `max = 63.0`, `default = 0.0` makes the float the shown number; a mapping with `2^x - 1` and no constant term gives exactly 0 at 0.
- A send effect (`route = "send"`, `routes = ["send", "master"]`) with MIX default 127: on a send MIX is the return level (0 adds nothing), so loading the effect changes nothing until a track has a send amount. As a master insert the same default means fully shifted, and MIX is one value for both routes: pick a default that works as a send, and say in the README what it means on the master.
- Two-pass block code: oscillator values for the whole block into the state, then one pass per chain with its states in registers (a subroutine called per channel; the loop body is the unrolled even/odd sample pair).
- An integer phase accumulator (never a float one) for anything that must not drift; ramp the increment inside the block.
- Clamp the output to +-7.9: an input at the level bound times a filter's overshoot would otherwise refuse the block.
