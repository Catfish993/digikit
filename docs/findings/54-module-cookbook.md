# 54. Module cookbook: what each reference module taught

One entry per module: what it does, what it costs, the traps found while building it, the patterns worth reusing. Every
module PR adds or updates its entry. The agent skill (`.agents/skills/digikit-module/`) links here; facts that every module
needs move into the skill's references. Marks as in docs/FINDINGS.md.

The reference set (Em, 2026-10-21): pluck (machine, physical model), a modal resonator (machine, resonator bank), the
frequency shifter (effect, after the run-time FX adapter of P3d-1).

## digikit/pluck (machine, Karplus-Strong string; both devices)

- **What:** a noise burst excites a delay line of one period; the loop filters it (one-zero damping, a first-order all-pass
  for the fractional period); a comb at the pick position shapes the burst. Parameters DECAY, PICK, BRIGHT, COLOR (the 4
  words the DSP receives). Source: `modules/machines/pluck/src/dsp/pluck.s`, one file for both devices.
- **Cost [D]:** in the engine, one ringing voice adds about 900 instructions per block in the first 0.3 s and 561 on
  average over 2 s; 16 voices about 14,400 (2.5 to 3.1 % of a block) (finding 06, "Modules"). On the rig a steady block is
  under 1,900 instructions with the adapter and guard (`tests/patchkit/test_pluck.py`); the adapter and guard are about 700
  of that. Declared budget: `max_cycles_block` 2200, `max_cycles_note_on` 9000.
- **Memory [D]:** 256 KiB of delay lines (16 voices x 4096 words): half of the Digitakt II run-time DSP area (512 KiB) and
  most of the Digitone II one (320 KiB) (finding 32).
  Size per-voice state for the lowest musical note, not for the arithmetic limit: pluck's 4096-word lines are 258 KiB; 2048 words would still reach the lowest note, about 23 Hz (finding 57: the area is whole 64-byte granules and the loader refuses a module that does not fit with `E_DSP_NOSPACE`).
- **Traps found [D]:**
  - An LFO on TUNE moved the period past what the delay line serves; the all-pass went unstable and Inf/NaN reached the
    send FX of every track. Fix: clamp the pitch, the period (2 .. 4090) and the all-pass coefficient (+-0.9); the
    runtime guard now zeroes a non-finite or too-loud block and disables a machine after 8 faults in a second
    (finding 51).
  - A store of a same-DAG register through an index register ran in the emulator and halted the DSP on the device (rung C,
    finding 49 section 10.7). The build now reports these stores.
  - Knob slot 3 of a Digitakt II SAMPLE clone is the sample word: a parameter there is refused (finding 02, P3c-4).
  - The Digitakt II gives an added machine one SRC page only (finding 50).
- **Device [D]:** played on a Digitakt II 1.17 at rung C4 (finding 47), before the SDK v1 changes. Re-tested by Em on
  2026-10-21 with image rt021 (runtime 0.2.1) and `mise run dk:run -- digikit/pluck --port Digitakt`: MOD_LOAD 1, MOD_DSP 0,
  plays, guard 0 faults, DSP 42-46 %, type 16 (the old PLUCK-D sticky entry holds 18).
- **LFO on TUNE, device result [D]:** at large depth (worst with the random LFO) the sound crackles and is almost silent
  at the extremes; no guard fault. This is the design, not a fault: the period N jumps once per block (no ramp inside the
  block); when the pitch drops, the longer line holds samples that were never excited; when it rises, the shorter loop
  decays fast. A machine that takes pitch modulation must ramp its pitch-dependent state inside the block and keep its
  whole state excited. Fix planned as pluck v2 (fractional delay read ramped over the block, whole line excited at
  note-on, optional TUNE modulation range).
- **Patterns:** `references/dsp-recipes.md` quotes them (parameter word to float, the block loop, noise, damping, all-pass,
  delay line with a mask, pick comb, silence detection to `DK_DONE`).

## core/fx and test/fx-gain (effects adapter and its fixture; both devices)

- **What:** the FX contract (`sdk/dk_fx.h`) is run by `core/fx`, which hooks the first instruction of a post-master function on both devices and
  walks a chain of up to 4 effects. `test/fx-gain` is the smallest effect (0.5 gain). Details and evidence: finding 55.
- **Cost [D]:** an empty chain is 16 instructions per block on the rig (+18 busy instructions in the smoke cell); a chain whose rows are all off 174; one
  effect steady 732 (the 0.5 test effect itself about 210; the adapter's copy, output check and bookkeeping are about 520); a fade block 1065.
- **Traps found [D]:**
  - An entry hook has no stock frame yet: save every register in a static area, as `dsp-voice-dn2.s` does; the DT2 stock function saves its own
    I2, I3 and I5 only, the DN2 one reads R4, R8, R12 and M3 as arguments and the caller needs R14 and R15 back.
  - The context registers (M5-M7, M13-M15, L0-L5, MODE1) belong to the DSP context: save and pin them for the chain, restore before the stock code.
  - `.PKBSS` memory is not loaded; the adapter zeroes an effect's state when it switches on, the rig poisons it to prove that.
  - A master hook at the DAC converter (H0, master-gain / master-drive) is after the USB pack: the USB audio does not carry it (finding 55).
  - Patching a function bars the emulator's AOT regions of its whole hashed code range: the same adapter costs x1.03 on the DT2 at `FUN_1c80f2` and x3.09 on the DN2 at
    `FUN_1c244d` (finding 55). Choose a hook site by its range before anything else.
- **Patterns:** `references/contract-fx.md`; the rig (`tests/support/fx_rig.py`) and the effects for tests (`tests/support/fx_effects.s`) are templates for
  an effect's unit test.

## digikit/remote (a service module: SysEx commands of its own; both devices)

- **What:** a run-time module that registers commands with `Api.register_cmds` (api 1.2) and calls the runtime's imports `input_key` / `input_turn` (core/input). One C file, `modules/tools/remote/src/c/remote.c`; the packed `.dkm` is identical for both devices (1032 bytes).
- **Shape:** `dkm_init(api)` registers one range per `register_cmds` call; a handler is `int h(cmd, seq, body, len, out)` and returns the reply length or a negative status; it runs in the MIDI task, so it is short and never waits.
  Use a range that `sdk/sdk.toml` `[protocol.ranges]` reserves; a new service adds its range there first (`mise run gen:modules`).
- **Guard:** put commands that act on the instrument in a `[protocol] guarded` range: the runtime answers `E_DISABLED` while the owner has the REMOTE switch off, the module needs no check of its own.
- **Traps [D]:** a request number is 6 bits (bit 6 marks a reply), so a range cannot start at 0x40; an import that the image does not have (core/input missing) fails the load with `E_IMPORT`;
  the firmware clamps a flushed turn to 30 detents, so the module keeps a pending estimate and returns the delta it took.
- **Test:** scenario `remote-control` (live, both devices), fixtures `test/svc-echo` and `test/input-probe` are smaller templates.
