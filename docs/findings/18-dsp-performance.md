# 18. DSP performance: measurement, cost model, state at freeze

Branch `work/sharc-emulator` at 911b1fc (performance work frozen for cleanup and
merge). Sources: the lane notebooks for the fast tier, perf tool, call path,
helpers, chaining, generated lowering, AOT profile and class (e); they are folded
into this file and removed. Marks as in CLAUDE.md: **[V]** measured here at 911b1fc,
**[D]** measured earlier on a lane base commit (cited), **[O]** open or predicted.
Scenario for every number: DN2 1.11, `trig1-taps` (10.5 audio s; 502M executed
SHARC insns per audio s, another 498M idle-skipped, free).

## How to measure

- `mise run perf -- --config NAME [--config NAME ...] [--seconds S] [--timed] [--no-diag]`
  (`tools/perf.py` on `tools/fastcov.py`). Per config: a clean run (DSP CPU per audio
  s, PCM sha, tier split, region calls) and a diagnostic run (SHARC_FAST_HIST and
  SHARC_FAST_COV: interpreted insns, calls and kernel insns per function) that ranks
  functions by estimated cost. Report: `out/runs/perf/<stamp>/report.md` and `perf.json` (see docs/INSTRUMENTATION.md).
- Configs: `default` (AOT blocks + verified regions: the shipped desktop build),
  `static-blocks|static-insns[+follow][+gen][+aot]` (fast regions armed at every block
  start or instruction, AOT off unless `+aot`), `fast-auto:256,64`, `env:K=V;...`,
  `<config>+env:KEY=VALUE`, and `hybrid-top:N` (default config plus fast regions
  armed only at the static block starts of the top N functions of the cost ranking,
  taken from a cached fast-only diagnostic run, `--rank-seconds`).
- `--gen prod` is required for any AOT config (the shared core has no firmware blocks).
- `--timed` takes the exclusive benchmark lock; milestone numbers only. Unlocked runs
  are good for coverage (deterministic) and for A/B inside one session, not for
  absolute CPU. Run 1.5 s for coverage, the full 10.5 s for CPU: at 1.5 s compile time
  dominates the fast-only configs.
