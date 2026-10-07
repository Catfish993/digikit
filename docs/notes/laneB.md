# Lane B notebook (fast tier for everything)

## Coverage command and reference hashes (B-agents: read this first)
Build (prod gen, ~10 s for engine edits; the first build in a new worktree needs tools/worktree-setup.sh and a ~4 min sharc-gen compile):
    uv run python tools/coupled.py build
Coverage run (short, unlocked, deterministic; ~25 s; the fast/interp split is the metric):
    DIGI_EMU_SYX=<repo>/Digitone_II_OS1.11.syx uv run python tools/fastcov.py --no-build --no-lock --diag no --seconds 1.5 --config static-blocks+follow
Diagnostic (stop/exit reasons, 1.5 s, ~4 min with COV): same with `--diag only --top 25`; tools use SHARC_FAST_COV/HIST.
Reference at the diff you were given (static-blocks+follow, 1.5 s): fast 83.0%, interp 17.0%, 11.4 DSP CPU/audio s, PCM 6a79bcc347e1da8e (ok=False is expected at 1.5 s: the gate needs the full run).
Full scenario (timed runs are the integrator's, exclusive lock): trig1-taps PCM 1ca0ae84a2374b13; static-blocks+follow 82.6% fast, 11.25 CPU/audio s.
Other references: default AOT build trig1-taps 1.45 CPU/audio s, PCM 1ca0ae84a2374b13; trig1-tap-release 3 s PCM a0a0f42625dcb87e (patched with lane A's patch: 6a7f5cecc41c9385).
Any change must keep every PCM above. Unit tests: native/sharc `cargo test --lib` with SHARC_GEN_DIR=out/native/shared-core/gen (mise run core).
Do not build into out/native/target*/prod/pgo-use; do not commit.

Scenario trig1-taps (10.5 audio s, DN2 1.11), executed insns 5.27G (idle skip 5.23G more). PCM sha256 1ca0ae84a2374b13 in every run below.

## Step 1 numbers (fastcov)
- default (AOT + verified regions): DSP CPU/audio s 1.44-1.47; fast 2.80G (53%), AOT 2.46G (47%), interp 0.02G.
- SHARC_AOT=0 used to turn the fast tier off too (block_plan returned early). Now it only turns off blocks.
- verified regions + SHARC_FAST_AUTO=256,64, AOT=0: 19.5 s/audio s; fast 3.30G (62.6%), interp 1.97G (37.4%); 539 built, 75 refused, 17.8M calls. PCM equal.
- fast-auto only (no verified regions), AOT=0: 24.4-25.0 s; fast 54.3%, interp 45.7%.
- fc3 (5 s, SHARC_FAST_COV+HIST): of 1.14G interpreted insns, 81% have no stop/deny/refusal at the PC ("no region"); ~5% sit at refused/stopped/denied insns (3a/3b/3c address range unknown, 25a_direct, 9b_abs, 3d, 18a MODE1 write). 621M insns ran between a kernel exit and the next kernel call.
- fc4-fc6: arming kernel-exit landing PCs (SHARC_FAST_LAND=8) changed nothing: coverage 54.3% with or without. RESUME (arm the PC after the interpreted insn that follows an exit): result not read (run killed).
- Lesson: coverage is deterministic; iterate with --seconds 1-2, no lock.

## Bugs found
- SHARC_FAST_COV + auto: index out of bounds in run_inner (cov_last after drop_slot); the DSP thread died and the CF thread spun forever. Fixed (drop_slot clears cov_last); fastcov kills on "panicked at".

## Step 2: static region selection (fastcov --config static-funcs|static-blocks|static-insns)
Entries come from out/sharcdb/dn2-1.11.sqlite at load (SHARC_FAST_REGIONS, `@FILE` for long lists); SHARC_AOT=0; explicit entries are never dropped as unprofitable. 1.5 s runs, no lock; PCM equal to the earlier runs in every case.
| entries | fast % of executed | interp % | built/refused |
| auto 256,64 (no verified regions) | 54.5 | 45.5 | 965/- |
| function entries (1078) | 5.6 | 94.4 | 149/11 |
| basic-block starts + function entries (7.4k) | 77.7 | 22.3 | 896/145 |
| every instruction (138k, lazily built) | 87.5 | 12.5 | 7328/1501 |
Full trig1-taps, static-insns, locked: 13.1 DSP CPU/audio s, fast 87.2%, interp 12.8%, PCM 1ca0ae84a2374b13 (default AOT build: 1.45). 654M kernel calls (8 insns per call): call overhead, not insn speed, dominates.
Remaining interpreted insns (static-insns, top reasons): 18a MODE1 write 14%+, 12a_ureg / loop end in region ~9%, 9b_abs 7%, 25a_direct 8%, 16a 4%, 3a/3b address range unknown ~4%, 3d 3%, refused "end of outer active loop in the region" 4%, cold 6%. Interpreter < 2% needs these forms lowered; selection is no longer the limit.

## Per-range AOT invalidation (done)
- tools/sharc_rsgen.py emits REGION_ENTRIES (entry PCs per generated region) into image.rs; out/native/dn2-audio/gen-ranges was regenerated with the documented command: blocks/spec/core/tables/syms byte-identical to the prod gen, only image.rs differs.
- native/sharc build.rs sets cfg sharc_region_entries; lib.rs verify_code now hashes every CODE_RANGE, finds the bad ones, bars the entries of every region that has an entry in a bad range (120 of 773 regions span several ranges, so the unit is the region) and adds them to the chain-stop set. block_plan drops only barred entries. Old gens without REGION_ENTRIES bar everything as before.
- Verified on trig1-tap-release, 3 s, with lane A's patch (BASE line fixed to the DN2 1.11 sha, DSP_PATCH_ALLOW_FAST=1): PCM 6a7f5cecc41c9385 (= lane A's); DSP CPU 4.53 s vs 3.99 s unpatched (AOT 697M of 701M insns kept); no-op PCM a0a0f42625dcb87e, same on the old and new gen binaries.
- SHARC_FAST_LAND and SHARC_FAST_RESUME removed.
- Lane A's tree files copied into this worktree to run it (boot/src, coupled/src, machine/board.rs): sync only.

## Chaining investigation (first result)
- Exits of static-block regions are almost all stops at unlowerable instructions, not CALL/RTS: of 23.4M kernel exits (1.5 s): 3a "address range unknown" 29.5%, 25a_direct (call) 13.5%, 9b_abs (return) 13.5%, 3d 11.4%, 3b 6.3%, 2a_short 1.9%, 18a MODE1 write 1.6%, 5a_move 1.5%, 8a_rel 1.3%. 55M of 92.7M region calls in static-insns were declines (MODE1 unknown 18.5M, write window not plain RAM 13.8M, pending delayed branch 9.3M).
- Added SHARC_FAST_FOLLOW=1 (fastcov `+follow`): the PC after each stop instruction (not call/jump/loop/delay) becomes an entry. static-blocks 77.7% -> 83.0% fast, 13.7 -> 11.4 CPU/audio s (1.5 s runs). Full trig1-taps locked: 11.25 CPU/audio s, fast 82.6%, PCM 1ca0ae84a2374b13.
- Cluster lowering (guarded dynamic windows, call/return sequences) for every block entry is worse: 53-57% fast.
- Direct chaining without lowering the stop forms cannot help: each stop still ends a kernel. Next: lower 3a/3b with dynamic windows in non-cluster regions, then 25a/9b call/return, then 3d.

## MODE1 (integrator analysis, static-blocks+follow, 1.5 s COV run)
- 18a stop/refusal sites by instruction: MODE1 set 0x200000 (35 sites, 290k hot hits), clear 0x200000 (28, 292k), set/clear 0x1000/0x2000/0x8000/0x10000 (few), IRPTL/STKYX bit ops (3 each). 0x200000 is MODE1.PEYEN: the "MODE1 writes" are SIMD enable/disable around loops (e.g. 0x1c2045 set; DO loop of float adds on DM(I..)+30 pairs; 0x1c2067 clear). Lowering the write itself does not help: the code between the pair is SIMD (PEy executes every compute on the S/SF file with ASTATY and MSF/MSB; memory companions), which the lowering lacks (NREGS=48 holds R, I, M only; glue declines "MODE1 unknown, SIMD or broadcast load").
- Interpreted instructions attributed to 18a (after a stop at it, or an entry refused at it): ~14% of the interpreted ones = about 2.4% of executed = about 9% of CPU (interpreted insns cost about 86 ns each, from the 13.7 -> 11.4 s step when follow removed 40M of them; the interpreter is about 65% of the 11.25 CPU/audio s).
- The other declines are cheap: 15.3M of 43.9M region calls per 1.5 s (pending transfer 10.4M with shape, non-RAM windows 3.1M, MODE1 unknown 0.6M) cost about 20 ns each, about 2% of CPU.
- "No stop recorded" interpreted code (9.7%) is the return epilogues (RETURN + 25c_rframe delay slot): B2's.

## Integration of B1+B2+B3 and the interpreter-cost profile
- Merged B1, B2, B3 in this worktree (tests.rs hunks were re-added by hand). Coverage static-blocks+follow, 1.5 s: fast 87.2% (was 83.0%), PCM 6a79bcc347e1da8e. Full timed trig1-taps: 11.50 CPU/audio s, fast 86.9%, PCM 1ca0ae84a2374b13 (before the merge: 11.25 s, 82.6%). Coverage rose 4.3 points but CPU did not fall: 323M region calls (308M before), more guarded windows and 2846 built regions (1686).
- Unit tests: 57 of 58 pass. `fast_calls_and_returns_match_the_interpreter` (B2) fails once B3 is merged ("call in a loop, chunk 1, step 0": r120 known-mask: the fast tier leaves bit 26 known where the interpreter has m=0). It passes with B3's source hunks reverted. Value-equal, mask metadata differs; B3 (loop state) or B2 needs to reconcile.
- Profile (sample, DSP thread, 4 s of trig1-taps, static-blocks+follow; 257M interpreted insns, 47.3 DSP CPU s): interpreter-related frames about 65% of the thread: Engine::step self 16% (about 29 ns per interpreted insn: gate checks per iteration), exec_insn self 14% (about 25 ns), generated core_g semantics 36% (about 66 ns: compute, 4a/15b/3a/3b transfers, SIMD companions, loop-register sync) = about 119 ns per interpreted instruction. Region-call overhead (run_cfg, resolve_window, window, run_inner, wants, memmove) about 25%; kernel code about 10%. Decode is not in the profile (decode cache keyed on dec_gen already exists): a decode cache gains nothing.

## B+ test fix (B2+B3): 58/58
Cause: the fast tier applied the DO's STKYX bit-26 event as "known 0" even where the interpreter keeps the bit unknown (the generic push keeps the known mask). Fix in glue.rs: a DO-only event (stk26 == 1) clears bit 26 only where it was known. Patch vs ac04ac5 (only B1-B3 + this fix, 8 files): scratchpad/bundles/B123.patch. Coverage after: fast 87.2%, PCM 6a79bcc347e1da8e.

## Browser architecture data (2026-10-07)
- Existing prior art: native/sharc-jit (P1/P2): AOT-generated code compiled to wasm runs 2.9-3.1 ns/insn in V8 vs 1.9 native (x1.55); the run-time translated wasm (P2) was 2.5-3.3 ms/frame vs 0.63-0.72 ms/frame for AOT-as-wasm, paused.
- Browser core today (generic gen, no AOT, no fast tier; built in 15 s): `idle` scenario, 1.0 audio s under Node/V8 = 140 s wall (132M CF insns, 4.15G DSP insns incl. idle skip). cpuprofile of the run: sharc_gen+sharc_native (the DSP interpreter) 97%; ColdFire+machine+periph+boot glue about 1.6 s per audio second (coldfire 1.04, machine 0.12, periph 0.05, boot 0.45), against 0.97 s/audio s natively for the same scenario (x1.65).
- DSP budget arithmetic: about 0.5G instructions executed per audio s (idle skip is free). An interpreted instruction costs 119 ns (profile above), a kernel instruction about 0.5-0.8 ns. With I the interpreted share, DSP s/audio s = 0.5G * (I*119 ns + (1-I)*0.8 ns): 13% interpreted = 7.7 s; 1% = 0.65 s; 0.5% = 0.4 s. A concrete interpreter at 15 ns would allow I = 5%.

## Wasm backend and V8 gate (2026-10-07)
Reference at 0414505: static-blocks+follow 1.5 s PCM 3bf3ba3026953c22 (the earlier 6a79bcc347e1da8e predates lane 0/A/E changes in main); default AOT config gives the same hash.
Code: native/sharc-fast-wasm (own workspace root, `native-bench` feature for the Cranelift column): `emit_function`/`emit_module` (one module per region or many, `env.memory` imported, kernel `(ctx, st) -> code`), `WasmBackend: KernelBackend`; bins `wasmgen` (dump -> .wasm) and `clbench` (native replay); `gate.mjs` (V8 replay). Capture: SHARC_FAST_DUMP=DIR (native/sharc/src/fast/dump.rs; hooks in glue.rs/mod.rs; PCM unchanged with it on). Tools: scratchpad scripts capture.sh/gaterun.sh (to move into tools/ if kept).
Capture: 757 kernels, 125 real calls of 60 regions covering 73.9% of fast-tier instructions (hottest: 0x1c399a 17.5%, 0x1c3862 12.6%, 0x1c3646 11.1%, 0x1c5765 6.7%, 0xb82767 5.2%).
Results (40,265 captured instructions; setup cost subtracted):
- Cranelift native: 0.49 ns/insn, compile 1.7 ms/kernel.
- wasm in V8, TurboFan: 0.56-0.58 ns/insn (x1.15-1.2); compile 1.06 ms/kernel (--no-liftoff); default tiering 0.556 after warm-up.
- wasm in V8, Liftoff only: 2.96 ns/insn; compile 0.22 ms/kernel (the speed a patch runs at until TurboFan finishes).
- Rust-side emit: 104 us/kernel (757 kernels, 1.8 MB module, 14 ms in one module).
- Exactness: wasm and Cranelift give identical results on all 125 calls; both match the engine's recorded post-state on 118; the same 7 calls (second captures of kernels, guard exits) differ from the recording in the replay, in both backends (a capture/replay fidelity issue, not a backend one).
Gate (<= 1.2 ns/insn): PASS (0.58).
