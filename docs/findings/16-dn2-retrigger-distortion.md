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

## Task 2: root cause (register dump)

Instrument: `SHARC_PCDUMP=pc,..` `SHARC_PCDUMP_ICOUNT=lo:hi` `SHARC_PCDUMP_LIMIT=n`
`SHARC_PCDUMP_OUT=csv` (87 columns, rows align; built without recompiling
sharc-gen, no edits needed). Interpreter-only.

- At FUN_1c54c7 entry R4 = 0x2414e0 (record), R8 = I1 = 0x2556ec (group 2;
  0x2554b8 for group 1). **[V]**
- At 0x1c550a (after `R0 = DM(I1+36)` at 0x1c5508): F0 = +inf (0x7f800000) in
  clean, 0 in bad. DM(I1+36) = 0x25577c. Velocity = clamp(inf) = 127; bad
  gives 0, so limit (via F4 = inf in clean) is 0 too. **[V]**
- SHARC_WWATCH 0x255710:7,0x255750:6,0x255774:6: words 0x255714/24/74/7c/84
  toggle +inf <-> 0 every ~2.0M instr (3 frames on, 3 off), identical in both
  runs and independent of the press; writer PCs 0x1c2a21/0x1c2a29/0x1c2aa9/
  0x1c2aae/0x1c2ab1. The bad press hits the 0 phase, the clean one the inf
  phase. This is the 6-frame beat. **[V]**
- Those writes are the PEy (SIMD companion) stores of the block 0x1c2a93..
  0x1c2ab3 (MODE1 PEYEN bit 0x200000 set): `F2 = float_by(F2, F12)` etc. R12
  = 0xfffffff1 (-15, correct) but the PEy scale S12 holds a stale float
  (0xbdacebc5 negative in the 0 phase, 0x3dc1250a positive in the inf phase;
  a bus-sine-like sample), so FLOAT BY S12 underflows to 0 or overflows to
  +inf. Dump: `SHARC_PCDUMP=0x1c2aa2,.. ICOUNT=824004300:824004500` (0 phase)
  and `826004300:826004500` (inf phase). **[V]**
