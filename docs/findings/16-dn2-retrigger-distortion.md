# 16. DN2 retrigger distortion (investigation notebook)

Marks: **[V]** verified here, **[D]** documented, **[O]** open, **[C]** corrects.

## Symptom

In the coupled DN2 1.11 emulator, retriggering Trig 1 while the previous note
still sounds sometimes renders a bad note from the note start: 261.6 Hz plus
15-185 Hz non-harmonic content, and about 11% of samples flattened at 0.693.
Real hardware never does this (Em). **[V]**

## Facts

- Deterministic: the same inputs give the same PCM. **[V]**
- Identical on all DSP tiers (interpreter, AOT, fast). Note: the tiers share
  semantics generated from `tools/sharc_core`, so agreement does not rule out
  a semantics bug. **[V]**
- Not the playback path. **[V]**
- CF to DSP frames are identical for a clean and a bad run, except the trigger
  timing. **[V]**
- Bad or clean depends only on the press frame P. The pattern is strict: 3 bad,
  3 clean. The period is about 5.75 frames, one period of the 261.6 Hz note at
  1500 frames/s. **[V]**
- The slope of the sounding note's bus sine at 0x257ee8.. predicts the result
  (25/25): falling is bad, at frames consume-2..consume-1. **[V]**
- In bad runs the group-2 voice record never gets its velocity and limit. The
  record tags are at 0x241520, 0x2415a4, 0x241628, 0x2416ac (stride 0x84).
  Relative to a tag: -0x18 level, -0xc velocity (0x7f), -4 hold limit (0xbb8),
  +4 counter. **[V]**
- The voice loop FUN_1c54c7 (DO 0x1c5616; decision 0x1c565e..0x1c5673 and
  0x1c651a..0x1c6549; gate R9 = DM(I15+0x894); limit DM(I5-4)) sends the voice
  to tag 2, then tag 1, forever. **[V]**
- Clean runs initialise at P+1 (velocity 0x7f, limit 0xbb8, tag 4). **[V]**

## Ruled out

All **[V]**:

- SPI delivery phase within the period (`DIGI_DSP_RX_PHASE` 0..400k).
- DSP clock and period (`DIGI_DSP_PERIOD` 400k..800k).
- Core timer phase.
- Idle skip.
- State-import fixup.
- LEFTZ/SV, LSHIFT/SZ and FCOMP semantics on the decision path (checked against
  the SHARC+ programming reference, printed p.24-14).
- Voice slot.
- Compressor parameters.

## Note-on path (1 s write-watch, interpreter-only, group-2 records)

Records: FUN_1c54c7 (called once per frame from FUN_1c8ef1 at 0x1c9627) gets
the record base in R4 (`I15`; group 2 = 0x2414e0, stride 0x84 between voices)
and a parameter block in R8 (`I1`). I-registers hold byte addresses; a
word-scaled `modify`/`DM(I+n)` offset is n*4 bytes, a `[raw-byte offset]`
modify is n bytes. **[V]** (addresses below checked against the write-watch)

- Trigger flag: FUN_1c2712 writes M14 (0x100) to record+0x224 (0x241704) at
  PC 0x1c2d35. FUN_1c54c7 tests its byte 1 (record+0x225, 0x241705) at
  0x1c5584/0x1c558c (skip the whole init if zero). The flag is cleared at
  0x1c57a1 (end of the call). **[V]**
- Init section 0x1c5505..0x1c55de computes, from the `I1` block:
  velocity R10 = trunc(clamp(((DM(I1+36)-0.5)*2 + DM(I1+10)) * 128, 0, 127))
  (0x1c5508..0x1c5566) and hold/limit R2 = trunc(f(...)) through
  CALL 0x1c06ba (0x1c5594/0x1c559e). It stores velocity at record+0x34
  (0x1c55a6 -> 0x241514) and limit at record+0x3c (0x1c55a9 -> 0x24151c), then
  the type-dependent resets (0x1c6603 path: 0x1c6607/0x1c660f/0x1c6611/..).
  **[V]** addresses and PCs; formulas are read from the listing, **[D]**.
- Voice loop 0x1c5616: `JUMP IF SV` at 0x1c566a sends limit==0 to 0x1c651a,
  which sets tag 2 (0x1c6549) then tag 1 (0x1c6597, next call), forever.
  Limit nonzero gives tag 4 (0x1c566d). **[V]**

Write-watch result, same call, 1 s scenarios `fine1s.json` (f00 bad press
P=1237, f01 clean P=1238; hashes d4ad6137.. / 2a2c8d30.. identical to the
default tiers):

- Both runs: flag 0x241704 = 0x100 at PC 0x1c2d35, about 31.5k DSP
  instructions before the init call (bad icount 825342968, clean 826009929);
  0x241708 = 0x3c00 (0x1c908f); 0x241710/14/0c = 0x3f7e0000 (0x1c5188..);
  0x1c660f/0x1c6611/0x1c6615/0x1c661d reset counters (0x2414f4, 0x241578,
  0x2415fc, 0x241680) at the init call; flag cleared at 0x1c57a1.
  **[V]**
- Clean: velocity 0->0x7f at 0x1c55a6 (+0x4 instr from 0x1c55a9: limit
  0->0xbb8), then tag 4 at 0x1c566d, level 0xbf7e0000 -> 0 at 0x1c5673.
