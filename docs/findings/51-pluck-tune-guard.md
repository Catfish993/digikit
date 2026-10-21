# 51. An LFO on pluck's TUNE silenced every track, and the runtime guard in the voice wrapper

Phase 11 item 1 (docs/plans/digikit-runtime.md). Marks as in AGENTS.md: **[D]** measured or read once here, **[O]** open; nothing is **[V]** (no second
agent has checked the image bytes). Firmware: Digitakt II 1.17 for every address; the sources are the same for 1.16. Emulator and rig only: **the device has not
run runtime 5.**

## 1. The failure

On Em's Digitakt II an LFO on the TUNE knob of `digikit/pluck` (run as `test/dyn-pluck`) silenced every track. The DSP kept running (`dispatches` = `returns` in
the log, no `DSP_STOPPED`): nothing halted, a voice's block carried Inf/NaN into the send FX, which keep it (their feedback never loses it).

**Cause [D].** `pluck.s` computes `x = (pitch + TUNE - 16384) / 3072` with no limit and the loop period `P = P0 2^-x` (P0 = 5870.99, `fs / f0` of note 0 plus
a whole tuning octave offset). The delay line serves `N = clamp(trunc(P - tau - 0.5), 2, 4090)` and the fraction `R = P - tau - N`, which the all-pass
coefficient polynomial needs in [0.5, 1.5). When `P - tau` is below 2.5 or above 4091.5 the clamp of N leaves R outside, the polynomial
(degree 7 in `delta - 1`) returns `|a| >= 1`, the all-pass `ap = a (lp - ap') + lp'` is unstable, and `lp'`, `ap'` and the line become Inf/NaN. They stay so
until the next note-on (the line is refilled then), and the voice never ends (the NaN peak compare, `COMP(F13, F0)`, never counts a quiet block). A sweep on the
routine rig (scratch, not in the repository) found, among others: note 60 with TUNE below about 0xAA0; notes 0 to 6 at the default TUNE; note 100 with TUNE
0x7c00 and above; every note from 100 at 0x7f00. In-range knob values reach it (TUNE 0 .. 127 is words 0 .. 0x7f00 and the pitch word adds the note).
DECAY, PICK, BRIGHT and COLOR are clamped in the routine and were clean. The same arithmetic is in `pluck_dn2.s`: notes 0 to 6 failed there too.

## 2. The pluck fix

Three clamps in `pluck.s` and `pluck_dn2.s`, nothing else (the in-range sound is unchanged: the 7 tests of `test_pluck.py` pass, the `test_pluck_tune*.py`
tuning tests pass, the float32 model `tools/patchkit/pluck_ref.py` has the same three operations):

- `x` to 0 .. 11.2 (`P` at 2.5 or more; no word makes x negative);
- `P - tau` to 2.501 .. 4091.49, so N is 2 .. 4090 with R in [0.5, 1.5) whatever x and BRIGHT are (tau is at most 0.5);
- `a` to -0.9 .. 0.9 (the valid range is about -0.2 .. 0.33; the stability limit is 1).

A pitch outside the string's range plays the nearest period the line can hold. `tests/patchkit/test_pluck_range.py`: the model's coefficients for every note 0 .. 127 at
256 TUNE words (every 0x100 up to 0xffff, 0x7f00, 0xffff) and both extreme BRIGHT values; 200 blocks of the model's string for every note at TUNE 0, 0x1000, 0x4000,
0x7c00, 0x7f00, 0xffff (finite, below 2.0 in magnitude, finite state); the routine itself (concrete SHARC+ core) at five corner cases against the model. In range nothing moved: ten note/parameter sets (notes 8 to 127, TUNE 40 to 70, DECAY, BRIGHT, COLOR, PICK from 0 to 0x7f00), ten blocks each through the wrapper, are bit-identical between `a80274dc` (wrapper and routine) and now. The old routine is non-finite from block 0 in all three cases
tried (note 100 at 0x7c00, note 0 at 0, note 60 at 0x400). A rig run of 200 blocks costs about 0.4 s a block, so the full grid runs on the model, which the rig
agrees with to 2e-5 (the existing model test).

## 3. The runtime guard (`core/dsp-voice`, Digitakt II wrapper)