- Cause: `R12 = 0xfff1` at 0x1c2a1d (Type 17b, executed with PEYEN set at
  0x1c2a11..0x1c2a2e). In SIMD mode Type 17 also writes the complementary
  register (S12) with the same data (SHARC+ Core Programming Reference,
  Type 17b, ch.15, extraction page 398: "provides parallel writes for the X
  and Y processing elements. The Y element uses the complementary Cureg").
  `tools/sharc_core/forms_move.py::_type_17a` (name 17a and 17b) writes only
  the explicit ureg: no companion write. [C] The 0x1c29f2 remark was wrong: it precedes the PEYEN set at 0x1c2a11. The Python core and every tier
  generated or differentially tested against it share the omission. **[V]**
  for 17b at 0x1c2a1d; **[O]** whether other 17a/17b sites run under SIMD.
- Fix location: `_type_17a` in tools/sharc_core/forms_move.py: when
  `_simd_active(state)` and the ureg has a complementary Cureg
  (`_cureg_code`), also write the Y element's register (S0..S15 for R0..R15)
  with the same value; then regenerate sharc-gen and update the golden hashes
  (expect the DN2 retrigger result to change). Fast tier lowering of 17b
  (`native/sharc-fast-cl` lower::sysops) must get the same companion write.
  **[O]** not applied here.

## Fix and verification (Task 3)

Corrections from the independent check **[C]**: the load is Type 17b
(opcode 0x0f8cfff1) at 0x1c2a1d, between MODE1 set 0x200000 (0x1c2a11) and
clear (0x1c2a2e) (PEYEN from code order); the manual (Type 17a/17b SIMD text,
extraction pp.396-398) gives the parallel X/Y write, but is silent on data16
sign extension (inferred from the firmware use, -15); the fast tier declines
regions entered with MODE1 bits 21-23, so it never ran 17a/b under PEYEN.

Fix: `tools/sharc_core/forms_move.py::_type_17a` also writes the Cureg under
`_simd_active`; test `test_type17_immediate_load_writes_cureg_only_under_peyen`
in tests/test_sharc_stack_registers.py. **[V]** A static scan found 57 Type 17
R/S loads inside PEYEN spans in DN2 1.11 and 19 in DT2 1.16 (e.g. DN2
0x1c1b5d, 0x1c209a, 0x1c248c, 0x1c2593, 0x1c4693): all were wrong before.

Regeneration (backup `out/native/dn2-audio/gen.pre-type17`, never commit):
`uv run python tools/sharc_transpile.py --strict --out out/native/dn2-audio/gen
--work out/native/transpile-work-genbatch` (5 s) then `uv run python
tools/sharc_rsgen.py dn2-1.11 --coverage $I/cov6.txt --entries $I/ent9.txt
--transitions $I/trans9.txt --model-safe --explicit-memory-model 0 --chain
--exclude 0xb88a49:0xb88abc --unknown-fallbacks 0x1c253f --work
out/native/transpile-work-genbatch --out out/native/dn2-audio/gen`, with
`I=out/native/gen-provenance-20261003/inputs` (94 s). Same 1751 blocks and
block starts; 773 regions (was 772), 259668 variants (was 258585).
PROVENANCE.txt kept (not updated). `uv run python tools/coupled.py build`
(3 m 25 s) and `tools/native_wasm.sh` (a Python script; 3 m 57 s).

Results after the fix **[V]**:

- Fine scan f00..f24 at six DSP periods (150 runs, an22): 0 bad; tone 94.3-95.2%.
- Wide scan P=1202..~1342 (140 runs, an24): no 'B'.
- Scenario hashes: trig1-taps, trig1-tap-release and exactness-100m changed
  (expect_sha256 updated in native/coupled/scenarios.json, because the DSP
  output changed on purpose); trig1-noqa-40m unchanged (a50586e8..). New:
  trig1-taps 1ca0ae84.., tap-release aa1dbd9e.., exactness-100m 3b600f6d... All
  taps in the three changed scenarios are clean (tone 94.7-95.1%).
- tests/test_sharc_golden.py: all six hashes unchanged (they use DT2 1.16 and
  coverage/run paths that do not hit this case): no `--update`.
- `pytest -k "sharc and not slow"`: 45 failed (all the stale native dylib
  tests, same count as before), 1703 passed: no new failures.
- Node wasm parity trig1-tap-release: aa1dbd9e.. MATCH with native.
- Reproduce scans: `taps/sweep.sh P` (fine, per period) + `an22.py`;
  `taps/wide.sh P...` + `an24.py` (scratchpad scripts, not in the repo).

Pinned values in `coupled_ready_exactness` (native/coupled/src/lib.rs),
updated for the fix **[V]** (gate passes; run as tools/native_pgo.py does,
manifest packages/desktop/src-tauri, absolute DIGI_COUPLED_FIXTURES):
CF digest `fbace0f0..` -> `bc82b1996d75..0507` (the DSPI RX buffer at CF
0x800053a4 is filled with DSP replies; replies differ in 1650 of 1652 frames
of f06 before/after, TX identical, CF instruction counts identical), PCM
`38d2a322..` -> `3b600f6d..7f267`, DSP export `27152f09..` ->
`06980a01f22363a1ca0d14d6dfb107eaeb2c1ade96df5a0da1a24fe6c84dd4fb`; 72,896
samples unchanged.

## Open

- **[O]** DT2 1.16 core regeneration and other DN2 PEYEN sites were not
  re-audited for audible effect; fast-tier 17b under PEYEN is declined by
  construction.
- (done, see Task 2) Next instrument was: dump registers and the `I1` block at the init
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
  `trig1-tap-release`. With the 2026-10-06 snapshot (the default) no NO press
  is needed before Trig 1; the NO prelude in older scenarios is harmless on the
  main screen. The 2026-10-03 snapshot has the SLC dialog open, so it needs the
  NO press first and `DIGI_CARD_IDENTITY=mlc4g`.
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


## Reference state rebuilt from cold start (2026-10-07)

**[C]** The DN2 hashes above (`3b600f6d..`, `aa1dbd9e..`, `1ca0ae84..`, CF digest
`ab27c5c9..`, DSP export `c24a3040..`) belong to the old ready state, whose
DSP start state (`digi-audio-loop2-fresh.bin`) had the DC-blocker coefficient
DM `0x24f378` (c22 of the record at `0x24f320`, read by `0xb809f2`) corrupted:
the pre-ed02999 SIMD Type 17 bug left the S2 lane at -0.0 instead of -a/16.
The reference is now `mise run ready-dn2` (tools/ready_dn2.py: CF cold boot
plus a DSP cold-start state from the .syx alone). New pins: exactness-100m
`b3f5230f..`, trig1-taps `d60ad339..`, trig1-tap-release `88347621..`,
trig1-noqa-40m `2861ce03..`, CF digest `55dc699e..`, DSP export `4dc48019..`
(`native/coupled/scenarios.json`, `coupled_ready_exactness`). The retrigger
scans of this finding were made on the old state and are not repeated **[O]**.
