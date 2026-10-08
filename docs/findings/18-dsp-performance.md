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
- Mask-free AOT emission **[O]**: predicted AOT 70 -> 22% of the DSP thread, 4.4 ->
  ~1.5 ns/insn, default 1.41 -> ~0.75; +-30%, 1-2 weeks, changes the transpiler V type.
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
shared core, opt-in `SHARC_FAST_FLATAUTO=1` (config suffix `+flat` in `tools/fastcov.py`). Not
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
- Left in these functions (batch 2): `2a_short` compute cu=0 opcode 0xad at 0x1c4fa3, 0x1c516b,
  0x1c5186; multifunction category 0x1e/0x1f at 0x1c5060 (3a), 0x1c553a, 0x1c5547; `8a_rel` with a
  compute in the delay slot at 0x1c55c5; in `FUN_1c244d` the 17a/4a/18a loop stops at 0x1c2583,
  0x1c25ae, 0x1c25bd and 0x1c252d. Interpreted share is now 0.99%; the top remaining are
  `FUN_1c0c76` (1.1M), `FUN_1c07c6` (0.87M), `FUN_1c06ba` (0.69M), `FUN_1c0a6f`, `FUN_1c02a4`.

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

DT2 AOT and the DT2 render are in finding 15.
