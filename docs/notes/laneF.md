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
