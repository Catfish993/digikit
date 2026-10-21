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
- **Traps found [D]:**
  - An LFO on TUNE moved the period past what the delay line serves; the all-pass went unstable and Inf/NaN reached the
    send FX of every track. Fix: clamp the pitch, the period (2 .. 4090) and the all-pass coefficient (+-0.9); the
    runtime guard now zeroes a non-finite or too-loud block and disables a machine after 8 faults in a second
    (finding 51).
  - A store of a same-DAG register through an index register ran in the emulator and halted the DSP on the device (rung C,
    finding 49 section 10.7). The build now reports these stores.
  - Knob slot 3 of a Digitakt II SAMPLE clone is the sample word: a parameter there is refused (finding 02, P3c-4).
  - The Digitakt II gives an added machine one SRC page only (finding 50).
- **Device [D]:** played on a Digitakt II 1.17 at rung C4 (finding 47), before the SDK v1 changes; the current
  `digikit/pluck` on runtime 0.2.1 is being re-tested (2026-10-21).
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
