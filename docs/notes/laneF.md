# Lane F notebook: generated lowering (full-coverage translator)

Worktree `.claude/worktrees/agent-a17039a5984f10165`, base 0414505. Nothing committed.
Goal: any .syx runs in the browser, so the fast tier's lowering must not be
hand-written per form. This lane generates it from `tools/sharc_core`.

## Design (checkpoint 1)

One semantics source, three layers.

1. Form structure (generated). `tools/sharc_lowergen.py` runs the real
   `sharc_core.forms` handlers, unmodified, over a tracing machine state:
   - The decoded fields and the lowering context (MODE1, `assume_nw32`) are
     static symbols (`S`): arithmetic builds expressions, a test on one forks the
     trace (replay of the handler with the decision scripted), a dict lookup or
     index pins the fields it needs (enumeration, at most 4096 values). Each leaf
     carries a path of decisions. Register-class tables (`_cureg_code`,
     `_lw_pair_mate`) are lifted by cases over the field's whole domain
     (`lift_pure`), so a 7-bit register field becomes a few range tests.
   - Register contents and memory words are dynamic nodes (`D`): a register read
     is `Const(D)`, so the unchanged `values.py` helpers build the expression
     DAG (`_add`, `_bitwise`, ...). A data-dependent branch raises `Stop`
     (refuse): the form-structure layer has none.
   - Intrinsics (small, per primitive not per form) record effects in program
     order: register read/write, load, store, post-modify, condition, apply
     compute, apply shift-immediate. The trace never runs `_advance`, events,
     stops or forks.
2. Emitter (`tools/sharc_lowergen_rs.py`). Leaf programs are value-numbered
   (hash-consed identities, CSE, DCE, `x & 0xFFFFFFFF` dropped, identity
   register writes dropped). The decision tree is folded: branches share a
   common prefix and suffix, only the differing middle stays in an `if`/`match`;
   chains of `x <= k` become range groups. Output: `native/sharc/src/fast/lower/
   gen_forms.rs`, one `impl Lower` method per form over the primitives of
   `gen_prims.rs`. No firmware, committed-able.
3. Primitives (hand-written once, `gen_prims.rs` plus the existing `Lower`):
   windows, anchors, guards, circular post-modify (`post_value`), conditions,
   register file, flag replay, MODE1. They are the "boundary" of the
   semantics; adding one serves every form.

