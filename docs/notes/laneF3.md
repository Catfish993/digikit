# Lane F3 notebook: control forms in the fast tier

Base 16e23a0, worktree agent-a4dd1f9263c82ecea, nothing committed. Family: returns,
jumps and calls, conditional and delayed transfers, IDLE/NOP, the 9b_abs stops.

## What changed

Control forms are not traced by `tools/sharc_lowergen`: the tracer never runs
`_advance` (pending transfers, loop and PC stacks), so the transfer half stays
hand-written in `cfg.rs`/`flow.rs`, written from `forms_flow.py` and
`sequencer.py` and checked against the interpreter by the program tests below.
The data half (compute, slots) goes through the generated forms.

- IDLE (22c) is a generated form (`GENERATED` + 22c): `_type_22c` is `_advance`.
  26a (SYNC) is `Uncertain` in the decode table, so it never reaches the fast
  tier; not generated.
- `Tail` (cfg.rs): a taken call or return leaves the kernel at its target, and
  the glue does the stack bookkeeping the interpreter does at the branch
  (`glue::sites::apply_fx`):
  - CALL (8a b=1, stack model, `follow_loaded_calls`, target decodes): pushes
    `0x01000000 | return` on the PC stack and, when the call is over, the return
    on the followed-call stack, `at_loaded_entry = true`. Inside the delay slots
    the pending transfer is `{call, return_sw}` with the push already done.
    Entry check: followed-call stack below `max_call_depth`.
  - RTS (11a, 11c, x = 0, lr = 0, stack model): pops the PC stack
    (`_take_return`; the followed-call stack pops when its top is the target,
    else it clears). The target is the PC stack top, read by the glue at entry
    into `CTX_DYNPC` (declined when the stack is empty or the top is unwritten).
    Exactness needs the top unchanged when the return runs: the region models no
    entry loop (`n_entry == 0`) and no loop is active at the return node (else
    the node is a stop).
- Transfers with a compute (9a_rel, 11a, 9a_abs): the compute is lowered as the
  `2a` it runs as (`Dec::compute_part`), conditional on COND (ELSE: on the
  complement, `cond ^ 0x10`; ELSE with no condition never runs). The condition
  is read before the compute's flag effects. Refused in SIMD mode (the core
  runs the SISD `_compute` there).
- 9b_abs/9a_abs indirect jumps: the `Return` node (target `I(8+pmi) + M(8+pmm)`)
  now also covers the non-delayed jump and 9a_abs with compute.
- Return slots: `tail_write` (no circular range proof for the last writes
  before the exit) now holds for the last slot whatever the form, not only
  RFRAME (the 0x1c0da1 return: `I7 = I6; I6 = DM(I6 + 0)`). 25c_rframe lowers
  in SIMD mode (it has no SIMD companion).
- Stop reasons of delay slots now name the slot's refusal
  (`delay slot (insn # (4a): compute ...)`).
- Fixed in passing (lower/mod.rs, shared): `bank_site` stayed set after the
  instruction it adjusted, so the budget guard of the next cut point reported
  the MODE1 write's pc (a MODE1 write, one plain instruction, then a cut point).

## Tests

- `gen_control_programs_match_the_interpreter` (tests_gen.rs): programs from
  `tools/sharc_lowergen_programs.py` (`CONTROL`: delayed and plain calls and
  returns, conditional ones, calls in a DO loop, returns with a compute, jumps
  with a compute and ELSE, indirect jumps, the compiler's return with its two
  slots on a circular I6, the same with RFRAME in SIMD mode, an IDLE), run with
  regions at the function entries and return points, hand and generated
  lowering, cfg 0 and 1, chunk sizes 1..64 (every delay-slot cut), comparing
  registers, memory, pending, PC stack, followed-call stack and
  `at_loaded_entry`. `GEN_CTL=name` selects programs.
- `cargo test --lib`: 61 pass; `pytest tests/test_sharc_lowergen.py`: 3 pass.
- The generated 22c is only covered by the IDLE in a control program: the
  decoder will not pick 22c for random words (21p/22p overlap, successor-based
  width), so the vector fuzz cannot reach it.

## Results (DN2 1.11, trig1-taps, 1.5 s, static-insns+follow, `--no-lock`)

PCM 3bf3ba3026953c22 in every run.

| run | fast | interpreted insns (diag) | kernel calls (diag) | declined shape |
| base 16e23a0, SHARC_FAST_GEN=all | 92.5 % | 56.65 M | 73.86 M | 2.74 M |
| F3, SHARC_FAST_GEN=all | 93.0 % | 52.62 M (-7.1 %) | 71.13 M (-3.7 %) | 1.23 M |
| base, hand forms | 89.5 % | | | |
| F3, hand forms | 90.2 % | | | |

Steps: the return-slot fix and IDLE took 0.9 M off the interpreted count, calls
and returns 2.1 M, the indirect jumps and RFRAME in SIMD mode the rest.
Calls and returns now cost no interpreted instruction (before: the call, its two
slots and the return, about 7 per call and return pair).

## What is left in the family

Hot control stops after this lane are not control: the delay slots or the
compute they carry (`4a` multifunction, `2a_short` compute, `5a_move` MODE1 write
with a run-time value). Control proper:

- 20a PUSH/POP (PC, loop, status stack): about 0.15 % of interpreted; needs the
  stack effects applied at the exit like `Tail`.
- 9a_abs/9b_abs with a condition or a call (b = 1), RTI, loop re-entry (lr).
- A call whose callee is inlined (a region that continues through a call and its
  return, with the return address known statically): not done; each call and
  return still costs a kernel exit.
