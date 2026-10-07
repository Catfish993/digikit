# Lane F4 notebook: MODIFY, circular buffers, system registers

Base 16e23a0 (worktree agent-a4f10678545fa43f5). Nothing committed.

## What changed

Forms generated now: 7a, 7b, 19a_scaled (27 forms in `GENERATED`; 7a runs in
SIMD and with L != 0 without the hand path). 7d (ACONV) is not generated: its
handler depends on the address map (`_aconv`); 6750 cold executions per 1.5 s.

Tracer (`tools/sharc_lowergen.py`)
- `concretize`: a line with `UREG_CODES[...]` is structural even when it has a
  `%` (`UREG_CODES["L%d" % source]` pinned to L0 before; `forms_flow` has the
  same pattern for I8+pmi, now pinned correctly too).
- `S.__abs__` (a decision on the sign).
- A register read of L (48..63) or B (64..79) is a static symbol `("v", "L3")`;
  `rs_e` prints it as `self.sv_lb(code)?` (the entry value, with the `Req::Eq`).
- Intrinsics `_circular_wrap_const` (effect `circ`: iv, B, L bytes, delta,
  unsigned flag) and `_modify_scale` (adds the `plainmod` effect: a normal-word
  MODIFY needs a plain address, no memory access checks it).
- Emitter: `circ`, `plainmod` items; `{ let b = ..; let n = ..; g_circ(..) }`
  (two borrows of self otherwise).

Primitives (`gen_prims.rs`, own block at the end)
- `sv_lb`, `lb_stale` (a region that writes L or B of an index register does
  not lower circular updates of it), `g_plainmod`, `g_circ`, `wrap_unchecked`,
  `wrap_general`, `entry_value_of`.
- `g_circ`: `Ia = MODIFY(Ib, ..)` with L != 0. Requires a constant modifier
  (|delta| <= L bytes; 7b reads M unsigned, so a negative M refuses), as the
  core stops otherwise. Entry value in the buffer (`Req::Within`) -> `wrap_add`
  with the buffer's range; register also written plainly -> `wrap_unchecked`.
- `TagKind::Mod`; `g_wr` no longer requires L == 0 for a plain MODIFY sum
  (`Add`): the core's 7a/19a sum is plain whatever L is.
- `g_wr_mode1`: a MODE1 value that is a register the region never writes
  becomes the register's build-time value under `Req::Eq`
  (`entry_value_of`).

Shared files (small edits)
- `mod.rs`: `post_value` drops `no_plain_circ` and uses `wrap_general` for a
  register in `plan.plain`/`plain_out`; `commit` marks a circular index written
  outside its buffer as plain (`plain_out`, `retry` from pass 2 on) instead of
  refusing; `wr` accepts L/B; `lb_stale` checks in `post_value`,
  `modify_update`, `circ_range`; `check_code`/`rd` accept `sysops::plain_sys`;
  `wrap_add` is `pub`; `Lowered::retry`.
- `region.rs`: `MAX_PASSES` 5; a third pass that found a circular index written
  outside its buffer is repeated with it plain (`Lowered::retry`), refuse when
  still failing at pass 5.
- `sysops.rs`: `plain_sys` (PX1, PX2, USTAT1-4, MMASK, MODE2).
- `mem_addr.rs`: `guarded_xfers` is `pub(super)`.

## Semantics worth knowing

- `_type_7a` wraps only on the cond == 0x17 path; the unconditional MODIFY (the
  only one the lowering sees: COND_FORMS has no 7a) is a plain sum. 7b and
  19a_scaled wrap on L != 0 whatever MODE1.CBUFEN says.
- A post-modify (`_circular_step`) needs no |delta| <= L and no in-buffer entry
  value: `wrap_unchecked` is exactly `candidate >= B+L ? -L : candidate < B ?
  +L`, in unsigned 32-bit.

## Verification

- `cargo test --lib gen_` (SHARC_GEN_DIR=out/native/shared-core/gen): the fuzz
  now runs 4 seeds per vector; seeds 2 and 3 set B, L (4..15 units), the index
  inside the buffer (often at an edge) and MODE1.CBUFEN; engine_cfg sets the
  plain system registers known. New vectors: 7a, 7b, 19a_scaled (small signed
  immediates), system-register codes in ureg fields (25 %). New program
  `circ_loop` (post-modify and MODIFY on a register that is also written
  plainly, below and above its buffer). Mutation checks: flipping the wrap
  direction in `wrap_unchecked` fails `gen_programs`; flipping the edge test in
  `wrap_add` fails `gen_lowering` (at seeds 2/3); an edge test that differs only
  at `t == top` is not caught (rare in the fuzz).
- `cargo test --lib`: 60 pass with and without `SHARC_FAST_GEN=all`.
- `pytest tests/test_sharc_lowergen.py tests/test_lint.py`.

## Coverage (DN2 1.11, trig1-taps, 1.5 s, static-insns+follow, SHARC_FAST_GEN=all)

| state | fast | interpreted insns | PCM |
| base 16e23a0 | 92.5 % | 56.65 M (7.52 %) | 3bf3ba3026953c22 |
| 7a, 7b, 19a_scaled, general wrap | 92.8 % | 54.88 M | 3bf3ba3026953c22 |
| + MODE1 from a register, L/B writes, plain system registers | 93.0 % | 52.89 M (7.02 %) | 3bf3ba3026953c22 |

The "7a in SIMD", "MODIFY on an index register with L != 0", "I6/I7 written
outside a circular post-modify", "write of L/B" and "MODE1 written with a
run-time value" rows are gone from the interpreted-reason table.

## Left in this family

- System registers with machine state behind them: ASTATX/ASTATY/STKYX/STKYY
  (lazy flag replay at a read; the 18a bit-test sets BTF, which no flag group
  tracks), PCSTK/PCSTKP/LADDR/CURLCNTR/LCNTR (stack and loop models), IRPTL,
  IMASK, MODE1STK. They sit in the cold context-save function
  (FUN_1c02a4, 6750 calls per 1.5 s, about 84 interpreted insns each) and in
  FUN_1c0a6f.
- MODE1 writes with the bank model need a plain successor in the block
  (`bank_pair`): 0.74 M. Both sites are followed by a bit-test (not lowered) or
  a conditional jump.
- 7d.

## Integration

Regenerate after merging: `tools/sharc_lowergen_rs.py gen_forms.rs <GENERATED +
7a,7b,19a_scaled>`, `tools/sharc_lowergen_vectors.py ... 7a 7b 19a_scaled`,
`tools/sharc_lowergen_programs.py --out native/sharc/src/tests_gen_programs.rs`.
The patch carries no generated file.
