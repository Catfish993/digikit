# 18. DSP performance: measurement, cost model, state at freeze

Branch `work/sharc-emulator` at 911b1fc (performance work frozen for cleanup and
merge). Sources: lane notebooks `docs/notes/laneB.md` (fast tier, perf tool, call
path, helpers, chaining) and `docs/notes/laneF.md` (generated lowering, AOT
profile, class (e)). Marks as in CLAUDE.md: **[V]** measured here at 911b1fc,
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
