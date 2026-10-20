# The contract voice (`sdk/dk_machine.h`)

Entry: the adapter in `core/dsp-voice` enters your routine with `I13 = return address; JUMP (M13, I12)`; you return with
`JUMP (M13, I13)`. Arguments: `R4` = state, `R8` = `dk_voice_in`, `R12` = parameter words. Result in `R0`.

```
dk_voice_in (words):  0 pitch (semitones << 8, the tune knob added)   1 velocity (0 .. 127 << 8)
                      2 block (blocks since the note-on)               3 out (pointer to 32 floats)
params:               8 words; word i is the module's parameter i, clamped to its [[param]] min .. max
```

- `render` runs once per block of 32 samples (48 kHz) while the voice runs, also on the note-on block after `note_on`. Write 32 floats to
  `out`. Return `DK_OK` (0), or `DK_DONE` (1): the block is written and the voice has ended.
- `note_on` runs once per note after `state_clear_bytes` of the state were zeroed. Return 0, or a negative number (a fault).
  Do not put `note_on` at offset 0 of the image: an offset of 0 reads as "no note_on".
- `desc` (`dk_machine_desc`, 10 words): magic `DK_MAGIC`, contract `DK_MACHINE_V1`, parameter count, voices (1 to 16), state bytes per
  voice (multiple of 4), bytes cleared at note-on, max cycles of a block, max cycles of the note-on block, max call depth, stack bytes.
- The state is per voice and is the only memory that survives between blocks. No statics.
- Include `dk_machine.inc` for the constants. `_dv_exp2` (F0 = 2^F0) is a helper of `core/dsp-voice`: `.EXTERN _dv_exp2;` clobbers R0 to R3.

Registers: free R0-R15, I0-I5, I13, M1-M4, B3. Keep I6-I12, I14, I15, M0, M5-M15, the L registers and MODE1. The adapter pins
M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L5 = 0, SIMD off, RND32 set.

The guard checks every block: any sample not finite or above 8.0 in magnitude zeroes the block, ends the voice and counts a fault; 8
faults within 1500 blocks switch the machine off until the next start.

The conformance suite (`tests/modules/test_machine_conformance.py`, `module:check`) runs the routine on the concrete SHARC+ core: it
must return, keep the context registers, produce finite samples below 8.0, not fault the guard, and stay in the instruction budget.
Template: `tests/fixtures/modules/sine/src/dsp/sine.s`. Pitch tests: `tests/patchkit/test_sine.py`.
