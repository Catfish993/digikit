# sharc-jit: run-time translation for the browser DSP (follow-up spike)

Status: planned, not started. Reference branch `lane/jit` (a2292d7, 4637c61, fcd0d4b) is kept unmerged.
Results so far are in docs/findings/18-dsp-performance.md, section "Firmware-independent DSP in the browser".

## Why

The GitHub Pages build runs firmware the user uploads, so it cannot ship the per-firmware AOT core: that core is
firmware-derived and matches one DSP image only. The page runs the generic (shared) core instead.

| Path (DN2 1.11, Chrome 150) | DSP s per audio s |
|---|---|
| Generic core, fast tier at 4,2,1, faster interpreter (today) | 5.8 |
| Per-firmware AOT core, for reference (cannot ship) | about 2.0-2.2 |
| Goal of this spike | about 2.5-3 |

At 4,2,1 the fast tier runs about 94% of instructions in kernels. The rest falls to the interpreter, at about
5.8% of instructions and about 72% of DSP time. sharc-jit translates general code, including the forms the
fast tier refuses, at about 10-12.5 ns per instruction in V8. The interpreter costs about 220 ns per
instruction in Chrome.

## What exists

- `native/sharc-jit` on `lane/jit` is a partial evaluator over the generated generic core.
  - It works on the engine's own `St`, with field offsets from a layout export.
  - It emits one wasm module per hot region with `wasm-encoder`.
  - It is exact on 91/91 frames with the models off.
  - It runs about 12.5 ns per instruction in V8, and 10 with the loop-back fix and wasm-opt.
- fcd0d4b wires it into the engine:
  - a wasm32-only jit tier after the fast tier, feeding `fast_pc`, so `block_plan` gating applies;
  - the translator linked into the core;
  - the 21 `jit_*` helper exports;
  - `host_jit_compile`/`host_jit_run` in core-host and wasm_parity.
  With the tier on but inert, PCM equals the pin.

## Blockers found by the slice proof (FUN_1c8ef1, 16% of interpreted instructions)

1. **Models.** The translator is exact only with the opt-in models off: stack, bank, core timer and
   peripheral. The coupled DN2 run has all four on. With them on, it refuses `loop_depth` stores and the
   `pc_stack` ops. FUN_1c8ef1 always runs inside a DO loop, so 0 regions were entered.
2. **Translation cost.** About 2.7 ms per SHARC instruction in V8 (75.5 s for 274 regions). A whole
   firmware would take minutes on first load.
3. **V8 limits.** 58 of 274 regions exceed V8's 50k-locals limit.

## Plan, in stages. Each stage has a measured gate; stop on a miss.

1. **Model-aware translation** (about 2-3 agent-days).
   - Add `loop_depth` and the loop-slot state, the `pc_stack` helpers and ops, and the
     `_pc_stack_complete`/`_bank_complete` boundaries, `timer_written`, and trap-undo of `loop_depth`.
   - Oracle: the PCM pin (`wasm_parity --scenario trig1-tap-release --fast 4,2,1`) plus a replay compare
     against the interpreter.
   - Gate: FUN_1c8ef1-only translation enters regions, stays exact, and saves at least 70% of the modelled
     ~0.9 s per audio s.
2. **Region size cap for V8** (about 0.5 day). Split or limit regions so no function exceeds the locals limit.
   Gate: 0 refused regions.
3. **Translation speed** (about 1 day to profile, then fix).
   - Profile the partial evaluator.
   - Targets: translate off the DSP thread (a compile worker, as in docs/design/fast-compile-offload.md);
     cache translated modules in IndexedDB keyed by firmware hash; then optimise the evaluator itself.
   - Gate: first load of a firmware under about 10 s, and later loads from cache.
4. **Translate everything** (no allowlist).
   - Measure DSP s per audio s in Chrome against the 5.8 baseline.
   - Gate: at most 3.5 to continue to integration.
5. **Integration.** Merge behind a page option, then make it the generic-core default after paired runs in
   Chrome, Safari and Firefox. Add a browser PCM pin.

## Risks

- V8 codegen for large, call-heavy wasm. finding 18 measured spills and constant materialisation at about 2x
  native instructions, so the floor may be about 5 ns per instruction rather than 2.5-3.
- The translator parses generated Rust source text at start-up, which costs memory and start-up time.
- The overall confidence is about 35% for reaching about 2.5-3 DSP s per audio s.

## Cheaper alternatives to weigh first

- Fast-tier coverage. Hours each:
  - make the 17th jump a stop instead of a refusal (MAX_CFG_JUMPS);
  - add the `compare on PEy` and `MR data move` lowerings;
  - diagnose the 32% "no stop recorded" pool.
- More interpreter trimming: a `block_plan` early-out and the `exec_insn` wrapper.
- Together these are estimated to reach about 4 DSP s per audio s, with medium confidence.
