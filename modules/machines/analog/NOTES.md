# analog — state, limits, and what is deliberately not built

A subtractive voice for the Digitakt II: two oscillators on one continuous wave slider, a fixed reese-capable
detune, a reflection wave folder and a sub. It runs before the track's FLTR and AMP, so the DT2 does the shaping;
that is the whole design idea — a usable synth voice without loading single-cycle waveforms into the sampler.

## Panel (DT2 1.17, machine type 12)

| slot | knob | range |
|---|---|---|
| 1 | DET | 0 = two saws in unison, 1 = 3 % apart (the rolling reese beat) |
| 4 | WAVE | one slider: saw -> triangle -> pulse (width 5..50 %) -> square |
| 6 | SUB | sub level over 0 .. 3/4, wave-folder drive over 3/4 .. 1 |
| 0 / 7 | TUNE / LEV | stock |

## The slot constraint that shapes everything

Only **three** knob positions deliver a module value on a DT2 SAMPLE clone:

| slot | stock row | delivers? |
|---|---|---|
| 0, 7 | TUNE, LEV | stock, not available |
| 1 | PLAY | **yes** |
| 2 | (empty, `clr.l` on all 7 stock machines) | no |
| 3 | SAMP (sample slot) | no — only the sample number bits reach the DSP |
| 4 | STRT | **yes** |
| 5 | LEN | no — the firmware maintains LEN for the DSP itself, so a value written there never reaches the frame |
| 6 | LOOP | **yes** |

Evidence: docs/findings/02-machines-and-parameters.md, the slot table and "Knob slots 3 and 6 on a run-time SAMPLE
clone"; devices/digitakt-ii.toml `[modules.frame]` and `[adapter.machine] param_slots`. SAT lived on slot 5 for two
builds and never responded on hardware for exactly this reason.

Consequence: four features (detune, shape, fold, sub) share three knobs, and the folder rides the top quarter of SUB.
Adding a fifth control means either trading one of the three or waiting for the platform to expose another slot.

## Measured behaviour (note 48, 110 velocity, saw setting unless said)

- DET: window-to-window level spread over 150 blocks 24 % at 0 -> 74 % at full; rms 0.126 -> 0.087 as the two saws
  beat out of phase. That is the reese roll, not a chorus. (A fixed 0.5 % detune reads as "two saws slightly apart"
  and lost the effect — see v0.11 and its revert.)
- WAVE: h2/h1 0.50 (saw) -> 0.11 (triangle, even harmonics nearly gone) -> 0.14 (square, odd series).
- SUB: f0/2 energy 0.014 -> 0.145 over the sub part of the knob.
- Folder (top of SUB): drive 1..3 into `y = x - 2(x - clamp(x, +-1))`. h3/h1 0.331 clean -> 0.273 (one fold) ->
  0.505 with h5 0.398 and h9 0.163 at full, rms held at 0.124 — level-neutral grit, unlike a saturator.

## Not built: the arpeggiator (deliberate, 2026-07-22)

Verified as feasible: `note_on` runs on every trigger *including a retrig* (the trig page's retrig is therefore a
clock), and state past `state_clear_bytes` survives across retriggers, so a step index and a pitch table can persist.
The pattern would be a per-mode semitone table and `increment x 2^(n/12)` via `_dv_exp2`.

Why it is not in: the track is monophonic, so the arp cycles a *shape* from the played note rather than held chord
notes — fine — but a mode knob needs a fourth slot, and there is none. Options when it is revisited: (a) merge the
detune into WAVE (detune open first, then shape) to free slot 1, which costs independent detune and unison-square;
(b) fix the mode at build time and let retrig switch it on, needing no knob; (c) wait for the SDK to expose another
parameter slot or a note-stream hook.

## Traps this module has hit (all real, all cost a build)

1. **`F`/`R` are one register file.** `R10 = ...` overwrites `F10`. Fine in the constant table
   (`F12 = increment 1`, `F15 = fold drive`, ...) is the first place to look when a value is silently wrong.
2. **An integer-sourced conditional assign does not work**: `IF LT F1 = PASS R1` applies unconditionally. Use an
   `F`-to-`F` conditional (`IF GE F13 = PASS F2`, as the phase wraps do) or avoid conditionals with `MIN`/`MAX`.
3. **An immediate load between the flag-setting op and the conditional clobbers the flags** — the arithmetic that
   sets them must be adjacent to the conditional.
4. **`patchkit patch` does not rebuild modules.** After `patchkit gen` (any `devices/*.toml` or manifest change) the
   affected core modules must be rebuilt with `pk_build`, or the image silently carries stale glue.
5. **Parameter word order is knob-slot order**, not manifest declaration order.