- Bad: the init section DOES run (the type path writes at 0x1c660f.. happen,
  so neither 0x1c558c nor 0x1c55c5 skipped it), but velocity and limit are
  not changed from 0: the stores at 0x1c55a6/0x1c55a9 write 0 (no change
  event). The voice loop then sees limit 0 and takes tag 2, then tag 1.
  Level goes to 0xbf800000 at 0x1c653b. **[V]**
- So the deciding values are the inputs of the velocity/limit expressions:
  the `I1` block words DM(I1+36), DM(I1+10), and the limit inputs
  (DM(I1+13/14), F4 chain into FUN_1c06ba) are about 0 in bad and large in
  clean at the init call. The earlier "guard skipped" guess is **[C]**
  wrong: the guard byte is 1 in both runs.
- Model counters (`DIGI_DSP_MODEL_LOG`, default tiers): at equal absolute
  frames the two runs are identical up to frame 1236; `first` reports
  differences from 1237 only in `interpreted`/`block_instructions`/
  `idle_skips`/`gated` (follows from the different work). irq vector 22
  fires on 2 of every 3 frames with the same phase in both runs (period 3),
  not tied to the press; `irq_deferred`, `mmr_exits`, `traps`, `code_mismatch`
  are 0 or equal everywhere. No model-gap counter differs. **[V]**
- Cells 0x241d1c/0x241d24 (value 0x4c0d250b or 0) toggle 3 frames on / 3
  off, the same in both runs: this is the 6-frame beat seen in bad/clean
  (bad press = one frame before the cell switches on). Meaning unknown.
  **[O]**

## Open

- **[O]** Which memory the `I1` block is and why DM(I1+36)/DM(I1+10) (and
  the limit inputs) are ~0 in bad at the init call, and the link to the
  falling bus sine at 0x257ee8. Not yet shown to be an emulator
  semantics error: no instruction with wrong operands identified.
- **[O]** Next instrument: dump registers and the `I1` block at the init
  call. `SHARC_LIVE_AOT_CAPTURE_PC=0x1c54c7` already writes the canonical
  state at a block entry, but only for the first <=16 calls; add
  `SHARC_LIVE_AOT_CAPTURE_SKIP_ICOUNT` (start capturing at icount N, e.g.
  825.3M bad / 826.0M clean, call entry about 400 instructions before the
  0x1c55a6 store) so the pre-state (all DM, R4 = record, R8 = I1) of the
  bad and clean call can be diffed. Then walk the differing words back with
  `SHARC_WWATCH` (<=64 words per run) to their writer PC. A PC-triggered
  register dump in the interpreter (`SHARC_PCDUMP=pc,..:icount_lo-icount_hi`)
  would give the intermediate F0/F1/F2/F10 at 0x1c5566 directly.

## Tools

- `DIGI_DSPI_LOG` with `tools/dspi_log.py`: raw DSPI frames.
- `DIGI_DSP_WATCH` / `DIGI_DSP_WATCH_OUT`: CSV of `frame,pc,busy,pc_at_rx` and
  watched words after each frame.
- `SHARC_WWATCH` / `SHARC_WWATCH_OUT`: writer PC per store. Run interpreter
  only (`SHARC_AOT=0 SHARC_FAST_REGIONS= SHARC_BLOCKTRANS=`) for exact PCs.
- `DIGI_DSP_MODEL_LOG=path.csv` with `tools/dsp_frames.py`: per-frame deltas of
  tier and model counters, including interrupts taken (`irq_taken`,
  `irq_vectors` as `bit:count`, bit = IRPTL/IMASK bit number). `dsp_frames.py`
  has `diff`, `first` and `summary`, and also reads the watch CSV.
- `SHARC_LIVE_AOT_CAPTURE_PC` / `_DIR` / `_LIMIT`: raw generated-block call
  snapshots.
- `DIGI_DSP_RX_PHASE`, `DIGI_DSP_PERIOD`: delivery-phase and period switches.

## Reproduce

- Scenarios in `native/coupled/scenarios.json`: `trig1-taps`,
  `trig1-tap-release`. Do not tap first: the ready snapshot has the SLC dialog
  open.
- Press-frame scan template: no tap at 0-1M; Trig 1 down at 60M, up at 100M;
  second Trig 1 down at 110M +/- k x 100k, up 40M later; about 1 s of run. Run
  it with `uv run python tools/coupled.py scenario ...`, or with coupled-run
  `--scenarios FILE --scenario NAME --wav`.
- 1 s write-watch pair (needs a `fine1s.json` with f00/f01 at `seconds` 0.9;
  scratch copy in the session scratchpad `taps/t1/`):
  `SHARC_AOT=0 SHARC_FAST_REGIONS= SHARC_BLOCKTRANS= SHARC_WWATCH=0x241704,0x241514,0x24151c,0x241520,...
  SHARC_WWATCH_OUT=x.csv uv run python tools/coupled.py scenario f00 --no-build --out x.json -- --scenarios fine1s.json`
  (about 3 min; max 64 words per run). Model log pair: same without the
  interpreter switches plus `DIGI_DSP_MODEL_LOG=m.csv`; compare absolute
  frames with `tools/dsp_frames.py first A B --align 1000 1000 --after 400`.