A routine can have another bug of the same kind; the wrapper now checks what it cannot trust. All inside the registered path of the even voice (the odd voice
copies the even voice's block).

**(a) Parameters.** Before the routine runs, the eight machine knob words of its track (frame halfwords `0x6d + 48 t + j + 2 (j >> 2)`, the same positions
`_dv_frame_hw` reads) are clamped **in place in the DSP's frame copy** to the row's declared range and put back when the routine has returned (a word the clamp
changed is saved; the engine's later stages see its own words). Undeclared (0): 0 .. 0x7f00. Ranges come from the manifest, no new field: the `min`/`max` of the `[[param]]` that the
machine's `knobs` name (`typed.rs voice_ranges`; a stock code such as TUNE `0xca` or LEV `0xd1`, an empty knob, or a range outside 0 .. 0xffff is
undeclared). They travel in the **voice row**, which grew from 2 to 16 words: `type`, `pc`, `rng0 .. rng7` (min | max << 16), `dis` (the type the guard disabled), `wstart`,
`wcount`, `nfault`, two spare. The static registry gets the ranges from patchkit's lowering; a run-time module gets them from its `.dkm`: the DSP section is version 2 with
48-byte voices (type, entry, eight ranges, two reserved), runtime 5 also loads version 1 (ranges undeclared), and the runtime publishes a row in 11 one-word transfers (eight ranges, pc, `dis` cleared, type last).
`core/dsp-link` window 1 counts rows of 16 words. **[D]** `test_dsp_guard.py`: in range the routine sees the engine's words and the frame is bit-equal after the call; with out-of-range words it sees the clamped ones,
the frame is restored and the neighbouring words are untouched.

**(b) Output.** After the routine returns, one integer test over the 32 samples (sign bit cleared, magnitude bits above `0x41000000` = 8.0, which also catches Inf and
NaN above `0x7f800000`). 8.0 is +18 dB over the full scale of a voice: pluck stays below 1.5 (the test bound is 2.0) and a voice block is a signal before the engine's gain stages, so a block above 8.0 is taken as a bug and not as a loud sound (no stock voice was measured against it); the
bound is one constant in `dsp-voice.s`. A fault zeroes the block, **ends the voice** (ACTIVE and the note-on mark of the record clear: the least invasive reset that
guarantees a clean next block without knowing the routine's state; a routine that follows the contract renders silence for an idle voice and starts at its note-on path at the next trig; for pluck the
note-on path rebuilds every state word, the line included), and counts the fault in the row (`wcount`, `nfault`). The 8th fault in 1500 blocks (one second) sets the row's `dis` to the type: from then on the wrapper writes a silent block for that type and never calls the routine
(the stock path would play the cloned sample machine, which is worse than silence). The disable lasts until the next start. **[D]** `test_dsp_guard.py`: NaN and 100.0 fixtures are zeroed, ACTIVE/mark clear, kind 1/2, count 1; ten faults 1600 blocks
apart leave the type enabled, then 7 in a second disable it; a disabled type is not called (ACTIVE untouched, no new fault); the clean fixture is untouched.

**(c) Report.** `dl_diag` grew from 17 to 24 words (`modules/core/dsp-link/README.md`): 17 = faults (16 bits) | kind << 16 | types disabled << 24, 18 = type | voice << 16, 19 to 22 the eight words of the call (as the routine
saw them, clamped), 23 = the type disabled last. The words live in rows 1 to 12 of the reply record, entries 11 and 12 as words 0 to 16 do (finding 31; the ColdFire reads those entries only for parameters 36 to 39, **[O]** unchanged:
rows 10 to 12 are new, and nothing in the scenarios or the rig reads them). The ColdFire runtime (`dlog.rs`) logs a new fault count as `MACHINE_FAULT` (kind 27: type | voice << 16, kind | faults << 8, knobs 0 to 5) and a new disabled type as
`MACHINE_DISABLED` (kind 28: type, types disabled, faults), both error events; the poll is a second apart, so a burst is one event whose count says how many. `tools/device/dklog.py` names the fields. `RUNTIME_VERSION` is 5.

**Digitone II.** Not guarded. The wrapper scans rows of 16 words and its diagnostic block is 128 bytes (the new words read 0). What is missing for a guard: the DN2 voice slot has no record to
end (the routine keeps its own per-slot state and starts a note on the rising edge of a trig bit), so there is no generic reset; the knob positions differ (`0x6d + 0x49 v + (s - 25)`, from the page, not the machine row), so `voice_ranges` returns undeclared for it. A DN2 guard would
need the routine to export a reset entry (or a state size to clear) and `voice_ranges` for pages.

## 4. Cost **[D]**

Rig instruction counts per call (`tests/support/dsp_voice_rig.py`, friendly context, concrete core), before = `a80274dc`:

| call | before | after |
| --- | --- | --- |
| pluck, steady block (active, even voice) | 1362 | 1776 (+414) |
| pluck, note-on block | 7308 | 7722 (+414) |
| pluck, idle (ACTIVE = 0) | 288 | 690 (+402) |
| odd voice (copy) | 260 | 262 |
| stock type (falls through) | 69 | 69 |

About 400 instructions per registered even voice and block: the clamp loop (8 knobs, one `CALL` each, about 270), the output test (3 instructions a sample, 100) and the setup. With `tools/patchkit/pk_dsp_budget.py` factors for the Digitakt II
that is 0.070 % (nominal) to 0.087 % (high) of the DSP per active registered voice, 0.56 % to 0.70 % if all eight tracks hold a registered machine. The link's diagnostic copy adds 14 instructions per block to every image with `core/dsp-voice`.

## 5. Emulator

`debug-guard` (`tests/digikit/scenarios/debug-guard.toml`, runner `run_guard` in `tools/patchkit/pk_digikit_debug.py`, DT2 1.17, full tier): `test/dyn-pluck` on track 1 (held), `test/dyn-guard-nan` on track 2 with its delay send raised, ten TRIG 2
presses in a second. **[D]** `MACHINE_FAULT` type 20, track 2, nonfinite, the parameter words; the count reaches 8; `MACHINE_DISABLED` once; a press after the switch-off causes no fault; the master capture holds no NaN or Inf; the pluck stays above -70 dBFS for the first 1.1 s. The task
asked for the stock ONESHOT on track 2; the digikit card of the boot harness has no sample, so the audible track is the run-time pluck (the guard treats both alike). It was not run against an unguarded image, so this scenario alone does not show the old failure; the rig does: the old pluck is non-finite from block 0.
Existing scenarios `equiv-dyn-pluck`, `menu-dkrun-pluck`, `rung-c` pass (see the lane report for the runs).

## 6. Open

- **[C]** The guard has run on a device: rt021 (runtime 0.2.1) with `digikit/pluck` and an LFO on TUNE at large depth on a Digitakt II, 2026-10-21: 0 faults, no `MACHINE_FAULT` (the clamp prevents it). The guard has not yet fired on a device. Sound result: finding 54.
- **[O]** The new reply-record words are in rows 10 to 12 (entries 11 and 12). The same argument as for rows 1 to 9 (finding 31); not read by any firmware path found.
- **[O]** The clamp loop could be about 100 instructions cheaper if the frame position of a track's knobs were computed once instead of per knob.