Mapping: flags stay lazy (`pend_flag(kind, sources)`; replayed at exit by the
transpiled `apply_flag`; PEy writers carry `pe` and replay on ASTATY; the lazy
group state has 3 groups per PE). Conditions: the hand wrapper (`cond_value`)
still gates a conditional form, the generated form sees `cond == 0x1f`;
SIMD conditions refuse. Delayed branches, loops and calls stay in `cfg.rs`
/`flow.rs` (static PC state; the semantics' `_advance` is not traced). SIMD:
`_simd_active(MODE1)` is a decision on the static MODE1, so every form has its
SISD and PEx/PEy leaves; companions are the semantics' own
(`_simd_ureg_mem_companion`). MODE1 is static per program point
(`Lower::mode1_cur`, joins checked in `flow.rs`), written back as register 114
(NREGS 48 -> 128 so S, L, B and MODE1 are kernel registers; L/B writes and
system registers refuse). Memory windows: the generated `load`/`store` carry
no range logic; they call `xfer_at`/`mem_xfer_w` with the index register and
value found from the value's origin tag (`Tag`).

## What is generated now

2a 2a_short 2b 2c 3a 3b 3c 3d 4a 4b 4d 5a_move 5b_move 6a_mem 6b_shiftimm 14a
15a 15b 17a 17b 18a 19a 21a 21c (24 forms, 12.7k lines). Compute bodies still
come from `compute_full`/`compute_short`/`shiftimm` (hand), run per PE
(`pe_off`). `SHARC_FAST_GEN=all|form,...` selects them (default off).

## Tools and tests

- `uv run python tools/sharc_lowergen_rs.py native/sharc/src/fast/lower/gen_forms.rs FORMS`
- `tools/sharc_lowergen_vectors.py` writes random encodings per form
  (`tests_gen_vectors.rs`); `tools/sharc_lowergen_programs.py` assembles
  programs (`tests_gen_programs.rs`; a SIMD loop with the shape of the DN2
  PEYEN loops, written from the ISA).
- `cargo test --lib gen_` (SHARC_GEN_DIR=out/native/shared-core/gen):
  `gen_lowering_matches_the_interpreter` (every vector, MODE1 SISD and PEYEN,
  interpreter vs fast tier after 1 and 4 steps, hand and generated acceptance
  counts), `gen_programs_match_the_interpreter` (8 step sizes).
- `tools/lg_try*.py`, `lg_leaf.py`, `lg_size.py`: trace, print leaves, size.
- `tools/lg_bisect.sh SECONDS FORMS...`: PCM and coverage with a form subset.

## Results (DN2 1.11, trig1-taps, `--config static-insns+follow --no-lock`)

| run | fast | interpreted | PCM |
| 1.5 s, hand forms only | 89.5 % | 10.48 % | 3bf3ba3026953c22 |
| 1.5 s, `SHARC_FAST_GEN=all` | 92.5 % | 7.45 % | 3bf3ba3026953c22 |
| 12 s, hand forms only | 89.5 % | | 8cbae26b73014732 |
| 12 s, `SHARC_FAST_GEN=all` | 92.2 % | | 8cbae26b73014732 |

Other configs (1.5 s, PCM 3bf3ba3026953c22 in every run): `fast-auto` (no firmware-derived selection) 60.8 % -> 63.4 % fast; `default` (AOT) unchanged at 53.2 %.

Interpreted instructions in the 1.5 s diagnostic run: 78.9 M -> 56.1 M (-29 %).
The "18a MODE1 write changes MODE1" stop, 25 % of the interpreted ones before,
is gone. The SIMD loop (MODE1 set, DO of 8 instructions with float adds on
PEx and PEy, MODE1 clear) lowers as one CFG kernel and runs exactly against
the interpreter at 8 step sizes, with and without the bank and stack models,
with random and with normal float data.

Tests: `cargo test --lib` in native/sharc, 60 tests pass with and without
`SHARC_FAST_GEN=all` (the fuzz run alone: 24 forms x 24 encodings x {SISD,
PEYEN} x {plain, bank+stack models} x 2 register states; the table it prints
shows the generated lowering accepting 1.1-5x as many vectors as the hand one).
`pytest tests/test_sharc_lowergen.py` keeps gen_forms.rs current.

Bugs the fuzz found in the generator or the primitives (all fixed): a decision
recorded as an equality when it was a range test; a lifted register-class
table that forked on a range already known; 14a's store dropped because the
semantics test `state.concrete`; the PEy compare shifting PEx's CACC history;
a SIMD vector decline in the glue that hid the first three.

## Measured costs

- Generation: all 50 forms trace and emit in about 8 s (`tools/
  sharc_lowergen_survey.py`); the 24 emitted forms are 12.7k lines of Rust,
  8 s of extra `cargo build` of sharc-native.
- Lowering at load time: 0.66 ms per built region (0.40 ms with the hand
  forms; the regions are bigger now), 8195 regions in 5.4 s; compile
  (Cranelift) 1.7 ms per region.
- NREGS 48 -> 128 grows the kernel context by 640 bytes; no change in the
  call overhead that was visible in DSP CPU per audio second (19.4 vs 20.6-21.1
  noisy, unlocked).

## What is left, by family (interpreted instructions of the 56 M in the diagnostic run)

1. Compute bodies (cu 0/1/2, multifunction, MR moves): about 12 M (21 %).
   Needs the dynamic layer: if-conversion of data-dependent branches (a
   dynamic fork whose leaves merge by `select`), lazy flag intrinsics (record
   the flags.py function and its sources, replay at exit with the transpiled
   function, generically instead of the `FlagKind` table), float intrinsics.
   The tracer already runs `compute._compute` over symbolic registers: the
   ALU add traces to a clean value DAG; only the flag `if`s stop it.
2. Control forms and returns (8a, 9a, 9b, 11a, 11c, 12a, 14d, 22c, 25a, 16a,
   25c; the "no stop recorded" return epilogues): about 14 M (25 %).
   Trace with the control state (pending, loops, call and PC stacks) captured
   as effects and give `cfg.rs`/`flow.rs` the effect list instead of its own
   decode of the fields.
3. Window analysis refusals (3a/3b "address range unknown", loop steps not
   stable): about 8 M (14 %). Primitive work in `Lower`, not generation.
4. MODIFY with circular wrap (7a, 7b, 19a_scaled, 7d) and system-register
   writes (LCNTR, PCSTK, IRPTL, ...): about 5 M (9 %). One `circ_wrap`
   intrinsic; the register-write special cases come from tracing
   `_write_ureg` over a symbolic code.
5. Conditions in SIMD and PEy compares: about 1 M.

## How to add a family (read this first)

Entry points
- Trace and print a form: `uv run python tools/sharc_lowergen_survey.py FORM...` (no
  args: all forms; shows leaves, refusing leaves and why, Rust lines, or the
  exception that is the next intrinsic or lifting to write).
- One leaf: `tools/sharc_lowergen_leaf.py FORM field=value ...`.
- Emit: `uv run python tools/sharc_lowergen_rs.py native/sharc/src/fast/lower/gen_forms.rs F1,F2,...`
  (the form list is the whole list: take it from `GENERATED` in gen_forms.rs and
  add yours). `tests/test_sharc_lowergen.py` fails when the file is stale.

Where things go
- A new intrinsic (a semantics function the trace records as an effect instead of
  running): `tools/sharc_lowergen.py`, a function `i_*` and an entry in `PATCHES`;
  an effect kind needs a case in `leaf_items`/`operands`/`Emitter.prog` in
  `tools/sharc_lowergen_rs.py` and a `g_*` primitive in
  `native/sharc/src/fast/lower/gen_prims.rs` (hand-written once, serves every
  form). Static lowering context (L, B, config) is a `("v", name)` symbol, with its
  Rust text in `rs_e`.
- Never edit `gen_forms.rs` by hand.

Verify
- Vectors: add the forms to `tools/sharc_lowergen_vectors.py` (steer random fields
  into the accepted region, or the fuzz proves nothing), regenerate with
  `--out native/sharc/src/tests_gen_vectors.rs --n 24 <all forms>`; for sequences
  add a program to `tools/sharc_lowergen_programs.py`.
- `cd native/sharc && SHARC_GEN_DIR=$PWD/../../out/native/shared-core/gen cargo test --lib gen_ -- --nocapture`
  (about 75 s; the table shows hand vs generated acceptance), then `cargo test --lib`.
- Coverage and PCM: `uv run python tools/coupled.py build`, then
  `tools/sharc_lowergen_bisect.sh 1.5 static-insns+follow none all` (PCM must be
  3bf3ba3026953c22 at 1.5 s); `--diag only` for the interpreted-reason table.
  Do not build into prod/pgo-use.

Shared files (conflict map)
- Append-only per family: a new `g_*` block in gen_prims.rs, new `i_*` in
  sharc_lowergen.py, new rows in the vectors/programs tools. Keep your edits in
  your own block, do not reorder.
- Hot shared: `lower/mod.rs`, `glue.rs`, `flow.rs`, `cond.rs`, `ir.rs`: touch
  minimally, tell the integrator. gen_forms.rs and tests_gen_vectors.rs are
  generated: do not merge them, regenerate after merging.
- Deliver `scratchpad/bundles/F<n>.patch` without the generated files.

## Running coverage table (integrator)

| state | fast (1.5 s, static-insns+follow) | interpreted | PCM |
| base 0414505, hand | 89.5 % | 10.48 % | 3bf3ba3026953c22 |
| F1 merged (24 forms) | 92.5 % | 7.45 % | 3bf3ba3026953c22 |

## AOT cost profile and a mask-free estimate (f0a3180, DN2 trig1-taps, default config, 1.41 DSP CPU/audio s)

Method: `xctrace` CPU Profiler on the coupled-run DSP thread (5 s, 13.6k samples) with a
line-tables build (`tools/sharc_build_dbg.py`, same codegen), leaf addresses symbolicated with
`atos -i` (`tools/sharc_prof_addrs.py`), lines classified by their source text
(`tools/sharc_prof_classify.py`; `tools/sharc_xctrace.sh` runs it). `sample` alone cannot do this:
everything inlines into the region function.

DSP thread: Cranelift kernels 24 %, engine/glue/std 4-6 %, AOT 70 %. Inside AOT (share of the DSP
thread): V{b,m}/Option/Result plumbing 27 %, memory (page lookup, present/dirty bitmaps, watch)
14 %, uncategorised moves/copies 11 %, float arithmetic 5 %, loop/stack/pc bookkeeping 4 %, undo and
budget and rf copies 4 %, address-map checks 3 %, flags 3 %. Real arithmetic is under 10 %.

Per instruction a region does: budget test, 3-4 V copies for undo, `_sync_empty_loop_registers`,
`_access_modifier_scale` (range tests on the address), `_advance` (pending, loops, pc), then the
operation on V (mask AND/known checks, `Err(Trap)` branches). The code is the core partially
evaluated on the fields only; LLVM folds the masks of values it can see, not those that come out of
memory, flags or calls.

Mask-free emission (concrete values, one budget check per block, no per-insn undo, no loop sync, no
address-map test, flags lazy, memory through a cheaper guarded access): removes about 45-50 points
of the 100 of the DSP thread, so AOT 70 -> ~22, ns/insn 4.4 -> ~1.5, default CPU 1.41 -> ~0.75.
Confidence medium-low (+-30 %): the classification is by source line text. Effort 1-2 weeks, high
risk (it changes the transpiler's V type in block mode). The fast-tier route reaches 0.5 ns/insn
without touching the AOT generator; prefer it for the hot functions, keep AOT for the rest.

## Generated helper bodies (base f0a3180 + lane B's stub)

`tools/sharc_lowergen_helpers.py` prints each traced form a second time as plain Rust over concrete
`u32` values (`gen_helpers.rs`: `h_FORM` runs the instruction on the engine state, `t_FORM` says
which registers it reads and writes, both from the same decision tree and effect programs as the IR
lowering). `fast/helper.rs` has the small runtime (`hload`, `hstore_ok`, `hpost`, `RegRead`,
`Touch`, `plan`); `lower/helper.rs` syncs exactly the touched registers around `Op::Helper`.
Forms: 3a 3b 3c 3d 4a 4b 4d 14a 15a 15b, unconditional, no compute. `mise run regen` rebuilds it.
`SHARC_FAST_HELPER=1|LO-HI[,LO-HI]` selects sites (an instruction that cannot be lowered becomes a
helper instead of ending the region); `SHARC_FAST_HELPER_INTERP=1` uses the interpreter step as
the body, `SHARC_FAST_HELPER_NODYN=1` is lane B's rule (give up the dynamic window).

Measured. Body: 6 ns mean (3.5-13) against 62 ns for the interpreter step in a microbenchmark
(`cargo test --release --lib helper_ns -- --ignored --nocapture`), plus about 5 ns of sync: about 11
ns per helper instruction against the model's 30. Exactness: `gen_helpers_match_the_interpreter`
(every vector, SISD/SIMD, bank/stack models, a trap leaves every register unchanged), 62 lib tests,
PCM 3bf3ba3026953c22 at 1.5 s and 8cbae26b73014732 at 12 s. Effect on DSP CPU/audio s, static-insns+
follow at 12 s (steady state; 1.5 s runs are dominated by compile time): hand/IR only 7.64, helpers
in 0x1c8ef1 7.28 (-5%, 1.3M helper calls per audio s), helpers everywhere 7.72 (+1%), ten hot
functions 14.8 vs 14.1 at 1.5 s (more denied instructions means more rebuilds). A second machine
state moved the baseline to 8.06 and the range to 8.01: the gain of this family is inside the noise
of unlocked runs, as the call count says (1.3M calls per audio s x 100-250 ns saved = 0.1-0.3 s).

Why the model's 8.8 -> 5 is not reached: the instructions that end regions in the hot functions
are mostly compute (2a_short opcodes, 5a_move compute, multifunction, MR moves: about 20% of the
interpreted ones) and they write ASTATX. A helper for them needs the kernel's pending flag writers
applied to the state before the call and reset after (lane B: +10 ns), and a body that computes
flags: the tracer would have to give dynamic branches and dynamic known-bit masks of ASTATX a
meaning, or the body falls back to the interpreter step (about 60 ns without Engine::step) which
still saves about 300 ns against a region exit plus an interpreted instruction.

## Class (e): why regions do not run (564e4c5, DN2 trig1-taps, static-insns+follow, 12 s)

Before building the universal helper fallback, `tools/sharc_classe.py` (new) joins the interpreted-PC
histogram with the per-region run records of a `--diag only` run. Finding: most interpreted PCs of
class (e) have a region that was built and called; the call was declined on a requirement. Example
0xb82d04: `Req::Eq(M4, 3040)` failed on every call (M4 = 3041, 3042, ...): `Lower::baked` bakes an M
register the region never writes as a constant, and here the M register is data (a counter). The
engine rebuilt the region for the new value up to REBUILD_BUDGET (16) times, spent the compile time,
then declined for good. 17 builds per entry; req declines 4.05M -> 0.58M per 1.5 s after the fix.

Fixes (both small, `SHARC_FAST_NOVOLATILE=1` / `SHARC_FAST_NOEXIT0=1` turn them off):
1. An M register whose baked value fails two calls in a row is marked volatile for the code around
   the entry (`pc >> 7`, `region::mark_volatile`) and is not baked in later builds there.
2. A call whose first instruction leaves the kernel before completing (`Decline::Exit0`: a guarded
   dynamic window that never holds, e.g. `DM(I3, M4)` with M4 an index) four times turns that
   instruction into a helper call in the next build (`cfg::force_helper`).
DSP CPU per audio s, same session, three runs each: 8.82 (both off) -> 7.19 (1) -> 6.93 (1+2);
built regions 8317 -> 5169 -> 5291; interpreted 6.22% -> 5.89% -> 5.66%; PCM 8cbae26b73014732.
`SHARC_FAST_DEBUG_REQ=0xPC` prints which requirement fails for an entry.