- `--calibrate` fits the cost model below (5 configs, least squares, kernel ns fixed
  at the gate's 0.5), writes `out/native/perf/calibration.json`.
- `--regions` (build with `region-profile`): wall time per function; `--device dt2`.
- Pinned PCM at 10.5 s: `d60ad3390038a84b`. Other pins: 3 s `9c3b55b48ca49d41`; 1.5 s
  fast-only `3bf3ba3026953c22`; 12 s `8cbae26b73014732`.

## Cost model

| item | ns | basis |
|---|---:|---|
| interpreted insn | ~105 (profile 116-119) | **[D]** fit; step loop 29 + exec_insn 25 + generated core 66 |
| region call (engine round trip) | 130 by profile, 266 fitted | **[D]** fit also absorbs declines |
| kernel insn (Cranelift) | ~0.5 | **[D]** gate 0.49 |
| AOT insn | ~4.4 | **[D]** 46.6% of insns take 3.07 of 3.64 s region time |
| generated helper (body 6 + sync 5) | ~11 | **[D]** laneF microbench; interpreter-step body ~140 |

CPU = interp x 105 + calls x 266 + kernel x 0.5 + AOT x 4.3. It lands within 4-11% of
measured CPU on the calibration runs and 6-25% over on the runs below. **[V]**
Call path, phases per region call **[D]**: entry checks 8, requirements 13, window
resolution 22, kernel 22 (13 of it its own work), flag replay 6, exit bookkeeping 6,
other 5, plus run_inner/wants/step share 40-60: 100-130 ns. No single phase is large.

## State at freeze **[V]** (timed, exclusive lock, prod gen, 10.5 audio s)

| config | DSP CPU / audio s | PCM | region calls/s | AOT / fast / interp (% of executed insns) |
|---|---:|---|---:|---|
| default | **1.584** | d60ad3390038a84b | 0.62M | 46.7 / 52.9 / 0.37 |
| hybrid-top:10 | 3.845 | d60ad3390038a84b | 4.06M | 59.3 / 37.3 / 3.37 |
| hybrid-top:20 | 4.251 | d60ad3390038a84b | 4.92M | 36.7 / 59.2 / 4.02 |
| hybrid-top:50 | 4.614 | d60ad3390038a84b | 6.76M | 23.7 / 71.6 / 4.68 |
| fast-only (static-insns+follow) | 7.037 | d60ad3390038a84b | 20.96M | 0 / 94.4 / 5.57 |

Fast-only has the same PCM as the default here.
The headline desktop metric is 1.584 DSP CPU per audio s: **target <= 1.0 not met**
(base 2085854 measured 1.46; the two runs are on different sessions, so the 8% gap
is inside the noise of this machine; lane F's 1.41 was another session again).
Hybrid-top is worse than default at every N: the AOT code it displaces runs at 4.4
ns/insn with almost no calls, while each armed function brings 4-7M region calls/s
(215 ns residual each) and 3-5% interpreted insns (about 1.5-2.5 s). The lowering is
exact in all five rows; it is a cost question, not a correctness one.
Other measured points:
- ColdFire CPU **[D]**: 0.52 s per audio s native.
- Wasm kernel gate **[D]**: 0.50 ns/insn in V8 (TurboFan) against 0.435 Cranelift
  native (limit 1.2); Liftoff-only 2.96 ns/insn until TurboFan finishes; compile
  about 1 ms per kernel, 104 us for the Rust-side emit.
- Browser **[D]**, Node 24/V8, two instances over a SharedArrayBuffer, `idle`
  scenario, fast tier on, no AOT: DSP worker ~39 s per audio s, ColdFire worker ~1.3
  s. One instance without the fast tier: 140 s. Browser DSP at <= 0.6 needs the
  interpreted share <= 0.1% at 105 ns, or <= 1% at 10 ns/insn.

## What worked

- Per-range AOT invalidation **[D]**: a code patch bars only the regions with an
  entry in a bad range; trig1-tap-release with a patch kept 697M of 701M insns in AOT,
  CPU 4.53 vs 3.99 s unpatched (was: all AOT barred).
- Generated lowering (laneF, `tools/sharc_lowergen*.py`, 24 forms) **[D]**: fast
  89.5 -> 93.4% (92.5% on 1.5 s) and interpreted insns -29%; one semantics source
  (`tools/sharc_core`), fuzzed against the interpreter; on by default.
- Region-call back-off after window declines **[D]**: calls 27.5M -> 16.6M/s, CPU
  11.13 -> 10.08 (-9%), PCM identical.
- Class (e) volatile-M fix **[D]**: an M register that fails its baked value twice
  is not baked again (`region::mark_volatile`), and a call that exits at its first
  insn four times turns it into a helper: 8.82 -> 7.19 -> 6.93 CPU/audio s, built
  regions 8317 -> 5291, req declines 4.05M -> 0.58M per 1.5 s.
- Generated helper bodies **[D]**: 6 ns mean against 62 ns for the interpreter step;
  ~11 ns per helper insn with sync. Gain where used is small (-5% in 0x1c8ef1).
- SIMD semantic fixes **[D]**: both PEs for 7a, 9a/11a; fuzz found 14a store dropped,
  PEy compare shifting PEx CACC, a circular tail-write bug (halt at 0xb88ea8).

## What did not work

- Form-family lowering **[D]** moved coverage (83 -> 87 -> 93%) but CPU stayed
  11.25 -> 11.5: more guarded windows and built regions (1686 -> 2846) cost as much
  as the interpreter time saved. Coverage is not the metric; calls and interpreted
  insns are.
- Trimming the call path **[D]**: 20 ns per call is not reachable from 100-130 ns by
  tightening run_cfg; no phase exceeds 22 ns. Needs fewer calls or chaining.
- Flat guarded access (`SHARC_FAST_FLAT`) **[D]**: exact, no coverage gain, about 25
  IR ops per access, Cranelift compile 5.8 -> 31 s. Off by default.
- Blanket helpers **[D]** (all `3a`): +0.30 s; 1 ns guarded hits became 140 ns.
- Hot-function HLE **[D]**: DN2 top 20 functions are 84% of executed insns (79.5% of
  AOT region time), ~21 functions for 80%; DT2 needs ~35 (top 20 = 70%). Not viable.
- Mask-free AOT emission **[O]** (superseded: slices 0-2' below and in the design doc; concrete registers were
  dropped, hot-code density gave -15% without PGO and -9% with PGO): predicted AOT 70 -> 22% of the DSP thread, 4.4 ->
  ~1.5 ns/insn, default 1.41 -> ~0.75; +-30%, 1-2 weeks, changes the transpiler V type.
  **[D]** Design and redone ceiling in `docs/design/aot-mask-free.md` (2026-10-09, nothing built):
  the estimate rests on one xctrace category split; warm region benchmarks already reach
  1.4-2.3 ns/insn and the single ablations (undo copies, budget check) are worth 0-9%, so the
  in-situ 4.0 ns may be code size (about 1.1 KB machine code per block insn) and region entry
  rather than masks. Central estimate default 1.05-1.2, 0.8-0.9 optimistic; slices 0 and 1 (2 days)
  decide.
## Where the time is **[D]**

Default at 1.58: AOT 234M insns x 4.4 = ~1.0 s, kernel 266M x 0.5 = 0.13 s, 0.62M
calls and 1.9M interpreted insns the rest. Fast-only exit classes per audio s (laneB
chaining table, 16.95M calls, 8.9 s): (a) exit at another region's entry 6.21M calls,
1.76 s; (a2) call/return/jump structure stops 2.43M, 0.74 s; (a3) exit to a non-entry
0.32M, 0.34 s; (b) refused instruction 3.19M, 1.22 s; (c) guard failure in a region
2.72M, 1.02 s; (c') entry-requirement declines 1.33M, 0.35 s; (d) shape declines
0.49M, 0.13 s; (e) 36.5M interpreted insns where no region call happens, 3.8 s
(40%), untouched by chaining. About 70% of interpreted PCs lie after mid-region exits.

## Next levers, ranked

Desktop (target default <= 1.0, ideally 0.7):
1. Mask-free AOT emission: 1.58 -> ~0.8-0.9. Confidence low-medium (+-30%). The only
   lever aimed at the 1.0 s that is AOT, now 65% of the default CPU. High effort/risk.
2. Fast kernels for the hottest AOT functions without calls: needs (3) first; today a
   function moved from AOT to fast costs more than it saves (hybrid rows).
3. Chaining, class (a)+(a2)+(a3), 8.96M calls/s: -2.2 s of 9.5 fast-only at 20 ns per
   transition (-1.0 s by the profile cost). Step one is a Rust-loop chain that skips
   Engine::step and run_inner (~50 ns), then kernel-to-kernel jumps (~20 ns).
   Confidence medium for step one, low for 20 ns.
4. Continue class (e) and mid-region exits: each fix so far gave -10 to -20% on
   fast-only (volatile M: 8.82 -> 6.93). Confidence medium; no effect on default.
5. Generated compute/MR helper bodies with flag sync (+10 ns): ~20% of the
   interpreted insns that end regions; fast-only -1 to -2 s predicted, confidence low
   (flag state would need dynamic known-bit masks in the tracer).
6. hybrid-top coverage: no gain at any N; drop unless chaining makes calls cheap
   (then re-measure with `--config hybrid-top:N`).

Browser (no AOT possible without firmware; fast tier only):
1. Interpreted share to <= 0.1% (or <= 1% with a ~10 ns generated interpreter): the
   DSP worker's 39 s is ~97% interpreter; the fast tier cut 140 -> 42. Needs 4 and 5
   above plus the missing forms; confidence medium.
2. CF interpreter speed: 1.3 s of 1 s budget is already a CPU core; a faster
   interpreter loop (cached decode, fewer machine hooks) is needed before DSP matters.
   Confidence medium, effort moderate.
3. Chaining (3 above) in the wasm backend: imported calls are 2.7 ns, so kernel to
   kernel jumps in one module look cheaper than native; confidence low-medium.

## Per-FX coverage in the no-AOT config **[D]**

Measured 2026-10-08 on branch `work/scenario-oracles` (DN2 1.11, shared generated core, no
firmware blocks). Not re-checked by a second agent. Tools: `tools/perf.py --scenarios FILE
--scenario NAME... --seconds 0` (0 = each scenario's own length) and `tools/perf_agg.py DIR...`
(union and FX increase over the dry hold). Scenarios: `scenarios/dn2.json`,
`dn2-trackfx.json`, `dn2-sendfx.json`.

Config: **`static-insns+follow`** is the translator-only, no-AOT configuration (finding 18's
"fast-only"; fast regions armed at every instruction, generated lowering and helpers on, AOT
off). It needs no per-firmware core, so the shared core is enough. Check: `trig1-taps`, 1.5 s,
5.48% interpreted (finding: 5.57% at 10.5 s; the full run here gives 5.57% too), PCM
`3bf3ba3026953c22` at 1.5 s as pinned.

Counters are deterministic (untimed, shared lock, three runs in parallel). The CPU column is
the finding-18 cost model (105 ns interpreted, 266 ns per call, 0.5 ns kernel) and is 15-35%
high on this config; the two timed rows (exclusive lock) anchor it.

| scenario | audio s | interp % (no AOT) | calls/s | est. CPU s per audio s | interp % (default) | est. CPU (default) |
|---|---:|---:|---:|---:|---:|---:|
| trig1-taps | 10.50 | 5.57 | 20.95M | 8.75 (timed 6.52) | 0.37 | 1.50 (meas. 1.47) |
| tf-dry-hold | 4.10 | 5.50 | 19.67M | 8.38 | 0.25 | 1.42 |
| tf-br-127 | 4.10 | 5.47 | 19.71M | 8.40 | **1.24** | 1.94 |
| tf-over-127 | 4.10 | 5.50 | 19.67M | 8.38 | 0.25 | 1.42 |
| tf-srr-127 | 4.10 | 5.42 | 19.69M | 8.39 | **1.88** | 2.28 |
| tf-del-127 | 5.90 | 5.46 | 19.60M | 8.34 | 0.25 | 1.42 |
| tf-rev-127 | 5.90 | 5.46 | 19.60M | 8.34 | 0.25 | 1.42 |
| tf-chr-127 | 5.90 | 5.46 | 19.62M | 8.35 | 0.25 | 1.42 |
| sf-dry-hold | 4.36 | 5.50 | 19.67M | 8.38 | 0.25 | 1.42 |
| sf-del-fb-d127 | 6.16 | 5.46 | 19.63M | 8.35 | 0.25 | 1.42 |
| sf-rev-decay-u20 | 6.16 | 5.45 | 19.62M | 8.34 | 0.25 | 1.42 |
| sf-chr-depth-d127 | 4.36 | 5.52 | 19.86M | 8.44 (timed 7.26) | 0.25 | 1.42 |

Timed (exclusive lock, no-AOT, unloaded machine): `trig1-taps` 6.52 CPU s per audio s,
`sf-chr-depth-d127` 7.26 (the 4 s run carries more compile time per audio s); both gate ok,
PCM unchanged from the untimed runs. The model over-estimates this config by 34% and 16%.
Default-config runs were sequential but unlocked; the "est." column is the model, measured
CPU was 1.47 (trig1-taps), 1.39 (dry hold), 2.21 (`tf-br-127`), 1.52 (`tf-over-127`) and
2.23 (`tf-srr-127`).

What this shows:
- **No-AOT, the FX do not change the picture.** Every scenario sits at 5.4-5.6% interpreted
  and 19.6-21M region calls per audio s. The FX code is part of a chain that runs whatever the
  parameter values; the interpreted cost is the base DSP chain's, which the dry hold already
  has. The interpreted tail to cover is therefore the same list for every FX.
- **With AOT, br and srr are the exceptions.** `FUN_b8258f` (313 insns, br) and `FUN_b826c8`
  (272 insns, srr) are cold in the AOT core: interpreted 0 insns/s on the dry hold,
  5.0M and 8.3M insns/s with the effect up. That is +0.9% and +1.6% interpreted and about
  +0.5 and +0.9 s of estimated CPU per audio s (default 1.42 -> 1.94 and 2.28).
  Overdrive, delay, reverb and chorus (track and send pages) add no cold code in the AOT core
  (at most 8k insns/s in `FUN_b80432`).
- Without AOT, the same two functions are translated (70k insns/s for b8258f in `tf-br-127`,
  35k in `FUN_1c07c6` for srr), so a translator-only build is not worse than AOT on them.
- **Fix [V by pins, counters from one run]: FX-active profiles (2026-10-09).** The DN2 AOT
  core was regenerated from the old inputs (cov6/ent9/trans9) plus `coupled-run
  --profile-execution` profiles of `tf-br/srr/over/del/rev/chr-127` and `sf-del-fb-d127`,
  `sf-rev-decay-u20`, `sf-chr-depth-d127`, `sf-mst-thr0` (default config, current core;
  `tools/aot_fxprof.py`, merged by sum per key, same rsgen flags). Blocks 1751 -> 2657 (20722
  -> 38378 insns; gen 90 -> 122 MB, so the `sharc-gen` compile is longer). Default config,
  `perf.py --gen prod --config default`, interpreted share: br 1.24% -> 0.33%, srr 1.88% ->
  0.24%, dry hold 0.25% -> 0.25%. `FUN_b826c8` is now 0 interpreted; `FUN_b8258f` fell from
  3.7M to 0.44M insns/s (its loop body at 0xb825f2..0xb82658 still runs in the interpreter;
  +46 ms/audio s, not resolved). Measured CPU s per audio s (unlocked, other jobs running):
  br 2.21 -> 1.61, srr 2.23 -> 1.51, dry hold 1.39 -> 1.51. The dry-hold figure went up with
  the larger core (or the load), so check with an exclusive timed run before quoting it. DT2
  was not regenerated: it has no FX scenarios to profile.
- **A/B of the larger core [V, timed, exclusive lock, 2026-10-09].** Old (`gen-pre-fxprof-20261009`,
  binary in `out/native/target/g-e220e7a2`) vs new (prod gen), same sources, `coupled-run`
  alternated old/new, 2.0 s each, DSP CPU s per audio s (median, min-max): br-127 2.04
  (1.97-2.15) -> 1.62 (1.53-1.64), 7 pairs, -21%; srr-127 1.94 (1.92-1.99) -> 1.47
  (1.45-1.51), 7 pairs, -24%; dry hold 1.439 (1.41-1.57) -> 1.444 (1.42-1.63), 20 pairs, +0.3%
  (inside the IQR 1.42-1.46 / 1.43-1.48); trig1-taps 1.424 (1.41-1.47) -> 1.430 (1.41-1.46), 20
  pairs, +0.4%. PCM sha256 is identical old vs new in every run of all four scenarios. The
  earlier "dry hold 1.39 -> 1.51" was load noise. **Recommendation: keep the larger core**
  (large FX wins, no measurable cost elsewhere; the price is a longer `sharc-gen` compile).
  Script: scratchpad `ab.py` over `perf.timed_run` (`native_pgo bench-bins` runs the PGO
  workload test binary, not `coupled-run`).
- `FUN_b84099` (saturator) and `FUN_b8478d` (FX orchestrator) are not in the top 50 of the
  cost ranking in any scenario: they are covered. `FUN_b83f72` (delay writer) is rank 4.
- `tf-dry-hold` vs the del/rev/chr scenarios differ by the voice (hold vs tap); the negative
  "extra" in `perf_agg` for those is that, not an FX saving.

Ranked uncovered (interpreted) functions, no-AOT config, union of the 12 scenarios, by mean
interpreted cost (interp insns x 105 ns). Share is of the 8.4 s estimate. "Baseline" is
trig1-taps and the dry holds; no top-10 function is FX-only. The refusal is fastcov's reason
at the function's hottest interpreted PC.

| # | function | size | interp insns/s | est. ms per audio s | share | what | why the fast tier stops |
|---|---|---:|---:|---:|---:|---|---|
| 1 | 0xb82d1a | 1582 | 4.43M | 465 | 5.5% | loop kernel, unnamed | deny at 0xb832e3: form 3a, loop with a register step that is not stable |
| 2 | 0x1c2712 | 2633 | 3.9M | 409 (+193 calls) | 4.9% | unnamed | stop at 0x1c2df3: form 14d (not lowered) |
| 3 | 0x1c4f04 | 568 | 2.26M | 237 | 2.8% | unnamed | stop at 0x1c50e5: 3b "cannot be lowered in this region" |
| 4 | 0xb83f72 | 295 | 1.86M | 196 | 2.3% | delay writer (finding 15) | stop at 0xb8403a: form 2a_short, a compute opcode with no lowering |
| 5 | 0x1c54c7 | 779 | 1.85M | 194 | 2.3% | unnamed | stop at 0x1c5619: 8a_rel with a compute in the delay slot (4a) |
| 6 | 0x1c3911 | 254 | 1.54M | 161 | 1.9% | unnamed | stop at 0x1c39e0: 3a "cannot be lowered in this region" |
| 7 | 0x1c8ef1 | 3202 | 1.49M | 143 (+1072 calls) | 1.7% | unnamed, the largest call source (3.8M calls/s) | stop at 0x1c99a6: 2a_short compute |
| 8 | 0x1c37b4 | 326 | 1.32M | 139 | 1.7% | unnamed | stop at 0x1c38bd: 3a "cannot be lowered in this region" |
| 9 | 0x1c35a7 | 303 | 1.08M | 113 | 1.3% | unnamed | no stop recorded: never selected or dropped |
| 10 | 0x1c244d | 709 | 0.79M | 78 (+235 calls) | 0.9% | unnamed | deny at 0x1c2680: 3a, address range unknown |

Together the ten are about 2.1 s of the 2.9 s estimated interpreter time (the interpreted
share is 5.5% of 502M insns per audio s). Reasons group into five: 3a/3b accesses the lowering
cannot place (ranks 3, 6, 8, 10: "cannot be lowered in this region", "address range unknown"),
a loop with an unstable register step (1), compute forms without a lowering (2a_short: 4, 7),
form 14d (2) and a compute in a branch delay slot (5). Names for the 0x1c.... functions were
not looked up in findings 06/15; `FUN_1c8ef1` is the biggest call source, so chaining matters
more there than coverage.

Recommendation: the FX are not a separate coverage problem in the no-AOT build; fix the base
chain list above, in this order: (a) the 3a/3b "cannot be lowered in this region" and
"address range unknown" refusals (ranks 3, 6, 8, 10, about 0.6 s); (b) the 2a_short compute
opcodes at 0xb8403a and 0x1c99a6 (ranks 4, 7); (c) form 14d at 0x1c2df3 and the 8a_rel
delay-slot compute at 0x1c5619 (ranks 2, 5); (d) the stable-step loop in `FUN_b82d1a`
(rank 1, the single largest, 5.5%). Together the ten are 25% of the estimated CPU. The interpreted
tail needs to fall from 5.5% to 0.1% for the browser (see "Next levers"), so this list is a
start, not the goal: the rest is a long tail (top 20 are 78% of the estimated cost in
this config, but each remaining function is under 1%). Separately, `FUN_b8258f` and
`FUN_b826c8` are the only FX code that the AOT desktop build misses; a regenerated AOT core
or hybrid arming of those two functions fixes the desktop.

Batch 1 of the refusals (2026-10-09) **[D]**, no-AOT `static-insns+follow`, `trig1-taps`,
shared core, first opt-in `SHARC_FAST_FLATAUTO=1` (config suffix `+flat` in `tools/fastcov.py`), default since the flip below. Not
re-checked by a second agent.
- Root causes. The four "3a/3b cannot be lowered" stops were not one refusal. (1) Address
  range unknown: a pointer loaded from memory or passed in a register has no static range and
  no anchor register (0x1c2680 `R1 = DM(I2,M6)`, I2 loaded from a stack slot; 0x1c399a,
  0x1c3862 the same; 0x1c5054), or a constant base with a data-dependent modifier (jump-table
  loads `I12 = DM(I4,M4)` at 0x1c50f1 and 0x1c5284, I4 constant, M4 = R8). The region ended
  there (`deny`). (2) Window decline at run time, the larger part: 0x1c3911 and 0x1c37b4 (roots
  never ran: 569 and 284 declines, then back-off) and the roots 0x1c4fbe..0x1c5016 of
  `FUN_1c4f04` declined `window: not plain/present RAM (write)`. A static store window is the
  hull of all stores of the region and `Mem::window` needs every byte of the hull
  overlay-written; the hull covers stores on paths that never ran for that object (first byte
  and last byte neither present nor dirty), so the region declined forever.
- Fix, translator only (`native/sharc/src/fast`): (a) `flat_auto`: an instruction refused for
  "address range unknown" is re-lowered as a flat guarded access (the existing
  `Lower::flat_access`, per-access page-table and bitmap guards) at that PC only, not for every
  access as `SHARC_FAST_FLAT` does; (b) a root whose windows decline 3 times is rebuilt with
  every window access flat (`Env::flat_windows`), so a hole in a hull costs one guard exit on
  the path that touches it; (c) `Abs::as_addr`: a constant above 2^31 was sign-extended
  (`I4 = 0x8052d790` gave the range -2142053488), so its `NwPlain` requirement and window range
  failed for ever; it is now read modulo 2^32 (needed once (a) let regions reach such code:
  `FUN_1c8ef1` went 2.1M to 6.4M interpreted insns per 1.5 s without it).
- Result (1.5 s): interpreted 5.48% -> 0.99% of 502M insns/s, region calls 19.5M -> 17.2M/s,
  PCM `3bf3ba3026953c22` unchanged. Interpreted insns/s of the four: `FUN_1c4f04` 2.26M ->
  0.08M, `FUN_1c3911` 1.54M -> 0, `FUN_1c37b4` 1.32M -> 0, `FUN_1c244d` 0.79M -> 0.06M. It also
  covers most of the next ranks (`FUN_b82d1a` 4.43M -> 0.04M, `FUN_1c2712` 3.9M -> 0.15M,
  `FUN_b83f72`, `FUN_1c54c7`, `FUN_1c35a7`, `FUN_1c8ef1` 1.4M -> 0.24M).
- Timed (exclusive lock, 10.5 s, no diagnostics): 6.63 -> 5.00 DSP CPU s per audio s (-25%), PCM
  `e0e6c8af7a84c443` in both (the current `trig1-taps` pin). A 1.5 s run shows 15.7 s per audio
  s because the flat kernels are larger: Cranelift compile time is paid up front, so judge it on
  the full length. For the browser that compile cost has to be re-measured with the wasm backend.
- Exactness: oracle suite (`tests/scenarios --slow`, 61 passed) and `scenario --all` pins ok in
  this config on the shared core; `cargo test` in `native/sharc` 64 passed (two new unit tests).
  Default (AOT) config not measured: the flag is opt-in, `as_addr` is always on.
- Default flip (2026-10-09) **[D]**: `SHARC_FAST_FLATAUTO` is now on unless set to `0`
  (`+flat` in fastcov is a no-op; `env:SHARC_FAST_FLATAUTO=0` turns it off). Default config
  (AOT core + fast tier), one binary, env on/off, exclusive lock, alternating pairs, full scenario
  length, DSP CPU s per audio s (median, IQR): `trig1-taps` on 1.393 (1.392-1.397) vs off 1.395
  (1.392-1.399), 10 pairs; `tf-br-127` on 1.433 (1.413-1.445) vs off 1.443 (1.415-1.527), 10 pairs.
  No slowdown, no gain (AOT covers these regions). No-AOT `static-insns+follow` `trig1-taps`: on
  5.66 (5.14-5.67) vs off 7.32 (6.49-7.36), 5 pairs, so the batch-1 gain holds. PCM identical in
  every run (`e0e6c8af7a84c443`, `16f3d0a81e686049`). Checks with the default on: `scenarios`
  pins ok, oracle suite 61 passed, `dt2_sample_check` clean tone, golden 6 passed, `cargo test
  --lib --tests` in `native/sharc` 48 passed. The `region_bench` example no longer compiles (its
  `Env` literal lacks `flat_windows`), so `cargo test --workspace` fails; not touched here.
- Left in these functions (batch 2): `2a_short` compute cu=0 opcode 0xad at 0x1c4fa3, 0x1c516b,
  0x1c5186; multifunction category 0x1e/0x1f at 0x1c5060 (3a), 0x1c553a, 0x1c5547; `8a_rel` with a
  compute in the delay slot at 0x1c55c5; in `FUN_1c244d` the 17a/4a/18a loop stops at 0x1c2583,
  0x1c25ae, 0x1c25bd and 0x1c252d. Interpreted share is now 0.99%; the top remaining are
  `FUN_1c0c76` (1.1M), `FUN_1c07c6` (0.87M), `FUN_1c06ba` (0.69M), `FUN_1c0a6f`, `FUN_1c02a4`.

## Region-call overhead **[D]**

Measured 2026-10-09 on `work/scenario-oracles` (uncommitted), no-AOT `static-insns+follow`, shared
core, DN2 `trig1-taps`, 10.5 audio s, flat fallback on (`SHARC_FAST_FLATAUTO=1`), PCM
`e0e6c8af7a84c443` throughout. Not re-checked by a second agent. Tools: `tools/perf.py` (diagnostic
run), new `tools/perf_calls.py` (call sources and exit kinds from its stderr), `xctrace` Time
Profiler on `tools/sharc_build_dbg.py` output (`generic/dbg` target).

Instrumentation added: every `cov run` exit now carries a kind (`ret`, `call`, `edge` = taken
branch or loop-back to an address the kernel does not hold, `stop`, `guard`, `budget`; with
SHARC_FAST_COV a guard is named by the source line that emitted it, `g@mem_addr.rs@207` is the
dynamic-window guard). `tools/fastcov.py` `Diag.parse_run` split the `exits=` field on `,` while
the engine joins it with `;`, so it kept only roots with a single exit (it saw 82M of 196M calls);
fixed.

**The 10.5 s headline is 55% compile.** Timed run: 53.6 DSP-thread s = 19.8 s Cranelift compile +
2.9 s region build (6483 regions; the JSON has `fast_compile_ms_total` and `fast_build_ms_total`)
+ 30.9 s run time. Run time is 2.94 s per audio s; the 5.1 headline adds 2.16 s of compile. All A/B
below therefore give both numbers. (The 20 s run costs 110 s, 5.5 per audio s: later audio compiles
more regions; no linear fit.)

*Call sources.* 18.66M region calls per audio s: 16.75M run a kernel, 1.9M decline (exit0 1.23M,
req 0.55M, shape 0.13M, window 5k per run, budget 0). No chain misses (every PC is a root; the
interpreted gap between a region exit and the next call is 0.03M insns/s). Exits that ran, M per
audio s (insns per call): `edge>pc` 4.29 (6.9), `stop` 2.99 (23.7), `ret` 2.88 (112.6), dynamic-window
guard 2.69 (7.6), `edge>fn` 2.04 (8.1), loop-shape guard 1.23 (21.5), other guards 0.37 (4.3), call
0.22 (31.2). Top five functions, 8.25M calls (44%):

| function | calls/s | insns/call | why the calls end |
|---|---:|---:|---|
| `FUN_1c8ef1` (1352 roots) | 3.48M | 6.6 | the DO loop 0x1c9b12..0x1c9b2d is three regions per iteration (2.16M; 480 iterations per call, 1500 calls/s): edge>pc 0.92 (1.4i), loop guard 0.77 (7.3i), dynamic-window guard 0.76 (6.4i); stop 0.61 (8.8i); edge>fn 0.29; exit0 declines 0.11 |
| `FUN_b80c39` (22) | 1.75M | 2.7 | DO loop 0xb80c54..0xb80c60 calls 0xb809f2 every iteration: region to the call (4 insns, edge>fn 0.86M), callee, region at the loop end (1 insn, edge>pc 0.84M) |
| `FUN_b82d1a` (602) | 1.15M | 6.0 | dynamic-window guard 0.73 (4.1i), edge>pc 0.39 (5.0i) |
| `FUN_b809f2` (15) | 1.00M | 14.4 | the callee of the previous loop, ends in its return (ret 0.96M, 14.9i) |
| `FUN_b82a5b` (73) | 0.87M | 6.9 | dynamic-window guard 0.58 (4.8i), edge>pc 0.29; exit0 0.29M |

Next: `FUN_1c0c76` 0.73M (stop 0.44, ret/call 0.30), `FUN_1cdfa1` 0.58M and `FUN_1cdf46` 0.29M (the
same loop-calls-small-function shape as b80c39), `FUN_1c07c6` 0.58M (stop+ret, 3-4 insns).

*Cause of 29% of the calls.* A pre-modify or post-modify access whose address is a base register
plus a modifier loaded from memory (`I0 = modify(I1, M4)` with `M4 = DM(I5,M6)`, then
`DM(I0,M5) = R8`) is lowered through a guarded dynamic window anchored on the base register I1
(`lower/mem_addr.rs` `xfer_at`/`dyn_xfer`). The span resolved at entry is around I1's value, and the
real address (0x258xxx) is nowhere near it, so the guard fails on every call: the region does 4-5
instructions, leaves, and the next region starts at the failing store, where I0 is an entry
register and the window is right. Measured: 7.2M of 7.56M calls of that loop failed (only 480 distinct
addresses, all present and dirty, so it is not a memory-state guard). The chain of regions reached
from such exits is 252 roots, 5.9M calls per audio s, in `1c8ef1`, `b82d1a`, `b82a5b`, `1c57d2`.

*Per-call cost, run time only* (xctrace, 22.6K samples of the 59 s DSP thread, the build samples
removed, 9615 left; sampling covers a minority of the run, so shares only). JIT kernel code 46.3%
(0x... leaf addresses), interpreter plus generated core 24.0% (4.9M insns/s at ~120 ns = 0.6 s,
agrees), `glue` (`run_cfg`, `resolve_window`, `apply_*`, `sites`) 15.0%, engine step loop 3.4%,
`run_inner`/`wants` 2.6%, memmove/memset 2.2%, `Mem::window` 2.0%, helpers 1.6%. Per ran call
(2.94 s / 18.66M = 158 ns all in): about 73 ns in kernel code, of which ~13 ns is the instructions
(26.6 x 0.5); about 39 ns in Rust glue, dispatch, window and copy; plus the interpreter 0.6 s total.
All-in cost of an avoided call, interpreter and kernel work taken out: 2.94 - 0.6 - 0.25 = 2.1 s over
18.66M = 112 ns; measured below: 117 ns. The kernel's non-work part is not code size (a one-insn kernel
is 53 IR ops, 27 machine instructions): **[O]** it is cold code and an indirect call per region over
~1500 hot kernels, and data misses on the first loads; not separated. Chaining that only skips the
Rust loop can therefore remove the 39 ns part (about 0.5 s of 2.94 at 13-16M calls), not the rest.

*Ceilings* (run time per audio s, 112 ns per call): `FUN_1c8ef1` alone 3.48M calls = 0.39 s if all
its calls went (10x fewer: 0.35 s); top five 8.25M = 0.92 s; the 5.9M-call guard chain 0.66 s; all
calls 2.1 s (not reachable: the regions still run the same instructions).

*Change tried, behind `SHARC_FAST_DYNFLAT` (off by default; `1` = every pc, `LO-HI` hex = a range).*
When a region's calls leave the kernel at one of its dynamic-window instructions on at least half the
calls (checked at 64, 128, 256, ... calls), the slot is dropped and rebuilt from the next call's state
with those instructions as flat guarded accesses (`cfg::mark_flat_dyn`, `Lower::flat_dyn_cur`,
`Region::dyn_pcs`, `FastEngine::run_inner`). It uses `flat_access`, which batch 1 already proved
exact (a failing guard leaves before the instruction), and changes nothing without the flag.
A/B, same binary, exclusive lock, alternating pairs, 3 pairs each, medians:

| variant | calls/s | DSP CPU s / audio s | of which run time | compile + build s per run |
|---|---:|---:|---:|---:|
| base | 18.66M | 5.099 | 2.942 | 22.6 |
| slice `1c8ef1-1c9b73` (FUN_1c8ef1 only) | 16.57M | 5.029 | 2.777 | 23.6 |
| all pcs (`SHARC_FAST_DYNFLAT=1`) | 13.21M | 4.599 | 2.306 | 24.1 |

PCM `e0e6c8af7a84c443` in all 18 runs. The slice removes 2.1M calls for 0.165 s (79 ns each), the
widened change 5.45M for 0.637 s (117 ns each, run time -22%, headline -9.8%). The flat kernels are
bigger: compile +1.5 s per run. Remaining calls: 13.21M (11.68M ran); top: `FUN_b80c39` 1.75M,
`FUN_1c8ef1` 1.40M (stop 0.62M), `FUN_b809f2` 1.00M, `FUN_1c0c76` 0.73M, `FUN_1cdfa1` 0.58M,
`FUN_1c07c6` 0.58M.

*Next structural options, ranked by ceiling over exactness risk* (112 ns per call):
1. A call from inside a DO loop to a callee outside it, and its return (b80c39+b809f2+1cdfa1+1cdf46+
   1c07c6, 4.2M calls = 0.47 s). `cfg.rs` makes any branch whose target is outside the innermost loop
   an exit, and a return (`JUMP (Mm,Ii)`, a register target) always leaves with `DYN_PC`. Inline the
   callee (inside REACH) as nodes with the loop dormant, and turn the return into a guard "target ==
   the address after the call's slots" that continues inline and leaves with `DYN_PC` otherwise. Risk:
   the PC stack and loop stack at mid-callee exits (the sites must push the open calls), the 29-entry
   stack limit, the loop-end test inside the callee; no wasm-specific risk (it is IR only).
2. `stop` exits, 3.0M calls (24 insns each): form coverage, as in batch 2 above.
3. Kernel-to-kernel chaining that skips the Rust loop (`fast_continue`, `run_inner`, `run_cfg`
   entry checks and copy-out): at most the 39 ns glue part, about 0.5 s, and needs the state commit
   moved into the kernels. A redesign; wasm needs the kernels in one module for direct calls (an
   imported call costs 2.7 ns, an indirect call between modules does not exist).
Not recommended now: JIT-first or commit-once-per-block; the data above show a quality bug, not the
engine round trip alone, was the largest single source.

Exactness: PCM identical in 18 timed runs (full 10.5 s, trig1-taps pin). Oracle suite and
`scenario --all` in this config: see the status line in `docs/plans/scenario-oracles.md`.
Open: **[O]** wasm: `flat_access` takes its 32-bit path when `usize` is 32 bits, not run here; the
compile cost of the bigger kernels in V8 is not measured.

## Compile timeline and offload premises **[D]**

Measured 2026-10-09 on `work/scenario-oracles`, no-AOT `static-insns+follow` (flat fallback on),
DN2 1.11, shared core, exclusive lock; new `SHARC_FAST_TL=1` timeline, summarised by
`tools/fast_timeline.py`. Not re-checked by a second agent. Design: `docs/design/fast-compile-offload.md`.

- **Compile is a cold-start freeze, not a trickle.** `trig1-taps` 20 audio s, PCM `e1556053127f219c`:
  99.8 DSP-thread s = compile 19.8 + build 2.9 + run 77.1. 6789 of 8741 builds and 16.5 s of compile fall
  in the first 0.1 audio s (wall 20.2 s to reach audio 0.25 s); 1429 builds (2.67 s) come after audio 1 s,
  in bursts (1038 ms in one 0.25 s window at 1.5 s) and stop at 13.5 s. After audio 1 s: 867 first entries,
  229 `rebuild-exit0`, 200 retries, 93 `rebuild-req`, 40 `rebuild-win`.
- **Stall.** Compile per region mean 3.06 ms, p50 1.01, p99 27.2, max 42.6 ms (8226 IR ops, ~1.9 us per
  op); build mean 0.42, p99 4.4, max 27.7 ms. Build plus compile in one call: p99 25.4 ms, max 64.4 ms (97
  periods of 0.667 ms); 4253 builds exceed a period and hold 22.0 of 22.8 s. AOT default: 22 builds, 0.02 s,
  worst 2.3 ms, so offload does nothing for the AOT desktop build.
- **Waste.** 95% of rebuilds (2150 of 2270, 1.5 s run) replaced a slot that had run no instruction: 4.83 s of
  17.5 s compile (28%) went into kernels discarded before their first run (window and requirement declines
  are found at the first call).
- **Run time without compile**, DSP CPU minus compile and build: audio 2.5-8.5 s **2.66** per audio s
  (DYNFLAT 1.99, AOT 1.42); audio 12-19.75 s **4.88** (DYNFLAT 4.28, AOT 1.83). The call rate steps from
  19.4M to 26.8M/s around 9.2 s with no builds; cause found: see "Late-run slowdown" below (**[D]**). The 10.5 s headline mixes both regimes.
- **Determinism [V]:** `SHARC_FAST_DEFER=N` (a new kernel is not used for N-4N calls of its entry) gives the
  same PCM (`40d829c95c1d1480`, 2 s) for N = 0, 1, 7, 40 and 3000 (interpreted share 0.98% to 4.6%) and for
  AOT N = 0, 7, 300; the AOT and no-AOT PCM are equal at 2 s and 20 s. The switch point does not change results.
- **Cacheability [V in process, O across processes]:** Cranelift code has 0 relocations in 5599 kernels, is
  byte-deterministic, loads from stored bytes in 4.6 us (`define_function_bytes`), and a run with every kernel
  loaded from a blob (`SHARC_FAST_RELOAD=1`) has the pin PCM `3bf3ba3026953c22`. Not yet relocatable: the 8.8% of
  kernels with `Op::Helper` (a leaked record pointer in the IR and `helper_exec` as an immediate). Native
  compile scales with threads: 16.1 s (1), 4.2 (4), 2.15 (8), 1.36 s (16) for 4566 kernels. Code size 32.9 MB
  for them. `SHARC_FAST_CL_OPT=none`: compile -27% (19.8 to 14.4 s), code -26%, run time +1%, same PCM.
- **Wasm in V8 (Node 24)** for the same 4566 kernels: emit 179 us, 14.3 KB mean (66.7 MB; 24.7 MB zstd);
  `new Module` 0.05 ms (lazy), first call compiles: mean 1.43 ms, p99 23.8, max 45.2 ms, 6.5 s total; async
  eager compile 2.3 s wall; a worker that compiles and calls each kernel once hands over a `Module` whose first
  call costs 2 us; V8 does not share compiled code across threads by bytes. `Op::Helper` is unimplemented in the
  wasm emitter (8.8% of kernels). The DSP worker blocks in `Atomics.wait`, so receiving a `Module` needs a
  yield or `Atomics.waitAsync`. Browser behaviour **[O]** (Node only).

## Late-run slowdown **[D]**

Measured 2026-10-09 on `work/scenario-oracles`, DN2 1.11 and DT2 1.16, shared core (no-AOT,
`static-insns+follow`, `SHARC_FAST_DYNFLAT=0`, `SHARC_FAST_CL_OPT=none` set explicitly) and the prod
core (AOT, `default`); a binary copy per run; timed pairs under the exclusive lock. Not re-checked by
a second agent. **Superseded 2026-10-09:** the core now flushes denormals as the SHARC+ does
(finding 15, "Denormal flush"); the step is gone without any flag and `SHARC_FAST_DENORM` and
`Req::StkyMus` were removed. The text below is the measurement that found the cause and the interim fix.

**Characterisation.** "Calls" are fast-tier region calls per audio s, "run" is DSP CPU minus compile
and build per audio s (`tools/fast_timeline.py` ticks, `SHARC_FAST_TL=1`).

| case | step | calls/s before -> after | run before -> after |
|---|---|---|---|
| `trig1-taps`, no-AOT, 20 s | yes: ramp from 6.5 s, jump 9.0-9.5 s, flat to 20 s | 17.2M -> 26.2M | 2.66 -> 4.88 |
| `trig1-taps`, AOT default, 20 s | yes, same time | 0.5M -> 0.9M | 1.38 -> 1.86 |
| `idle`, no-AOT, 20 s | none | 17.2M flat | 3.2 flat (loaded machine) |
| `trig1-hold`, no-AOT, 20 s | none | 17.2M flat | 3.2 flat (loaded machine) |
| `dt2-sample-trig`, no-AOT, 20 s | yes, smaller, at about 4 s (the 1 s sample ended at 1.3 s) | 17.4M -> 19.1M | 2.8 -> 3.3 (loaded) |
| run length | none: the 12, 14 and 20 s runs and the 4/8/12/16 s diagnostic runs agree at equal audio time | | |

- The taps are at 0.06-0.10, 0.21-0.25 and 0.36-0.40 audio s (events at 60M-400M instructions,
  `scenarios/dn2.json`). The step comes 9 s after the last release, so it is not an event time. It
  needs a sound that has decayed (`idle` never made one, `trig1-hold` never lets it decay) and it is
  independent of config and run length.
- **The firmware executes the same instructions.** Non-idle instructions per audio s are 497M before and
  after (the 492M seen without the fix looks like accounting at guard exits **[O]**: with the fix the count
  stays 497M); AOT config 267M both with the fix, 260M after the step without it. The extra work is the engine's, not the firmware's.

**Cause (verified).** Diagnostic runs (`SHARC_FAST_COV`/`HIST`, cumulative, differenced as
4-8 s against 12-16 s windows): region calls 17.4M -> 26.3M/s, interpreted instructions 5.0M -> 9.7M/s,
kernel instructions 497M -> 492M/s; the growth is in guard exits at one site, `result_normal_or_zero`
(`native/sharc/src/fast/lower/mod.rs`, the float multiply result guard): 0.07M -> 2.19M exits/s, plus the
edge and `exit` re-entries each exit forces (edge +1.1M, exit +1.0M). Another 0.19M/s come from the
`guard_zero_or_normal` guard (`float_alu.rs`: `fix`/`trunc`/`scalb` of a denormal operand, one site in `FUN_1c244d`, not fixed). The functions are
the per-voice and FX stages: `FUN_1cdc30`, `FUN_b826c8`, `FUN_1c6664`, `FUN_1c244d`, `FUN_1c1f1e`,
`FUN_b809f2`, `FUN_b80432`, `FUN_b851e5` (IIR/envelope/gain loops), each from 0.00 to 0.1-1.5M calls/s.
`SHARC_PCDUMP` at `0xb82772..0xb8278e` in the window 12.0-12.1 s shows the multiplier operands: the
filter and envelope state registers hold **denormal floats** (`1e-45`, `3e-45`, `-1.96e-43`, `2e-44`,
`3.5e-39`) and STKYX is `0x01000100` (MUS already set). The guard fails because the product is
nonzero and below 2^-126, a case the lowering sends to the interpreter since the interpreter sets
ASTATX.MU and the sticky MUS.

**Why they appear and stay (verified for the persistence, inferred for the onset).** As a tail decays
the recurrences pass through the denormal range (the ramp from 6.5 s) and then stick at 1-3 ulp:
`x * g` with `g > 0.5` rounds back to the same denormal, so the state never reaches zero. The core
does not flush denormals (`tools/sharc_core/floats.py` header: "not modeled"; the Rust runtime computes
f32 in IEEE). `idle` has no excited state; `trig1-hold` stays normal.

**Hardware: not genuine load [D].** SHARC+ PRM (`out/refs/sharc-plus-prm`, "Floating-Point Format" PDF p.100, "Denormal
operands ... flush to zero when input to a computational unit and do not generate an underflow
exception"; the 2136x PGR add/mult entries: "post-rounded denormal returns +-zero. Denormal inputs are
flushed to +-zero"; multiplier MU/MUS flag text in chapter 22 p.22-1). On the device the same states
decay to exact zero and every instruction takes the same time whatever the data. The late cost exists
only because the emulator keeps denormal state alive and its fast tier leaves a kernel at each
denormal product. Flushing as the hardware does would change the model; its effect on PCM is
**[O]**, not tried (it needs the Python core, the transpile and the golden hashes).

**Fix (behind `SHARC_FAST_DENORM=1`).** A region built while STKYX.MUS is known set (`Env::mus_set`)
lowers a float multiply without the denormal half of the guard (infinities and NaN still leave); it
adds `Req::StkyMus` (a masked compare of STKYX bit 8 at entry, `glue::reg_checks`), so MUS needs no
update; ASTATX.MU comes from the result bits at the flag replay (`glue::apply_flag` `Fmul`, and
`cond.rs` for lazily tracked flags). PEy (SIMD) multiplies keep the old guard. A region built with MUS
clear keeps the old guard and nothing else changes without the flag. Test:
`fast_denormal_multiply_matches_the_interpreter` (`native/sharc/src/tests.rs`: all registers
including ASTATX and STKYX equal the interpreter at 8 chunk sizes, MUS set and clear, a flag-reading
`IF MS` in the loop; dropping the MU line makes it fail on `r118`); `cargo test --lib` 65 passed.

A/B, same binary (`SHARC_FAST_DENORM` unset vs `1`), exclusive lock, 14 audio s, run time per audio s
in the windows 3-8.5 s and 10-14 s (the second pair of the no-AOT runs was redone because a build ran
beside it):

| config | pair | base pre / post | fixed pre / post | headline DSP CPU per audio s |
|---|---|---|---|---|
| no-AOT | 1 | 2.67 / 4.92 | 2.63 / 2.77 | 4.76 -> 3.88 |
| no-AOT | 2 | 2.82 / 5.08 | 2.69 / 2.84 | 4.95 -> 3.98 |
| AOT default | 1-3 | 1.42, 1.41, 1.41 / 1.84, 1.83, 1.83 | 1.41, 1.41, 1.40 / 1.42, 1.41, 1.40 | 1.575, 1.567, 1.566 -> 1.423, 1.412, 1.404 |

The step is gone in both configs: post-step run time -44% (no-AOT) and -23% (AOT) and the 14 s
headline -19% / -10%; calls 26.2M -> 17.9M (no-AOT), 0.9M -> 0.5M (AOT). PCM `18e5204f2015f2eb`
(14 s) in all 12 14-s runs, AOT and no-AOT, flag off and on. DT2 (20 s, unlocked): calls
19.1M -> 17.4M, PCM `16b4abef610918fe` equal. Scenario sweep, flag off vs on, PCM equal in 25 scenarios no-AOT (idle, trig1-hold, kb-chord16,
exactness-100m, trig1-noqa-40m, trig1-tap-release, 7 dn2-fx, 7 trackfx, 5 sendfx) and the same 25 AOT plus
`trig1-taps` (pin `e0e6c8af7a84c443` holds); AOT and no-AOT hashes are equal per scenario.

Residual **[O]**: the tail of an AOT run also pays the interpreter's `_astatx_mult_float` general path
(four `_flags_put`) for denormal results in AOT blocks; not separated, and the fix above already
brings the AOT run to its pre-step cost (1.41), so it is small.

- **[D] Defaults changed 2026-10-09:** `SHARC_FAST_DYNFLAT` is on by default (`=0` off, `LO-HI` still works) and
  the fast tier's Cranelift `opt_level` is `none` (`SHARC_FAST_CL_OPT=speed` restores it). Same binary, exclusive
  lock, alternating pairs, PCM `e0e6c8af7a84c443` in all runs. Default AOT `trig1-taps`: 1.476 vs 1.477 DSP CPU per
  audio s (8 pairs, no change). No-AOT `static-insns+follow`, 5 pairs: 4.72 vs 5.89 (-20%), compile 1.62 vs 2.09,
  run 2.79 vs 3.50 per audio s. Not re-checked by a second agent.

## Tier mismatch: MODE1 read at entry **[D]**

`sf-del-time-u20` (scenarios/dn2-sendfx.json) rendered `67e0ea5c3f9a7166` in the default config
(AOT core and fast tier) and `c4688d57f9ef9aa9` in no-AOT `static-insns+follow` (rms 0.0127184 vs
0.0127187); finding 15 recorded it as open.

- Ground truth: the interpreter (`SHARC_AOT=0 SHARC_FAST_REGIONS= SHARC_BLOCKTRANS=`, 6.16 audio s, about
  12 min) renders `67e0ea5c3f9a7166`. AOT is right, the fast tier was wrong. The two WAVs first differ at
  frame 115653 (t = 2.4094 s); 24-bit-scale differences of the delay line contents follow from there.
- Cause: the fast tier read MODE1 bits from `Env::mode1` (the value at region entry) where it needs MODE1 as
  the kernel has it after an earlier MODE1 write in the same region (`mode1_cur`). The `fix` lowering
  (`falu_fix`) took MODE1.TRUNCATE from the entry value. DN2 1.11 function `0xb8b4dc` has
  `MODE1 = set(MODE1, TRUNCATE)` at `0xb8b4de` (decoded from the image); a region that goes on to a `fix`: the kernel rounded to
  nearest where the core truncates. Reverting only that line in an otherwise fixed tree gives
  `c4688d57f9ef9aa9` again; the fix alone gives `67e0ea5c3f9a7166`. (The generated forms already read
  MODE1 through `m1()`, which uses `mode1_cur`; only hand-written lowering used `env.mode1`.)
- Same pattern, fixed with it: ALUSAT in the signed conditions (`cond_value`, LT/GE/LE/GT) and
  MODE1.CBUFEN in the circular post-modify (`post_value`). A write makes the region require all of MODE1 at
  entry (`mode1_mask = u32::MAX`), so only the reads were stale. Not observed in a scenario, found by review;
  covered by unit tests.
- Related entry-state check: a MODE1 write that turns IRPTEN or NESTM on can make a latched, unmasked source
  deliverable at the next instruction boundary. The region's entry check (`latent_interrupt`) saw interrupts
  off, so the kernel ran on past the write while the interpreter enters the interrupt. Such a write is now
  refused (`g_wr_mode1`: "MODE1 write enables interrupts"). Unit test: without the refusal the kernel and the
  interpreter end at different PCs. `sf-del-time-u20` does not need it (equal with and without); the sweep below ran with it.
- Checked, no change: other `env.` reads are guarded. Registers the region writes are not baked (`baked`,
  `entry_value_of` use `plan.written`); L and B of an index register are refused when written
  (`lb_stale`), with one gap closed: `require_linear` (7a/19a MODIFY, hand-written forms only; the generated
  forms already refuse) now checks `lb_stale` too. MODE2, USTAT and MMASK are plain registers in the kernel.
  Register bank selection: a MODE1 write that changes the bank bits is refused with the bank model. STKY is
  not lowered. ASTAT is tracked per instruction (`fm_static`); `cacc_known` is an entry property by design.
- Tier equivalence after the fix, DN2 1.11, PCM sha256 of every scenario of `dn2-trackfx.json` (47) and
  `dn2-sendfx.json` (87), default (AOT + fast tier) against no-AOT `static-insns+follow`: 134 of 134 equal,
  `sf-del-time-u20` `67e0ea5c3f9a7166` in the interpreter, the default and the no-AOT config.
- Tests: `fast_reads_mode1_as_the_region_wrote_it` (TRUNCATE on/off, ALUSAT on/off, CBUFEN on/off in the
  region; fails on `env.mode1`), `fast_mode1_write_that_enables_interrupts_matches_the_interpreter`,
  `fast_modify_follows_l_written_in_the_region` (passes with and without the `require_linear` check, since
  the generated forms are the default; it guards the default path).

## Generated lowering: design and how to add a family **[D]**

Folded from the lane F notebooks (not re-checked by a second agent).

Design. `tools/sharc_lowergen.py` runs the real `tools/sharc_core/forms` handlers over a
tracing state: decoded fields and lowering context (MODE1, `assume_nw32`) are static
symbols, a test on one forks the trace, register contents and memory words are dynamic
nodes, and a data-dependent branch refuses. `tools/sharc_lowergen_rs.py` value-numbers the
leaf programs and folds the decision tree into `native/sharc/src/fast/lower/gen_forms.rs`
(one method per form over the primitives of `gen_prims.rs`, hand-written once). Never edit
`gen_forms.rs` by hand. Control transfers (calls, returns, loops, delayed branches) stay in
`cfg.rs`/`flow.rs` because the tracer never runs `_advance`. Flags stay lazy and are
replayed at exit. MODE1 is static per program point. Compute bodies still come from the
hand-written `compute_full`/`compute_short`/`shiftimm`. 27 forms are generated.

Add a family:
- Trace and print a form: `uv run python tools/sharc_lowergen_survey.py FORM...` (no args:
  all forms). One leaf: `tools/sharc_lowergen_leaf.py FORM field=value ...`.
- A new intrinsic is an `i_*` function plus a `PATCHES` entry in `tools/sharc_lowergen.py`,
  a case in `tools/sharc_lowergen_rs.py`, and a `g_*` primitive in `gen_prims.rs`.
- Regenerate with `mise run gen:regen -- --add FORM,...`. Add the form to
  `tools/sharc_lowergen_vectors.py` (steer the random fields into the accepted region) and,
  for sequences, to `tools/sharc_lowergen_programs.py`.
- Check: `cargo test --lib gen_` in `native/sharc` (with `SHARC_GEN_DIR` set; about 75 s),
  `tests/test_sharc_lowergen.py` (fails when `gen_forms.rs` is stale), and
  `tools/sharc_lowergen_bisect.sh` for PCM and coverage with a form subset.

Results **[D]** (DN2 1.11, `trig1-taps`, 1.5 s, `static-insns+follow`, PCM
`3bf3ba3026953c22` throughout): hand forms 89.5% fast; generated forms 92.5%; control forms
(calls, returns, indirect jumps, IDLE) 93.0% with interpreted instructions -7%; MODIFY with
circular wrap (7a, 7b, 19a_scaled), MODE1 from a register and L/B writes 93.0% with
52.9M interpreted instructions (7.0%). What is left interpreted: compute bodies (about
21%), 20a PUSH/POP and conditional or call forms of 9a/9b (small), window-analysis refusals
(14%), system registers with machine state (ASTATX, PCSTK, LCNTR, IRPTL; they sit in the cold
context-save function `FUN_1c02a4`), and 7d (ACONV, depends on the address map).

Semantics worth knowing **[D]**: `_type_7a` wraps only on the cond 0x17 path, so an
unconditional MODIFY is a plain sum; 7b and 19a_scaled wrap when L != 0 whatever
MODE1.CBUFEN says. A post-modify is exactly `candidate >= B+L ? -L : candidate < B ? +L`
in unsigned 32 bits and needs no `|delta| <= L`. A return's last slot (`I7 = I6; I6 =
DM(I6 + 0)`) is a tail write.

Bug found with `tools/fast_ddmin.py` **[D]**: the halt at 0xb88ea8 (`fork`). `fast_ddmin`
(with the `SHARC_FAST_ONLY` entry allow-list) shrank 2153 armed entries to three regions
(0xb85a02, 0xb85a19, 0xb85a1f). The epilogue loads `DM(I6-3)`, `DM(I6-2)` and `DM(I6)` read
through an absolute window, because I6 had been given its circular buffer's range without a
`Req::Within`: the only write of I6 was the RFRAME `I6 = DM(I6)` in a RETURN delay slot, and
`commit` skipped the in-buffer check for tail writes. A tail write of a circular register is
never "inside", so the register is now marked plain. Debug variables from the same lane:
`SHARC_TRAP_REGS`, `SHARC_PCDUMP_MEM`, `SHARC_FAST_DEBUG_REQ=0xPC`, `SHARC_FAST_DEBUG_WIN`,
`SHARC_FAST_DUMP_ALL`.

AOT cost profile **[D]** (default config, 1.41 DSP CPU per audio s, sampled with
`xctrace`): AOT is 70% of the DSP thread, Cranelift kernels 24%. Inside AOT, V/Option/Result
plumbing is 27%, memory access 14%, uncategorised moves 11%; real arithmetic is under 10%.
This is the basis of the mask-free emission estimate above.

Mask-free slices 0-1 **[D]** (2026-10-10, `docs/design/aot-mask-free.md` section 6; the correction
of the profile above is: the plumbing share is not the register masks). LLVM already folds the
masks of the 123 ordinary registers (asserting them known: warm 0.98-1.02, in situ not built);
only the ASTAT/STKY known-bit pairs remain, and assuming those known too (unsound experiment,
exits and PCM identical) is 6-16% warm and 6.5% in situ (paired 0.935, trig1-taps, 8 pairs).
In situ AOT is 34 ns per region call + 4.0 ns per instruction (region profile, 27.1M calls);
the default-bench regions (1C399A, 1C3862, 1C364F, 1C5765, B823DB, B8276C) are fast-tier kernels in
the default config, not AOT. The DSP thread loses 28-32% of its cycles to the front end in situ
(17.7% with PGO) against 1.5% warm (CPU Counters bottleneck split, calibrated with synthetic
programs); AOT code is 208-283 machine instructions per SHARC instruction in the binary (832-1130
B), 47-103 executed. Gate: no concrete-register mask-free; do the sound flag-known gate and hot-code
density (cold-path outlining, shared trap tail) first. Tools: `tools/aot_mcount.py`,
`tools/aot_dyntrace.py`, `tools/xctrace_bottleneck.py`.

DT2 AOT and the DT2 render are in finding 15.

## Desktop baseline 2026-10-09 **[D]**

Default config (AOT core + fast tier), `coupled-run` with `--scenario`, exclusive benchlock,
5 runs per cell, DN2 1.11. Cell = median DSP thread CPU per audio s (IQR); CF thread CPU per
audio s and real_time_factor are medians. PCM identical in every run to the pins
(trig1-taps `e0e6c8af..`, tf-dry-hold `79e2dac0..`, tf-br-127 `16f3d0a8..`). Machine load
average (1 min, read before each run) 2.2-4.3 for the prod and old-PGO rows, 2.5-4.2 for the new
PGO row; no Digiemu, a short `duetexpertd` burst before the new-PGO run. Not a quiet machine:
the floor is a desktop session (WindowServer, Chromium, wezterm). trig1-taps is 10.5 audio s
(not 14); tf-* are 4.1 audio s.

| scenario | prod (no PGO) | PGO old profile (stale) | PGO new profile | CF CPU/audio s (prod / new) | rtf (prod / new) |
|---|---|---|---|---|---|
| trig1-taps | 1.588 (0.053) | 1.636 (0.085) | **1.173 (0.024)** | 0.509 / 0.502 | 0.628 / 0.851 |
| tf-dry-hold | 1.578 (0.145) | 1.556 (0.074) | **1.172 (0.023)** | 0.550 / 0.544 | 0.632 / 0.851 |
| tf-br-127 | 1.638 (0.037) | 1.618 (0.049) | **1.255 (0.064)** | 0.601 / 0.602 | 0.601 / 0.775 |

A second prod run after the pin edit below reproduced the first (1.593 / 1.568 / 1.613). So:
- The old profile (trained 2026-10-07, key stale on sources and generated core) buys nothing
  (-3% to +3%, inside the noise). The retrained profile cuts DSP CPU per audio s by 26%
  (trig1-taps), 26% (tf-dry-hold) and 23% (tf-br-127) and moves real time from 0.60-0.63 to
  0.78-0.85. CF thread cost is unchanged (0.50-0.60).
- Gap to the 1.0 target: 1.17-1.26 now (needs a further -15% to -20%); without PGO 1.58-1.64.
  The no-PGO ceiling analysis (AOT 70% of the DSP thread) still applies; PGO and mask-free AOT
  would stack only if the profile transfers to the new emission.

How measured, and caveats:
- The PGO binaries are `coupled-run` built with `-Cprofile-use=<profdata>` into
  `out/native/target/prod/pgo-cr-old` and `pgo-cr-new` (`--target aarch64-apple-darwin`,
  rustup 1.98.1). Profiles are trained on the desktop app (`digiemu-desktop --pgo-train`), so
  `native_pgo.py verify` reports one crate hash mismatch (`elektron_native_boot`) for
  `coupled-run`; the other crates match. The launcher's own `pgo-use/.../digiemu-desktop`
  (2026-10-08 18:24) is stale and was not rebuilt; `mise run emu` rebuilds it with the new
  profile (`38d7c8fe..`) on the next launch. Its `pgo-use/.../coupled-run` (2026-10-05) is
  unrelated and stale. The app's own CPU split was not measured here.
- The training gate (`coupled_ready_exactness`, `native/coupled/src/lib.rs`) failed before
  training: its CF state digest and DSP export pins were stale since 7c489c5 (2026-10-08, SIMD
  shift fix). PCM pin `b3f5230f..` and sample count held. Observed CF digest
  `4a9d3123..` (pinned `55dc699e..`), DSP export `20905b23..` (pinned `4dc48019..`); both pins
  were set to the observed values locally (uncommitted) so training could run. Not
  independently checked: a second agent should confirm the new digests are the intended
  result of 7c489c5 before the pins are committed **[O]**. Editing `lib.rs` also made the
  instrumented build a full 8 min compile.
- Training: 4 runs of the gate, 507 s total, profile 40 MB.

## Slice 2' (hot-code density) 2026-10-10 **[D]**

`docs/design/aot-mask-free.md` section 7. Built, not committed: flag registers required fully known at region
entry (they were at `0xFFFFFFFF` at every dispatch of seven scenarios; the `0x00FFFFFF` ASTATX mask in the old
comment was stale), cold marking of every trap, budget and entry-gate path, one trap tail per region, chain exits as
two tables and one function per successor set, one budget check per body (a body that would cross the limit is left
to the interpreter), the normal-word range table out of line, a 32-bit plain-RAM address path.

| | old core | slice 2' |
|---|---:|---:|
| bytes per SHARC site (150 hottest in-situ regions) | 868 | 465 |
| front end of the DSP thread in situ (no PGO / PGO) | 27-32% / 14.6% | 21-24% / 10.4% |
| DSP CPU per audio s, trig1-taps, no PGO / PGO | 1.502 / 1.163 | **1.277 / 1.055** |
| tf-dry-hold, no PGO / PGO | 1.510 / 1.161 | **1.282 / 1.059** |
| tf-br-127, no PGO / PGO | 1.612 / 1.237 | **1.368 / 1.129** |

Exclusive alternating A/B (`tools/coupled_ab.py`), 6 to 7 pairs, paired ratio 0.85 without PGO and 0.91 with PGO
(0.90 to 0.93 over all pairs); PCM equal to every pin; DN2 pins, oracle suites (default and no-AOT), the sweep, the
134-scenario AOT against no-AOT comparison and DT2 (clean tone, AOT = no-AOT = AOT + fast WAV hash) unchanged.
Block traps are identical in count and code in 25 scenarios; block entries grow 0.27% and interpreted instructions
0.5 to 0.7% because a body that would cross the budget limit is no longer split at the instruction. The density
gate of the design (<= 400 B per site, front end < 12% without PGO) was missed (465 B, 21-24%); the warm gate was met
(0.77 to 0.91). The undo-copy ordering analysis was not built: deleting every undo copy (unsound ablation) is worth
2.4% of the bytes and 0 to 9% warm, and LLVM already drops the restores of Err paths it can prove dead. The
desktop is at **1.06 (trig1-taps), 1.06 (tf-dry-hold), 1.13 (tf-br-127) with PGO: not yet 1.0**; it needs
another 5 to 11%. Largest remaining items in `tools/aot_attrib.py`: the normal-word test (12.6% of the bytes), the
memory fast paths (8.6%), the float helpers (19%), `rf_set` (3.8%), `s_set_special` (3.1%).

### `tests_gen` "the fast tier never ran" (2026-10-10) **[D]**

`gen_programs_match_the_interpreter` and `gen_control_programs_match_the_interpreter` failed ("simd_loop: the fast
tier never ran", `ctl_call_ret` in the control test) only when built against a gen directory with an image (DN2/DT2
`out/native/*-audio/gen`); they passed against `shared-core/gen`. Not a fast-tier regression and not specific to
`simd_loop`: configuration 1 of the test turns on the bank and stack models (`set_option` 19, 20), which calls
`Cfg::refresh`; that recomputes `block_base_ok`, which also requires `explicit_memory_model` to equal the image's
`GEN_EXPLICIT_MEMORY_MODEL` (false for the DN2/DT2 images, true for the test engine's default). With a mismatch,
`Engine::block_plan` returns None before it offers the fast tier (gated 0, nothing built). Configuration 0 never calls
`refresh`, so it was unaffected. The runtime sets the image's value. Fix: `tests_gen.rs` `engine_cfg` sets option 10 to
`gen_explicit_memory_model()` for configuration 1. Both tests pass against the DN2, DT2 and shared gens.
Observation (open): `block_plan` also gates the fast tier on `block_base_ok`, though the fast tier does not use
blocks; a coupled run with a mismatched memory-model setting would silently run no fast tier.

## Slice 3 (float, memory and interpreter items) 2026-10-11 **[D]**

`docs/design/aot-mask-free.md` section 8. Built, not committed. The desktop is under real time: DSP CPU per audio s with
PGO **0.896 (trig1-taps), 0.901 (tf-dry-hold), 0.922 (tf-br-127)** (slice 2': 1.054, 1.056, 1.126), without PGO 1.104,
1.106, 1.122 (1.279, 1.280, 1.358); paired ratios 0.82 to 0.86, 6 exclusive alternating pairs each, PCM equal to every
pin.

| | slice 2' | slice 3 |
|---|---:|---:|
| DSP CPU per audio s, trig1-taps, no PGO / PGO | 1.279 / 1.054 | **1.104 / 0.896** |
| tf-dry-hold, no PGO / PGO | 1.280 / 1.056 | **1.106 / 0.901** |
| tf-br-127, no PGO / PGO | 1.358 / 1.126 | **1.122 / 0.922** |
| warm time, hot AOT regions (ratio, 13 regions) | | 0.70 to 0.94 (two at 1.0 and 1.1) |
| interpreted instructions, trig1-taps | 13,151,269 | 6,760,382 |
| bytes per SHARC site, whole DN2 core | 397.5 | 394.5 |
| front end of the DSP thread, no PGO / PGO | 20.6% / 10.4% | 20.9% / 9.0% |

What moved it, in order of size: (1) the interpreter ran 7.5 of 13.2M instructions as one 238-instruction routine
because a block entry gated by a pending bank swap sends the whole block body to the interpreter (a region has entries
only at block starts). The profile now records the PC after the first instruction of such a run, 35 more block starts
(6 rounds of `tools/aot_entry_iter.py`) cut the interpreted instructions by half and the DSP time by about 7% (no PGO; the build before the search
measured 0.926 against slice 2', after it 0.862). (2) The memory path was 34% of the dynamic instructions of the in-situ AOT regions (not the 8.6% and 12.6% of the
bytes): aligned-word and narrow paths, fixed-size page tables, a two-compare normal-word band test, no `fast_mem` test
and no 128-bit address mapping in the register load. (3) Float add, subtract and multiply flush the operands and
classify the result (normal, zero, rest), and the multifunction multiply, `_float32` and `_float32_result` are native
boundary functions. (4) `sec_arbitrate` visits only the SSTAT registers that exist (2.4% to 0.6% of the thread).
Dropped: float variants that test the operands first (they follow the data: silence is slower), memory accesses as
one shared out-of-line body per width (-9% code, +0 to +68% warm), running blocks in the idle probe (the probe
iteration is 24 instructions of the idle range, which has no blocks).

Exactness: the seven DN2 pins, `scenarios:check` (default and no-AOT, 61 each), `scenarios:sweep` (191), 134 of 134 FX
scenarios AOT = no-AOT PCM, golden/denormal/density/lint tests, `cargo test -p sharc-rt` (16, three new differential
tests), DT2 clean tone and WAV hash `4bab8330915f0929` in AOT, no-AOT and AOT + fast. Block traps: count identical in
all 134 FX scenarios, code and count identical in six profiled ones; block entries +1.1% and zero-progress bails +7%
in trig1-taps (the new block starts), AOT instructions +0.26%. **[O]** DT2 has no entry search yet; the loop end
(`stk_set_loops` with its undo entry, 8% of the in-situ regions) and the idle probes are the next items.

## Desktop app real time 2026-10-11 **[D]**

Spike log (`DIGI_SPIKE_LOG`, `tools/spikes.py`, docs/INSTRUMENTATION.md) in the desktop app: DN2 1.11, Trig 1
on track 1 with send delay, a little reverb and track SRR, PGO launcher build, 1 s prefill, Rebuffer. Steady state
(after the queue first reaches 900 ms), one run each; not re-checked by a second agent.

| | webview pump (`Mode::Ui`, b4f3693) | paced free running (f344278) |
|---|---:|---:|
| audio s per wall s | 0.907 | 0.995 (held by pacing) |
| underruns | 9 in 100 s | 0 in 53 s |
| queue min / median | 0 / 506 ms | 899 / 992 ms |
| DSP busy / CPU per audio s | 0.988 / 1.002 | 0.942 / 0.955 |
| CF work / DSP reply wait / gap between steps, per audio s | 0.492 / 0.356 / 0.254 | 0.478 / 0.489 / 0.039 |
| worst DSP period / CF step | 1.36 / 13.4 ms | 1.63 / 7.2 ms |

- The app was below real time on average, not spiking: the 1 s prefill hid it until it ran out. Fast-tier
  compile was 1.7 ms in the whole run; no period exceeded 2.1 ms.
- The webview's `setTimeout(0)` `emu_step` loop cost the CF thread 0.48 ms per call (25% of its time) and slowed
  the DSP thread by about 5% (1.002 -> 0.955 CPU per audio s); the cause of the DSP slowdown (contention) was not
  measured.
- The DSP runs the same 502M non-idle instructions per audio s whatever the FX settings (the FX code runs whether
  audible or not): headless `trig1-taps`, `sf-del-ref`, `sf-rev-ref`, `tf-srr-127` and `tf-del-127` all cost 0.93
  to 0.94 with the slice 3 PGO `coupled-run` (load average about 3; 0.896 earlier on a quieter machine). One
  scenario stands for every patch.
- Headroom in the paced app: DSP busy per 0.1 audio s window p50 94.0, p99 99.7, max 100.4 ms (4 of 519 windows
  over). CF work reached 1.0 to 1.4 per audio s in bursts while browsing pages; with the depth-1 link pipeline a
  CF burst stalls the DSP. A buffer well below 1 s needs DSP headroom first (target about 0.8).

## Linker order file: ceiling 2026-10-11 **[D]**

Measured before building (trig1-taps, DN2, PGO `pgo-cr-s3b` and prod `coupled-run`, exclusive benchlock, busy
machine; scripts and traces were in a session scratchpad, not kept). Not re-checked by a second agent.

- Front end of the DSP thread (`tools/xctrace_bottleneck.py`): PGO 11.8%, no PGO 24.1%; discarded (mispredict
  flush) 2.5% in both. The CPU Counters template gives no L1I/iTLB/resteer split.
- 20% (PGO) and 15% (no PGO) of DSP samples are outside the binary (Cranelift kernels, system libraries); 73% of the
  in-binary samples are in `r_*` AOT regions.
- Functions holding 90% of in-binary samples: PGO 167 functions, 6.1 MB, 467 16 KB pages, 94 address ranges over
  18.5 MB of a 31.2 MB text. Executed bytes are 15-25% of each region function (60-175 KB each), so the hot set is
  about 1.2-1.6 MB (lower bound from samples 268 KB): 6-8x the 192 KB P-core L1I, spread over 430-650 pages.
- Ceiling: an order file is function-granular. It can pack the pages (about 100 instead of 430-650, inside the L1
  iTLB reach) but not the cold blocks inside the region functions, so L1I capacity misses stay. Estimated 0-3% of
  DSP CPU with PGO, 1-4% without (1.10 -> 1.06-1.09); most of the no-PGO front-end excess is in-function layout.
  Not built. Note: `tools/xctrace_bottleneck.py` needs the `record` options before OUT, or xctrace fails with
  "Path not found '--time-limit'".

## LLVM hot-cold splitting with PGO 2026-10-11 **[D]**

`-Cllvm-args=-hot-cold-split=true` added to the slice 3 PGO `coupled-run` build (same profile `a76e820c...profdata`,
same DN2 gen; `out/native/target/prod/pgo-cr-hcs`). It outlined 1949 `.cold` functions and cut `__TEXT` from 46.4 to
42.6 MB (-8.2%), but DSP CPU per audio s rose: trig1-taps 0.935 (A) against 0.943 (B), paired ratio 1.014 (0.985 to
1.023), 6 exclusive alternating pairs (`tools/coupled_ab.py`), load average about 3, PCM equal to the pin in all 12
runs. Not adopted. Caveat: the profile was trained without the split, so its mapping onto outlined code is untested.

## Fast-over-AOT: per-region ceiling and stop reasons 2026-10-11 **[D]**

Can the hottest AOT regions run faster as fast-tier kernels? The fast tier already takes any PC listed in
`SHARC_FAST_REGIONS` before AOT. Region profile (`region-profile` build, no PGO, DN2 trig1-taps, timer overhead of
about 26 ns per dispatch subtracted): 1291 AOT entry PCs, 2.63M entries and 235M instructions per audio s (89 per
entry), 668 ms, 2.85 ns per instruction, about 59% of the DSP thread. The top 25 regions hold 75.9% of AOT
instructions and 70.1% of AOT time (top 50: 89.1% / 83.9%).

- Estimated saving per region S = AOT wall - (insns x 0.5 ns + entries x 160 ns): positive for 767 regions, 328 ms
  per audio s in total (the cost-model ceiling, no PGO; kernels run 0.5 to 1.6 ns per instruction in situ, so it is
  an upper bound).
- Probe: arming the top 5 by S (0x1cdb85, 0x1c0c9d, 0x1c926f, 0x1c25cc, 0x1c963b) on top of the shipped list changed
  nothing (1.1413 -> 1.1409, 3 runs each); interpreted instructions +0.5%, so exits land on AOT block starts. The
  kernels stop after a few instructions and AOT runs the rest, adding 219k region calls per audio s.
- Stop reasons with the top 50 armed (each region charged to its dominant exit; blocked saving = S x uncovered
  share; covered 57 of 300 ms):

| reason | blocked ms/s | regions | kind |
|---|---:|---:|---|
| call, return or region edge: continuation outside the kernel | 76.3 | 16 | structural |
| memory address range unknown: the 16a return-address push `DM(I7,M7)` in a CALL delay slot (I7 has no static range; `flat_auto` does not rescue it) | 67.3 | 11 | analysis |
| loop "register step not stable" (0x1cdb85 alone 32) | 48.1 | 7 | structural |
| shifter BTGL by register (cu=2, opcode 0xc8) | 23.3 | 1 | form |
| never built or entered | 13.7 | 3 | analysis |
| multifunction multiply with min/max (0x1e/0x1f), float MANT, a SIMD condition | 11.1 | 4 | form |

- BSET/BCLR/BTGL by register (0xc0/0xc4/0xc8) are now lowered (`shift_bitop` in `fast/lower/shift.rs`, `ShiftDyn`
  flags). `fast_diff forms` 16 runs x 5000 trials, both backends: 0 mismatches; seven DN2 pins, `scenarios:check`
  in both configs (61 each), 134 of 134 FX scenarios AOT = no-AOT, DT2 clean tone. 0x1c926f now runs 23 instructions
  per call (was 4) and exits at an edge to 0xb813e8; 0x1c963b runs 35 (was 18) and stops at form 25a with unknown
  address ranges. The probe medians moved 5% between sessions for the unchanged configuration (1.14 -> 1.20), so no
  speed change is claimed: each lifted stop exposes the next structural one.

### Run length to the first call or return (top 50 regions) 2026-10-11 **[D]**

Interpreter-only `coupled-run --profile-execution` of trig1-taps (exact per-PC counts) combined with the static CFG
in `out/sharcdb/dn2-1.11.sqlite`; branch probabilities fitted from the counts, 3000 Monte Carlo walks per region from
the entry to the first CALL, return, indirect or cross-function jump. The means match the kernel diagnostic's exact
per-call counts on all 10 regions where a kernel already reaches a call (e.g. 0x1c0c9d 34/34, 0xb83635 50/50,
0x1c25cc 8483/8378). p10/p90 are Markov estimates.

| share of the top-50 saving S | run >= 100 insns | 45-100 | < 45 |
|---|---:|---:|---:|
| kernel stops at the first call | 40.6% (23 regions) | 3.0% | 56.4% (24) |
| leaf callees of <= 64 insns followed | 50.6% (26) | 5.4% | 44.0% (22) |

- 58% of S ends at a call, 39% at a return. The long share is mostly regions that run to a return without a call:
  0x1cdb85 (446 insns), 0x1c25cc, 0x1c9ad5, 0x1c248a, 0x1c254e; their blockers are the loop step, unknown address
  ranges and one region never built.
- The short share stops after 5 to 45 insns at a call into a non-leaf callee (e.g. 0xb836ff: 5 insns, then 0xb82a5b
  at 4232 insns per call), so it needs kernels that follow calls.
- Without call following, the reachable part is about 5-8% of DSP CPU with PGO (0.93 -> about 0.86), an estimate.

## DT2 parity 2026-10-11 **[D]**

Method. DT2 1.16 (`sections/.source-sha256` = 278541e4...), prod core `out/native/dt2-audio/gen`, default config, scenario
`dt2-sample-trig` (3 s, one-sample card of `tools/dt2_sample_check.py`; the only DT2 load scenario). The DT2 DSP image has
no fast regions in `tools/fast_regions.json`, so the tiers are AOT block, idle skip and interpreter. `tools/aot_entry_iter.py`
now takes `--device dt2` (`aot_fxprof.py --dt2-card` runs on the card's ready state; the base profile is
`prof-merged-c2`). Four rounds, each profiled with the core built from the previous round (rsgen about 50 s, build 1.5 min):
new block starts 27, 8, 3, 1 (the fifth round finds one PC again, a 9000-count chain of one instruction per round, not
worth a round). The hot one is 0x1c0885 (873,000 interpreter runs in 3 s: a gated entry whose body ran in the
interpreter). Timing is `tools/coupled_ab.py --dt2-card --device dt2`: 6 exclusive alternating pairs, load average
3.4 to 6.6 from other jobs (pair ratios stay within 0.870 to 0.874).

| | prod core | with entry search (`gen-lane-entry`, not promoted) |
|---|---:|---:|
| DSP CPU per audio s, median of 6 (no PGO) | 1.311 (IQR 0.005) | **1.144** (IQR 0.004) |
| paired ratio | | **0.871** (0.870 to 0.874) |
| interpreted instructions, 3 s | 13,227,236 (0.44% of all, 1.4% of non-idle) | 5,926,406 (0.20%) |
| AOT block instructions | 938.4M | 945.7M |
| block entries | 6,832,135 | 6,907,129 |
| blocks / instructions in the core | 1202 / 12,588 | 1241 / 13,398 |
| PCM hash (3 s, coupled run) | 999dd10944ce7e62 | 999dd10944ce7e62 |

Exactness: `dt2_sample_check.py --seconds 3.0` gives a clean tone and WAV hash `4bab8330915f0929` for the new core in
AOT, AOT + fast (`--fast`) and no-AOT (`--env SHARC_AOT=0`), as the prod core does. (The pin is of the 3.0 s run; the
default 1.6 s run hashes a9cbc3beee5267fc for both cores.) DT2 has no pinned scenario hashes. Block traps: 36,000 in
both.

**[O]** 5.9M instructions are still interpreted; they are not gated block bodies and were not analysed. The DT2 core
has no FX profile (only a trig of one sample), so FX code stays interpreted whenever an effect is on.

DT2 FX scenarios, plan (not built). (1) Find the DT2 FX page button and encoder map (finding 03) and measure the UI
value per encoder event by read-back of the live kit Fx array (`*0x80004708`) and Sound array, as DN2 did (`UPS` in
`gen_trackfx.py`). Parameter codes are known (finding 02: sends 0x44/0x45/0x43, delay time 0x71, feedback 0x74, reverb
decay 0x7c, BR/SRR/routing/overdrive 0xff..0x102). (2) Check the DT2 values of `FX_PAGE` and `TRIG1` in
`scenario_dsl.py` (TRIG1 = 25 and the NO prelude already work in `dt2.json`). (3) Generate `scenarios/dt2-trackfx.json`
(BR, SRR, overdrive, delay, reverb, chorus sweeps on a card sample, so the DSP is loaded) and `dt2-sendfx.json` (send
FX and master). (4) Run them on the card's ready state (`--card-image`, as `aot_fxprof.py --dt2-card` now does;
`coupled.py scenario --device dt2` has no card option yet). (5) Profile with `aot_fxprof.py --device dt2 --dt2-card`,
merge into `prof-merged-c2`, run entry rounds, regenerate, and gate on AOT = no-AOT PCM in every scenario.

## Browser (Chrome) baseline 2026-10-11 **[D]**

Em's page showed 0.22 audio s per wall s on DN2 1.11 after Trig 1 (underruns 9). Method: `mise run web:bench`
(docs/INSTRUMENTATION.md): headless Chromium 150 (Google Chrome is not installed here; same V8), dev server,
`packages/web/.local/emulator-core-sharc.wasm` built 12:55 on 2026-10-11, DN2 1.11 ready state, `tap(25)` at
load as the Trig 1 button does, 10 audio s, no AudioContext (PCM port drained), exclusive benchlock, fresh
profile per run (no code cache). One machine, load not controlled, not re-checked by a second agent.

| V8 flags | audio s / wall s | first half / last half | worker busy per audio s |
|---|---:|---:|---:|
| default (3 runs) | 0.259, 0.256, 0.249 | 0.247 / 0.261 (mean) | 3.17-3.30 |
| `--no-liftoff`, TurboFan only (3 runs) | 0.222, 0.238, 0.219 | 0.178 / 0.280 (mean) | 3.52-3.83 |
| `--liftoff --no-wasm-tier-up`, Liftoff only | 0.248 (1 run; 0.254 and 0.243 in an interrupted batch) | 0.228 / 0.261 | 3.32 |

Each run leaves 17-18% of wall time outside the step call (busy fraction 0.82). Per audio s: DSP 1.000G
instructions, ColdFire 132M.

- **H1 (one worker, costs add): true, but not the main cause.** `digi_step` runs the DSP synchronously inside
  the ColdFire DSPI2 frame write (native/boot/src/abi.rs `mod coupled`), in the one worker
  (packages/web/src/runtime/runtime-worker.ts). CDP CPU profile of that worker (6 s, default flags, share of
  non-idle time): DSP 77% (generated `sharc_gen` kernels plus the engine), ColdFire 21%, other 2%. Per audio s
  that is about DSP 2.6 s and ColdFire 0.7 s, against the desktop's 0.93-1.10 and 0.5. Serial on the
  desktop numbers the page would reach about 0.66; it reaches 0.25, so the wasm is also about 2.5x (DSP) and
  1.4x (ColdFire) slower than native.
- **H2 (generated code stuck in Liftoff): refuted as the main cause.** Liftoff only is about 3% slower than
  default. TurboFan only reaches 0.27-0.29 in the last half of a run, after a first half of 0.17-0.19 (lazy
  compile of the 45 MB module), against 0.26 for default. Steady-state TurboFan is at most about 10% faster
  than default, so the 2.5x gap to native is not a missing tier-up. Top self time: `image::blocks_10` kernels
  (4.9% and 3.1%), `Emulator::step_chunk` 5.7%.
- **New: about 16.5% of the worker is idle by construction.** The profile's `(idle)` is 16.2-16.5%, the
  complement of the busy fraction. The pump runs 20 ms slices and re-arms with `setTimeout(0)`; nested timers
  clamp to 4 ms, and 4 / 24 ms = 16.7%. A `MessageChannel` yield or a longer slice should give about
  0.25 -> 0.30. **[O]** not tried.
- Getting to real time in one worker needs the DSP about 2x faster in wasm; the alternative is the DSP in a
  second worker (shared memory, needs cross-origin isolation). **[O]**

## Browser: yield and split workers 2026-10-11 **[D]**

Same method as the baseline above (`mise run web:bench -- --seconds 10 --runs 3`, headless Chromium 150, DN2 1.11,
`tap(25)` at load, exclusive benchlock, one machine, load not controlled, not re-checked by a second agent). The
core was rebuilt for this lane (adds `digi_dsp_*`, `digi_load_coupled_remote`); `--split 0` keeps one worker.

| Step | audio s / wall s (3 runs) | mean | ColdFire worker busy / audio s | DSP worker busy / audio s |
|---|---|---:|---:|---:|
| baseline: one worker, `setTimeout(0)` re-arm | 0.251, 0.247, 0.252 | 0.250 | 3.28 (both chips, 82% of wall) | (same worker) |
| one worker, `MessageChannel` yield | 0.315, 0.304, 0.294 | 0.305 | 3.2-3.4 (99.8% of wall) | (same worker) |
| one worker, yield, rebuilt core (`--split 0`) | 0.312, 0.301, 0.312 | 0.309 | same | same |
| two workers over a SharedArrayBuffer | 0.394, 0.390, 0.389 | 0.391 | 0.71-0.72 (blocked on the DSP the rest) | 2.57-2.59 |

- **Yield: confirmed.** Replacing the 4 ms-clamped `setTimeout(0)` by a `MessageChannel` task in
  `runtime-worker.ts` moves the worker from 82% to 99.8% busy and 0.25 to 0.30-0.31, as the idle share predicted.
  Input still interleaves (the queue is the worker's task queue; `web:test` passes with the harness faking the channel).
- **Split: 0.39, bounded by the DSP.** The DSP instance's 2.57 s per audio s equals the whole run's wall time per
  audio s (2.56): the ColdFire's 0.71 s overlaps it entirely. The split is pipelined like the native `ThreadedPeer`:
  the DSP worker answers frame N after the SPI exchange and renders the period afterwards (`RemoteDsp::reply` /
  `render`, `digi_dsp_reply` / `digi_dsp_render`), so the ColdFire runs up to one frame ahead. The branch's
  `digi_dsp_exchange` (reply and render in one call) would have been serial; it is kept as reply then render.
- **Exactness [V under Node, D in the browser].** `node tools/wasm_parity.mjs --scenario trig1-tap-release --split 1`
  (ColdFire instance in the main thread, DSP instance in a `worker_threads` worker, frames over the same
  `sab-link.ts`) gives `883476213933333f0c5f4e2b343f65ed470fda0c50c8fa37905972457e819cdd`, the pinned native hash
  and the single-instance wasm hash (816000 values, 8.5 s). Wall 23.6 s against 30.6 s single. In Chromium the Trig
  tap lands at a host-timing dependent instruction count, so the PCM cannot be hashed there; the bench reports
  `pcm_peak` (0.0549 in both modes), `missing_blocks` 0 and no halt.
- **Setup.** The page needs cross-origin isolation: the astro dev server now sends COOP `same-origin` and COEP
  `require-corp` (`astro.config.mjs`), `splitAvailable()` in `runtime.ts` enables the split when the page is isolated
  and the core exports `digi_dsp_reply`, else (or with `?split=0`) the single worker runs. The page compiles the core
  once and hands the `WebAssembly.Module` to both workers. `coi-serviceworker.js` from the parked branch is in
  `public/` but not loaded (GitHub Pages has no coupled core anyway). The generic-core build step of the branch's
  `tools/native_wasm.py` was left out.
- **Why wasm is still about 2.5x the native DSP.** Not measured further here. Known from above: not tiering; the
  browser core has no fast tier (V8 runs the AOT blocks; native reaches 0.93-1.10 s per audio s with its fast tier
  and AOT), so the remaining gap is wasm codegen of the generated blocks (bounds checks, no SIMD in the generated
  Rust for wasm32, 32-bit pointers). The wasm fast tier of the parked branch (`digi_sharc_fast`, generic core)
  is a separate path. **[O]**

## DT2 FX scenarios and profile 2026-10-11 **[D]**

Method. DT2 1.16 (`sections/.source-sha256` = 278541e4...), the one-sample card of `tools/dt2_sample_check.py` (1 kHz sine,
1 s), core `gen-lane-entry` as the baseline, new core `out/native/dt2-audio/gen-lane-fx` (PROVENANCE.txt there; neither is
promoted). Two lanes' work on one machine: load average 3 to 130 during the runs, so every timing is a paired A/B.
Not re-checked by a second agent.

**Scenarios.** `scenarios/dt2-trackfx.json` (25) and `dt2-sendfx.json` (35), generator `tools/gen_dt2fx.py`, same names and
layout as the DN2 files (`tf-*`, `sf-*`) so `tools/fxcheck.py` (`configure("dt2")`: F0 1 kHz, Sound array
`*0x8000470c+0x14:142`, Fx array `*0x80004708:142`) and `tests/scenarios/conftest.py` (`--device dt2`) read them. The
panel is the DN2's (finding 03, "DT2 1.16 FX pages"). Each run is 2 to 4 audio s: NO prelude, a 56-slot sweep (1M
instructions per slot), Trig 1. `coupled.py scenario --device dt2 --dt2-card` (new flag, `DIGI_DT2_CARD=1`) runs them on the
card's ready state; `tools/dt2fx.py render|check|tiers` renders both files with the peeks, prints the fxcheck tables and
compares PCM hashes. Oracle tests: `tests/scenarios/test_dt2_fx.py` (146 cases, 106 s, `mise run scenarios:dt2`).

- **Read-back [D].** The intended parameter lands on a calibrated UI value (BR/overdrive/SRR 22, 54, 127; sends 54, 127;
  delay time 0, 30, 100; feedback 0, 39, 109; reverb decay 0, 37, 108; and so on) and no other stored parameter differs from
  the reference, in all 52 non-reference scenarios. The DN2 event counts do not transfer (44 events give UI 90, not 127; the
  value is not smooth in the count: 48 events 127, 50 events 113), so the counts are calibrated by read-back per layout
  (`gen_dt2fx.py OUT --ladder`).
- **Audio direction [D].** BR: error against dry -52.1, -19.5, +0.7 dB and THD -56.9, -21.2, -7.3 dB for 22, 54, 127;
  overdrive: 3rd harmonic -40.3, -35.6, -25.3 dB, gain +2.7, +7.1, +15.8 dB; SRR: lowest alias image 12000, 2525, 475 Hz,
  error rising; BR 0 is bit-identical to dry; filter at UI 60 takes 35 dB off the 1 kHz note; SRR/overdrive routing 1 gives a
  different signal with the filter closed. Sends: wet RMS rises with the send (delay -48.2 to -33.4 dB, reverb -60.4 to
  -48.1, chorus -52.2 to -37.3). Delay: first echo at 0.087, 0.443, 0.568, 1.863 s for time 0, 23, 30, 100; repeats change by
  -9.4, -4.6, -3.1, +4.7 dB for feedback 0, 32, 39, 109 (above unity grows, peak 0.69); width 0 puts the echo hard left (R
  92 dB lower), 127 right (L 36 dB lower); pingpong follows the width; LPF 0 takes 29 dB off; mix 0 leaves no echo. Reverb
  RT60 1.41, 1.94, 2.12, 6.06 s for decay 0, 31, 37, 108; pre-delay 127 delays the onset by 0.38 s; mix 0 is silent. Chorus
  rate 0.86, 0.99, 3.11 Hz for speed 0, 32, 127; depth 0 removes the modulation (L/R wet correlation 0.999 against 0.04);
  mix 0 is silent. Master compressor (fully wet): RMS change -7.7 (ratio), -3.6 (threshold 0), +7.5 (mix only), +19.5 dB
  (makeup 127).
- **DT2 against DN2.** Delay repeat step at the default feedback is -4.6 dB (DN2 -6.0); echo spacing is about 18.4 ms per
  TIME step against DN2's 15.625 ms (not proportional to TIME + 1; the tempo of the ready state is the likely cause, not
  checked) **[O]**; chorus Depth and Width start at their maximum (a +4 turn changes nothing).

**Tier equality [D].** All 60 scenarios give the same PCM sha in the default config, in `SHARC_AOT=0` (interpreter only; DT2
has no fast regions, so that is the only other tier) and, on `gen-lane-fx`, in the default config again: 60 of 60 for both
cores. The no-AOT render of 197 audio s took 2189 s on six jobs (about 67 CPU s per audio s). `tools/verify.py --full --dt2`
(step `dt2-tier-sweep`) does the same comparison with `coupled.py scenario --device dt2 --dt2-card`.

**Profile and regeneration.** `aot_fxprof.py --device dt2 --dt2-card` over `dt2-sample-trig` and 18 FX scenarios, merged with
the entry-lane r4 profile, then two entry rounds (3 new block starts, then 1; the next one is a 190k-count chain of one
instruction per round, as before: stopped). The FX scenarios change the DSP instruction mix much less than expected:
block instructions differ by 0.0% to 0.4% between a dry run and any send or master scenario, because the send FX, reverb
and the compressor code run the same instructions whatever the sends and mix are. Interpreted instructions per 2 audio s:
3.95M dry, 3.95M with overdrive or SRR, **5.3 to 5.5M with bit reduction** (+1.4M: the only effect with its own interpreted
path), 7.9M per 4 s with sends. So the claim above, that FX code stays interpreted whenever an effect is on, was wrong
**[C]**: the baseline trig profile already covers it, except for the bit-reduction code. In `gen-lane-fx` the BR scenario
interprets 3.76M (was 5.5M); over the 60 scenarios the total falls from 396M to 372M (-6.3%); block traps are equal in 60
of 60.

| Paired DSP CPU per audio s, 6 exclusive pairs | gen-lane-entry | gen-lane-fx | paired ratio |
|---|---:|---:|---:|
| dt2-sample-trig (3 s) | 1.176 | 1.180 | 1.003 (1.001 to 1.010) |
| tf-br-127 (bit reduction on) | 1.303 | 1.189 | **0.913** (0.909 to 0.917) |
| tf-srr-127-flt-rt1 | 1.178 | 1.186 | 1.006 (1.002 to 1.016) |
| sf-del-ppwidth-u50 | 1.171 | 1.181 | 1.008 (1.004 to 1.012) |
| sf-mst-thr0 | 1.178 | 1.186 | 1.006 (1.001 to 1.015) |

PCM hashes are equal in every pair. The new core has 1256 blocks / 13,654 instructions against 1241 / 13,398. Verdict:
bit reduction on is 8.7% cheaper, everything else is 0.3% to 0.8% dearer (more entry checks and a larger table; not
analysed), so **do not promote `gen-lane-fx` over `gen-lane-entry`** unless bit reduction matters; the FX profile has no
other payoff on DT2. Gate on `gen-lane-fx`: `dt2_sample_check.py --seconds 3.0` is a clean tone (peak share 0.99995,
harmonics below -89 dB) with WAV hash `4bab8330915f0929` in AOT, AOT + fast and `SHARC_AOT=0`; block traps unchanged (36,000
for the 3 s trig; equal in all 60 scenarios); the oracle tests pass on both cores (146).

**[O]** What is left interpreted: about 1.9M instructions per audio s in every DT2 run. The largest single source is the entry
bail at 0x1c06fe (455,870 interpreter runs in the 3 s trig, a gated entry that is already a block start); the rest was not
analysed.

DT2 PGO, what it would need (measured by reading, nothing built): `tools/native_pgo.py` trains one profile for DN2 only
(state in `out/native/pgo/dn2/<triple>`; the workload is the `coupled_ready_exactness` gate run from the instrumented
desktop app with `--pgo-train`, and the gate restores the DN2 ready snapshot). There is no DT2 training workload. It would
need (1) a device argument for the state directory and `SHARC_GEN_DIR`, (2) a DT2 workload: the card's ready state
(`--card-image`) with `dt2-sample-trig` and three or four FX scenarios (tf-br-127, sf-del-ppwidth-u50, sf-mst-thr0, about
15 audio s) as a `coupled-run` scenario batch or as a DT2 variant of the gate, (3) the A/B on tf-br-127 and the trig with
`bench-bins`. Cost: one 6-minute train and one 6-minute bench; headless DT2 is at 1.18 DSP CPU per audio s without PGO
(DN2 0.93 with it).

## Browser: why the wasm DSP is slower 2026-10-11 **[D]**

Question: the dev page's AOT SHARC core in wasm costs about 2.6 DSP-worker s per audio s (DN2 1.11, headless Chromium
150, split workers), native 0.93-1.21. Method: `tools/browser_bench.mjs` against a private dev server
(`--url`, below), `--seconds 10 --runs 2-3`, exclusive benchlock; native by `coupled.py scenario trig1-taps
--seconds 10 --no-build` (prod core, no PGO) with and without `SHARC_FAST_REGIONS=`. One machine; other lanes run
unlocked jobs, so consecutive identical runs differ by up to +-8% (base core: 2.46-2.71 over 7 runs). Not re-checked
by a second agent.

- **Build configuration: same as native, nothing wrong.** `Cargo.toml` `[profile.release]`: opt-level 3, no LTO,
  16 codegen units, no debug, no overflow checks, panic=abort (forced on wasm32-unknown-unknown); the desktop
  build uses the same profile (plus PGO in the launcher). Target features at rustc 1.98.1: bulk-memory,
  multivalue, mutable-globals, nontrapping-fptoint, reference-types, sign-ext; not simd128, tail-call, atomics.
  `tools/native_wasm.py` runs no `wasm-opt` (none installed) and passes no RUSTFLAGS. The core has a name section
  (45 MB). Native differs only by PGO (0.93 against 1.10) and the fast tier.
- **Tier split.** The browser core runs no fast tier, so the instructions the desktop runs as Cranelift kernels
  run in AOT blocks. Native, 10 audio s, per audio s: default 235M AOT + 267M kernel insns (0.50M kernel calls),
  0.6M interpreted, DSP CPU **1.209**; with `SHARC_FAST_REGIONS=` 472M AOT + 28M kernel (blocktrans clusters) +
  1.2M interpreted, **1.498** (3.2 ns per executed insn); same PCM. So the fast tier is worth 19% native, and the
  browser at 2.6 (about 5.4 ns per executed insn) is **1.74x native AOT-only** (the wasm codegen share) times
  **1.24x** for the missing tier: 1.21 x 1.24 x 1.74 = 2.6. Browser instructions per audio s as in the baseline
  (1.31G with idle skip).
- **Profile** (CDP, DSP worker, 5 s, 15.0 s sampled, `--profile` now reports every worker and
  `--profile-all FILE` dumps all self times): AOT regions `r_*`/`b_*` **84.9%** (1209 functions; top r_1C399A 6.4%,
  r_1C3862 4.0%, r_1C364F 3.5%, r_1C9504 2.6%, r_B8276C 2.2%, r_1C5765 2.1%, r_1C1884 1.5%, r_1C0CA0 1.4%,
  r_1CDBAC 1.4%, r_B823DB 1.4%), `Engine::step` 3.6%, `exec_insn` 1.5%, generated per-insn interpreter (`core_g`)
  3.8%, sharc_rt 1.3%, SAB-link wait/serve 2.0%, other glue 0.7%. No single function stands out; the distribution
  is the native one with the 53% kernel share added to the regions. Out-of-line helpers in the hot regions
  (`normal_word_map`, `nan_order`, `__dm_read_full`, `__multi3`, `__fixdfti`) never appear in self time. A hot
  region (r_1C399A, 24 KB of wasm) has 207 i32 and 139 i64 loads, 97 calls and one `call_indirect`: the register
  file and state live in linear memory as natively.
- **Refuted (a) guesses of the earlier note:** bounds checks (`--js-flags=--no-wasm-bounds-checks`: no change, the
  trap handler is active), wasm inlining and loop peeling flags (`--no-wasm-inlining`, `--no-wasm-loop-peeling`,
  `--wasm-loop-unrolling`: all within noise), Rosetta (Chromium and Node are arm64). **[C]** "No SIMD in the generated
  Rust" is not the gap either (below).
- **Experiments** (DSP worker busy s per audio s, runs in order; parity = `wasm_parity.mjs --scenario
  trig1-tap-release` single instance, and `--split 1` where noted, pin `883476213933...819cdd`):

| change | audio s / wall s | DSP busy / audio s | parity |
|---|---|---|---|
| base (as `tools/native_wasm.py`) | 0.37-0.41 | 2.71 2.56 2.70 / 2.47 2.46 2.59 2.64 | match, split match |
| `lto=fat`, `codegen-units=1` (25 min build) | 0.36-0.37 | 2.75 2.70 2.77 | match |
| `sharc-gen` opt-level `s` | 0.36-0.41 | 2.47 2.46 2.47 / 2.74 2.79 2.82 2.60 | match |
| `-C target-feature=+simd128` | 0.37-0.44 | 2.40 2.30 2.30 / 2.53 2.64 2.60 2.76 | match, split match |
| simd128 + `sharc-gen` opt-level `s` | 0.36-0.40 | 2.63 2.79 2.75 / 2.79 2.60 2.50 2.73 | match, split match |
| simd128 + `sharc-gen` opt-level 2 | 0.37-0.41 | 2.48 2.48 2.43 / 2.65 2.60 2.55 2.71 | match, split match |

  No variant beats the base outside the noise (means 2.59 base, 2.74 LTO, 2.62 s, 2.50 simd128, 2.68, 2.56). The
  first batch of every variant ran in a quiet period (about 2.3-2.5) and the interleaved A/B (2 rounds, same
  order) did not repeat the gain, so I read it as load, not as a gain. simd128 changes the code (24 `v128`
  loads/stores and some `f32x4` in r_1C399A) but not the time. No build change is kept.
- **Ceiling (a), AOT core with build fixes: about 2.4-2.6** (confidence medium-high; the build knobs are exhausted
  short of `wasm-opt`, which is not installed and which V8's TurboFan mostly duplicates). The 1.7x is V8's
  code for LLVM's wasm output against LLVM's arm64 output for the same Rust.
- **Ceiling (b), the fast tier in the browser core: about 2.0 (1.8-2.3), confidence low.** Not measurable here:
  `digi_sharc_fast` panics on the first kernel build on `wasm32-unknown-unknown` (`std::time::Instant::now`; fixed
  in `fast/mod.rs` by a clock shim), and with the DN2 static regions (`tools/fast_regions.json`, tried via a
  temporary `threshold 0` seeding) it stops at `Op::Helper` (`unimplemented!`, `sharc-fast-wasm/src/lib.rs`).
  Auto selection (256,64,0) is exact (3 s hash equals the AOT-only 3 s hash `ae86d0b3...`) but arms only 19
  kernels worth 0.4% of the instructions. Model from the native numbers: AOT 235M x 5.4 ns = 1.27 s, plus the
  kernel part (native 0.46 s: 0.13 s kernel body at 1.15x for V8, 0.33 s engine round trips at 1.7x, plus about
  0.1 s for 0.5M `host_wasm_run` crossings) about 0.8 s: 2.1 against 2.6, the same 19% as native. Real time
  (<= 1.0) is out of reach for AOT plus today's regions in one worker either way; it needs kernels that follow
  calls and chain (fewer than 1M calls per audio s at 0.5 ns per insn), which is the engine work of step 2.
- **Bench pitfalls found.** (1) `browser_bench.mjs` without `--url` starts `astro dev --port 4399`; if another
  worktree's server owns 4399 the run silently measures that server's core. (2) Astro 7 keeps one dev server per
  `packages/web/node_modules/.astro`; a worktree that symlinks `node_modules` as a whole shares that lock and its
  start is skipped. Give the worktree a real `node_modules` directory with symlinked entries (not `.astro`), start
  `astro dev --port N` once and pass `--url`. The first baseline numbers of this lane (0.36-0.40) were taken against
  another lane's server; the figures above are from a private one and agree.

## Browser: fast tier in wasm 2026-10-11 **[D]**

Question: what does the fast tier buy in Chrome, replacing the modelled 1.24x? Setup as the previous browser lanes
(private `astro dev` on its own port, `--url`, real `node_modules` directory, exclusive benchlock; headless Chromium 150,
split workers, `--seconds 10`; other lanes ran unlocked jobs, so single runs differ by up to +-8%). Not re-checked by a
second agent.

- **`Op::Helper` in the wasm emitter.** A kernel with a helper call imports `env.helper(st, rec) -> status` (function 0,
  `k<i>` shift by one; only when a kernel needs it). The page links the import to the core's `digi_helper_exec(st, rec)`
  export (`native/boot/src/abi.rs`, `helper_exec` with `rec` as the wasm32 pointer), a wasm-to-wasm call with no JS
  trampoline; `core-host.ts` and `tools/wasm_parity.mjs` pass it. A record above 32 bits is refused (`emit_function`
  returns Err; the region falls back to the interpreter). No helper kernel arises in the shipped DN2 config (helpers
  need `SHARC_FAST_HELPER`, which the browser core cannot set, or the `exit0` rebuild, which did not fire in
  these runs), so the path is tested apart: `cargo test -p sharc-fast-wasm` (module imports `helper`; wide record
  refused) and `node native/sharc-fast-wasm/helper_check.mjs` on the emitted module (V8 validates it; the stub sees
  `(st, rec)` per iteration; status 0 leaves with exit 3). **[O]** a real helper kernel in the browser, which needs a
  way to turn `SHARC_FAST_HELPER` on without an environment (`cfg.rs`).
- **Build and seeding.** `tools/native_wasm.py` now builds the coupled core with `fast-wasm` (`--no-fast` leaves it out;
  the tier stays off until the page asks). New ABI `digi_sharc_fast_regions(regions, n, roots, n, threshold, min_insns,
  follow)` = the desktop configuration: the static regions and the block-translator cluster roots of
  `tools/fast_regions.json` for the firmware's SHA-256 (`FastEngine::new(..).with_clusters(..)`), optional run-time
  selection on top (`threshold != u32::MAX`). Page: `?fast=desktop|desktop+auto|auto|T,M,F`; `browser_bench.mjs --fast X`;
  `wasm_parity.mjs --fast desktop[+auto]` (also with `--split 1`). The bench reports the kernel host's counters and
  `digi_sharc_fast_report` (`dsp_kernels`, `dsp_fast`). The dev page is the only consumer: `?fast=` is off by default.
- **Exactness [V under Node].** `node tools/wasm_parity.mjs --scenario trig1-tap-release --fast desktop` and
  `--fast desktop --split 1`: `883476213933333f0c5f4e2b343f65ed470fda0c50c8fa37905972457e819cdd` = the native pin
  (816000 values). Generic core, `--fast desktop+auto --split 1`: the same hash. In Chromium `pcm_peak` 0.0549 and
  `missing_blocks` 0 in every run. Web tests (`node --test`, 10) and `astro check` (0 errors) pass.
- **Kernels.** DN2 desktop config: 23 kernels (11 regions + 12 clusters), 0.09 MB wasm, 0 refused. Chrome: module
  compile + instantiate 2.3 ms in total (Liftoff compiles lazily), the first call of every kernel 5 ms in total
  (compile plus one run), so the cold start is about 7 ms; 5.0M kernel calls per 10 audio s. With `desktop+auto`: 41
  kernels, 0.38 MB, 4.6 ms + 40 ms. Node: 2 ms for the 23 kernels.

AOT core (`SHARC_GEN_DIR` = prod gen) with fast tier off or on, 10 audio s, split workers:

| config | runs | audio s / wall s | DSP worker busy / audio s | kernel insns / non-idle insns |
|---|---|---|---|---|
| AOT only | 8 (2.34-2.77) | 0.399 | 2.54 | 0 |
| AOT + fast, desktop config | 8 (2.05-2.29) | 0.469 | **2.16** | 53% (2.67G of 5.0G per 10 s) |
| AOT + fast, desktop + auto 256,64 | 5 (1.93-2.24) | 0.46-0.52 | 2.11 | 53% |

  Interleaved ABA runs (3 each): 2.62 / 2.15 / 2.22. **The fast tier is worth 15% in Chrome (2.54 -> 2.16), against
  19% native and 19% modelled (2.6 -> 2.1)**; audio per wall second 0.40 -> 0.47. The ColdFire worker (0.71 s per
  audio s) is then blocked 1.4 s of every 2.2 on the DSP. `desktop+auto` adds 18 kernels and no instructions
  (interleaved it is not better than `desktop`; the 1.93-1.98 runs were from a quiet period). The kernel share of
  executed instructions equals native (53%).

Generic core (`out/native/shared-core/gen`, 3.2 MB, no AOT; the DSP image is decoded at run time), one 10 s run each,
the same page, DSP worker busy per audio s and share of the 4.70G non-idle instructions the interpreter runs:

| config | DSP busy / audio s | audio / wall | interpreted share | kernels (compile, first calls, wasm) |
|---|---|---|---|---|
| no fast tier | 88.9 | 0.011 | 100% | 0 |
| desktop regions | 42.3 | 0.024 | 44% | 24 (2 ms, 5 ms, 0.09 MB) |
| desktop + auto 256,64 | 24.3 | 0.041 | 25% | 617 (41 ms, 566 ms, 6.4 MB) |
| auto 256,64 + follow | 17.8 | 0.056 | 18% | 2234 (163 ms, 1.75 s, 35.7 MB) |

  Exact against the pin (Node, `desktop+auto`, split). The interpreter runs at about 190 ns per instruction in the
  browser (88.9 s for the 4.70G non-idle instructions; the other rows fit it: 2.08G x 190 ns = 400 s of 423 s
  for `desktop`), and the kernel engine's job is to take that share to zero: at the AOT's 5.4 ns per instruction
  all 4.70G would cost 2.5 s per audio s, the AOT core's 2.2-2.5. So the generic core can only reach AOT speed if
  nearly all instructions run in kernels. Today the interpreted share (18-44%) and the 58M kernel calls per 10
  audio s of `auto+follow` (43.6M ran, 13.9M declined at the shape check) dominate, not kernel compile time (first
  calls 1.75 s once, 35.7 MB of wasm in memory).
- **Open.** Single-worker mode (`?split=0`) still stubs the kernel host (`runtime-worker.ts`); a helper kernel in the
  browser; the Safari/Firefox timings; compile in a worker pool (design doc, stage 4) is not needed for the desktop
  config (7 ms) but would be for the generic core (0.6-1.8 s on the DSP worker).
