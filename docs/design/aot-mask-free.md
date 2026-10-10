# Mask-free AOT emission: design and slice proof

Status: design (2026-10-09); slices 0 and 1 measured 2026-10-10, results in section 6 (gate: do not build
concrete registers, build the flag-known gate and cold-path density instead). Sections 0 to 5 are the
original design and were not edited; the numbers marked **[D]** come from earlier measurements cited in place, **[O]** is open. Marks as
in CLAUDE.md. No firmware-derived code is quoted: block code is described, and the few lines shown
are generic.

Goal: the desktop build (AOT generated core plus fast tier) from about 1.43 DSP CPU s per audio s to
<= 1.0 (finding 18, "Next levers", lever 1; `docs/design/fast-compile-offload.md` lever list).

## 0. Where the estimate came from, and what "mask" means

The estimate is in finding 18 ("What did not work", "Next levers" 1, last paragraph of "AOT cost
profile"). Its only basis is one `xctrace` sample of the default config at 1.41 CPU s per audio s
**[D]**: AOT is 70% of the DSP thread, Cranelift kernels 24%. Inside AOT: V/Option/Result
plumbing 27%, memory access 14%, uncategorised moves 11%, real arithmetic under 10% (the five
add up to about 62 of the 70 points, so the categories are shares of the DSP thread). Removing
plumbing and moves (38 points) plus about 10 points of memory work takes AOT from 70% to 22%:
4.4 -> about 1.5 ns/insn, and 1.41 x (1 - 0.48) = about 0.75. The lane notebooks that held the
category rules were folded away; the categories were assigned by symbol and source line, not by a
counter experiment. Nothing in the repo re-measures them.

"Mask" is the known-bit mask of the value lattice. `rt::V { b, m }` (native/sharc-rt/src/rt.rs)
is a 32-bit register value known bit by bit: `m` has a 1 at every known bit. `Const` is
`m = !0`, `Unknown` is `m = 0`, `PartialConst` is anything between. The transpiler maps the
Python core's `Const/Unknown/PartialConst` to `V`, so every ALU, address and memory helper
(`bnd::_add`, `_multiply`, `_dm_read_b`, ...) tests `is_c()`, builds a `V`, and returns
`R<...>` / `Option<V>` for the trap and "became unknown" cases. In block code (rsgen) a register
read must be known at entry, and `rf_set` traps (`TRAP_BLOCK_UNKNOWN`) if a computed value is not
fully known, so for ordinary registers `m` is always `!0` at run time and LLVM is meant to fold it
("masks become constants", sharc_rsgen.py header). "Mask-free" therefore means: stop carrying `V`
in block code. Registers become `u32`, the helpers return `u32`, and anything that could make a
value unknown is a cold exit chosen at generation time. It also pulls in the "commit state once
per block" idea of the 10-06 handover (undo copies, snapshot copies, per-step bookkeeping), because
those are the other half of the plumbing and share the same trap protocol.

Static facts that make this feasible (current DN2 core, `out/native/dn2-audio/gen`, counted with
grep; counts include the duplicated MODE1 and `_unknown` bodies):
- Every body step is already an inlined specialised variant (74403 `#[inline(always)]` variants in
  `spec_NN.rs`, none `inline(never)`): decoded fields, pc, MODE1 and "no delayed transfer" are
  constants. Only register values, memory and the V masks are dynamic.
- The only registers ever given a generation-time-unknown value (`rf_put(.., V::UNK)`) are 118,
  119, 120, 121 (ASTATX, ASTATY, STKYX, STKYY: `PARTIAL_REGS`) and register 22 (I6, in the 452
  `25c_rframe` variants where the frame read finds no present bytes). The other 123 registers are
  known in every block by construction.
- `bnd::_bank_complete` and `_pc_stack_complete` are empty functions in block mode (model-safe
  only); 55k calls to them cost nothing.

## 1. What the AOT core emits today, and where the time goes

### 1.1 Shape

One generated function per region (1665 regions, 2657 blocks, 38378 block instructions in the
current DN2 core; 122 MB of Rust in 41 `blocks_NN.rs` plus 16 `spec_NN.rs`).

Region prologue (once per region call, `tools/sharc_rsgen.py` `region_text`):
1. one `AND` over the `.m` of every register any body reads, `!= u32::MAX` -> exit
   (`EXIT_BUDGET`); `s.pending.is_some()` -> exit;
2. `Rf::default()` (a 128-entry `r` and 128-entry `o` array of `V`, `allow_unknown`, pc,
   pending), then one `rf.r[c] = V { b: s.r[c].b, m: u32::MAX }` per loaded register;
3. a `match at` dispatch loop over the region's bodies; each body repeats the `.m` AND check.

Per instruction (the block loop in `body_lines`), described generically:

```text
// pc form
if short && s.icount + ic >= s.limit { exit BUDGET }      // budget, one compare per insn
rf.o[c] = rf.r[c];            // snapshot of each register the form reads as "old"
let svN = rf.r[c];            // copy of each register the form writes (undo on trap)
let ld = s.loop_depth;        // loop/MODE1 forms only
if let Err(t) = spec::_type_<form>__v<k>(s, &mut rf) {    // the inlined variant
    rf.r[c] = svN ...; s.loop_depth = ld; rf.pc = <pc>; rf.pending = None;
    s.trap = Some(t); exit TRAP                            // cold path, one per insn
}
s.un = 0;                     // only if the variant writes the undo log
ic += 1;
if rf.pc != <next pc> { match rf.pc { <successors> => chain/continue, _ => exit NEXT } }
```

Counts over the current core: 63758 step sites, of which 63661 (99.8%) are classified "can trap"
(`_can_trap` finds a `?` or `Err(` in almost every variant, so the "no trap path" branch of
rsgen is unused), 84.5k `rf.o` snapshot copies, 134.6k undo `sv` copies, 58.7k pc-successor
matches, 63.8k budget checks, 7.1k `s.un = 0`. At exit: `s.icount += ic`, pc and pending written
back, one write-back per written register (`s.r[c].b = rf.r[c].b` when only ever written known,
else the whole `V`), `rollback_log()` on a trap, then a tail call to the chained region
(`s.chain < CHAIN_MAX`).

Inside the variants, a typical register-and-memory instruction does: `bnd::_ureg_rf` for each
operand (a V copy), `_add`/`_multiply` (an `is_c()` pair test and a `V::c`), the access
(`_dm_read_b` / `_dm_write_b`), and `_advance` (sets `rf.pc`). A store (`_dm_write_b`):
`access_address`, `plain_address_b` (range tests), `Mem::fast_write`: `eff[page]` pointer load,
null test, per-byte `dirty` bitmap word test, the data store, `note_write` (code-watch lookup),
`canonical_of`, then `s.log(Undo::MemWord(..))` (cap check, 16-byte record, `un += 1`). A load
(`_dm_read_b`): the same page lookup, a `present` bitmap test, the data load, and an `Option<V>`
result that `rf_set` turns into a `TRAP_BLOCK_UNKNOWN` branch. A memory-model change would be a
separate project; this design leaves the semantics of `Mem` alone.

### 1.2 Measured cost

Cost model (finding 18) **[D]**: AOT instruction about 4.4 ns (46.6% of insns take 3.07 of 3.64 s
of region time), kernel instruction 0.5 ns, interpreted instruction 105 ns, fast region call
112-266 ns. At the freeze: 234M AOT insns x 4.4 = 1.03 s, kernel 0.13 s, 0.62M region calls and
1.9M interpreted insns the rest (about 0.55 s); default 1.584 (1.41-1.43 on other sessions; the
timed exclusive A/B of 2026-10-09 has trig1-taps at 1.424-1.430). Refit at 1.43: others about 0.49
s, so **AOT is about 0.94 s = about 4.0 ns/insn**.

Two things from earlier region microbenchmarks (`tools/sharc_regionbench.py`, warm cache, captured
entry states) change how the 4.4 should be read:

1. **Warm AOT is already 1.4-2.3 ns/insn.** Hot loops in the production-style core: `1C399A`
   1600 insns in 2.2-2.4 us (1.4-1.5 ns/insn), `1C5765` 1216 insns in 1.6-1.75 us (1.3-1.4), and
   `B8276C` 2.31 ns/insn (handover 10-05) **[D]**. The mask-free prediction of about 1.5 ns/insn is
   what these loops already reach today. The gap to the 4.0-4.4 in situ is not the warm
   per-instruction work.
2. **Per-feature ablations were small.** Variants `abl-nosv` (no undo copies), `abl-nobud` (no
   budget check) in `out/native/regionbench/variants/` (2026-10-05, same two regions): about 0-3%
   and 7-9% faster than `wt2b-base`. `abl-noo` (no `rf.o` copies) is a broken bench (exit code 2)
   and tells nothing. Hand-written code for the same loops (`sol`) takes 292 ns for `1C399A` and
   538 ns for `1C3862` against about 3000 and 4170 ns AOT at the time (10x and 7.8x); the fast
   tier lands at 1.3-1.8x of that **[D]**.

Likely reasons for 4.0 in situ against 1.5-2.3 warm **[O]** (not separated by any measurement):
- **Code size.** In the pre-fxprof core (1751 blocks, 20722 insns) the 774 region functions are
  22.6 MB of machine code (`nm` of the `g-e220e7a2` coupled-run, .text 27 MB), about 1.1 KB per
  block instruction. The current core has 1.85x the instructions (about 40 MB). Over the
  FX-active profile (`out/native/dn2-audio/fxprof/prof-fx`, 600M insns, 11862 distinct PCs) the
  hottest 300 PCs cover 61% of executed instructions and 1000 cover 80%: the hot set is 0.3 to 1.1
  MB of code against an L1 instruction cache of 128-192 KB. The handover of 2026-10-05 measured
  instruction-cache delay as a contributor without singling out a function.
- **Region entry and exit.** Each region call loads its registers into `Rf`, writes them back,
  and dispatches through the engine (about 14.5M block entries in the 3 s idle/chord scenarios
  of finding 07, roughly 100 insns per entry): estimated 0.5-1 ns/insn **[O]**.
- **Memory.** 40% of executed instructions access memory (join of `prof-fx` with `mem_access` in
  `out/sharcdb/dn2-1.11.sqlite`). Mix by form: `2a` 15%, `3a` 15%, `2a_short` 10%, `4a` 9%, `15b`
  8%, `5a_move` 7%, `6b_shiftimm` 5%, `15a` 4%.

So the 27% "plumbing" in the xctrace sample is a mix of V arithmetic, Result/Option branches,
undo and snapshot copies, and the I-cache cost of all the cold trap paths they create, all
attributed to the same source lines. The mask itself may already be folded by LLVM; whether it
is, is the first thing to measure (section 3, slice 0).

## 2. What mask-free changes, and why it is exact

Three changes, from cheapest to largest. Each stands alone and each can be switched off by
regenerating.

**A. Concrete registers in block mode.** The block variants use `u32` for the 124 ordinary
registers (a new `rf.c: [u32; N]` or `Rf` with a plain value array), and a `bndc` module of
concrete helpers: `_add(u32, u32) -> u32`, `_dm_read_c(...) -> R<u32>` (Err when the bytes are
not present or the access is not plain, as now `None`), `_dm_write_c`. `is_c()` tests and
`Option<V>` disappear because they are statically true. Registers 118-121 keep `V` (they are
`PARTIAL` today), register 22 gets a "frame read failed" exit instead of `rf_put(.., V::UNK)`
(cold, 452 variants). The interpreter-facing core (`core_i`/`core_g`) and the
`allow_unknown` fallback region (`r_1C253F`) keep `V`; they are a second translation mode of the
same Python core, so semantics stay in `tools/sharc_core`.

Why exact. Today `rf_set` writes `V { b: v.b, m: u32::MAX }` only when `v.m == u32::MAX` and
otherwise traps before any state is committed; reads of the 123 registers are `m == MAX` by the
entry gate and by the line above. So `proj(r) = r.b` is a bijection on every state block code can
be in, and every helper's result on fully known inputs equals the concrete operation (the `V`
helpers return `V::UNK` only if an input is not `is_c`). Invariants that must hold:
1. The entry gate (all read registers known, `pending` none, budget) is unchanged.
2. A value that would become unknown exits before commit, with the same `Trap` code, the same
   restored registers and the same undo log as now. The trap code is part of the engine's
   behaviour (zero-progress bails, `TRAP_BLOCK_UNKNOWN` counts), so the exit set must be
   identical, not just the result set. The tier-equivalence sweep checks the observable
   consequence; the per-region differential below checks the exits.
3. PARTIAL registers and any register the generator cannot prove "only ever written known" stay
   on `V` (a per-register class decided at generation time from `variant_putregs`).
4. Memory semantics (present/dirty bits, code watch, peripheral and MMR rules, `_dm_*_full`) are
   untouched; only the call shape changes.

**B. Check-then-commit ordering, so the undo copies go.** The `svN` copies, `ld/bk` copies and
the undo log exist because a variant may write a register and then trap. In most forms every
`Err` site precedes the first register or memory write (read operands, compute address, access,
then write results and `_advance`). The translator can prove this per variant (an ordering
analysis over the generated text, the same level as `_can_trap`): if no `Err` site follows the
first commit, emit no `sv` copies, no restore, no `s.un = 0` and no `s.log` for the store; the
trap path becomes "restore nothing, set pc, exit". Where the order is wrong (post-modify plus
load: the load may fail after the modify is computed), reorder the variant's statements so the
trapping access comes first when they are independent, otherwise keep today's copies for that
variant. This is exact because the undo set is by construction empty at every `Err` site. Check
that `_advance`'s own `?` cannot fire in model-safe bodies (`_bank_complete`,
`_pc_stack_complete` are empty; loop-stack overflow sites `stk_*` do trap and keep the copies).

**C. Snapshot copies and cold-path outlining.** `rf.o[c] = rf.r[c]` (84.5k) exists for forms that
read the "old" view while writing the same register in the same instruction (parallel
multifunction). With concrete registers LLVM sees through this when the form has no `RegView`
`OLD` read of a register it also writes; the translator can emit the copy only for that case.
Trap exits become one shared tail per region taking the trap code in a register
(restoring nothing under B), so the cold code stops being a per-instruction block. This changes
no semantics and is the part that shrinks machine code.

Not part of this design: a different memory model, batching `s.icount` across regions, changing
chaining, or removing the budget check per instruction (a per-body check instead of a per-step
one is possible but changes where a budget exit lands; the ablation says it is worth 7-9% warm,
so it is a separate exactness question: a body would have to run to its end or decline at
entry when `s.icount + n > s.limit`, which only splits a body at the limit and is exact if the
dispatcher then runs the exact tail in the interpreter, as `MODEL_SAFE_ONLY` does now).

### Ceiling, redone

Model (finding 18 cost model, refit at 1.43): default = 0.49 + 234M x t_AOT, with t_AOT 4.0 today.

| AOT ns/insn | default DSP CPU s per audio s | what it would mean |
|---:|---:|---|
| 4.0 | 1.43 | today |
| 3.0 | 1.19 | undo/budget/snapshot ablations only (about -25%) |
| 2.3 | 1.03 | in-situ equals today's warm `B8276C` |
| 2.1 | 1.00 | target reached; needs a 1.9x faster AOT |
| 1.5 | 0.84 | warm `1C399A` today; the finding 18 prediction (0.75 at its 1.41 base) |
| 1.0 | 0.72 | stretch |

What I believe, in order of weight: (1) the warm ablations show that each plumbing item alone is
worth 0-9%, so the xctrace 27% is mostly not the arithmetic of masks; (2) the in-situ cost is 2x
the warm cost, which points at code size and entry/exit; (3) A+B+C together cut machine code by
an unknown factor I would guess at 2-3x (cold exits, copies, `Option` plumbing; **[O]**,
measurable in slice 0). Central estimate **t_AOT 2.4-3.0, default 1.05-1.2; 0.8-0.9 is the
optimistic end, not the plan.** Reaching <= 1.0 with mask-free alone is possible but not the
likely outcome; the target needs mask-free plus one of the alternatives in section 4 (PGO on the
new core is worth 5-8% by earlier retrains, 0.92-0.96 ratio **[D]**).

## 3. Implementation plan

### 3.1 Code to change

- `tools/sharc_transpile.py` (5311 lines): a block-concrete translation mode next to the `V`
  mode (`Translator.blk`): type map `VAL -> U32` for ordinary registers, `Spec`/`VI` reduced to
  `u32`/`i128` addresses, calls to `bndc::*`; the ordering analysis (`variant_order`, next to
  `_can_trap` and `variant_logs`), the per-variant register class report (extends
  `variant_putregs`).
- `native/sharc-rt/src/rt/bnd.rs` (1693 lines): new `rt/bndc.rs` with concrete helpers; `Rf` gets
  a concrete file (`rf.c`) plus the `V` file for registers 118-121 (and 22 when unproven). Editing
  `sharc-rt` rebuilds the generated core: use the mini gen (3.2).
- `tools/sharc_rsgen.py` `region_text` / `body_lines`: concrete prologue (no `.m` AND, no
  `V { m: MAX }`), per-variant "no trap" and "no undo" emission already has the hook
  (`traps = TR.variant_traps.get(path, True)`; extend it with the ordering result), one shared
  trap tail per region, `rf.o` copies only where the form needs them. Flag `--concrete`
  (default off until the gate passes) so old and new cores can be generated from the same tree and
  A/B'd. The `_unknown` fallback region keeps the V path.
- No change to `tools/sharc_core` semantics: golden hashes (`tests/test_sharc_golden.py`) must
  not move.

### 3.2 Build cost and fast iteration

A full DN2 `sharc-gen` build is about 12 min now (it was about 4-5 min before the FX-profile
core doubled the source; DT2 has its own). Do not iterate there.
- `tools/sharc_regionbench.py gen --tools-dir DIR --only <regions>`: a mini gen of the selected
  regions (about 16 s for rsgen, about 1 min of cargo; transpile inference is cached by transpiler
  source hash, 5 s), `capture` for entry states (shared by all variants), `bench` and `compare A B
  --rounds 9` (+-0.7%). A variant built from a git worktree's `tools/` (`--tools-dir`) keeps the
  old and new generators side by side. The `region_bench` example does not compile at the moment
  (missing `flat_windows` in its `Env`; finding 18, plan log 2026-10-09): fixing that is step 0.
- A reduced production gen for in-situ checks: `tools/sharc_rsgen.py --only` the top 12 functions
  (about 65% of executed instructions: `1C3911`, `B826C8`, `1C37B4`, `1C35A7`, `1C54C7`,
  `B8258F`, `1C244d`, `1C8EF1`, `B8A912`, `1C0C76`, `1C1738`, `B8AC4C`) built with the old and the
  new generator, run through `coupled-run`. The rest runs in the fast tier and interpreter, which
  changes the absolute CPU but the PCM must stay equal and the A/B on the same block set is
  clean. About a tenth of the source, so about a 1.5-2 min compile.
- Only the final full core, the PGO retrain (`mise run pgo`, about 6 min, milestone only) and the
  timed baseline take the long path. The `mise run emu` prod/pgo-use target is not touched.

### 3.3 Keeping it exact

Layered, cheapest first:
1. Per-form differential (new tool, `tools/aot_diff.py`, built on regionbench's captured states):
   for every specialised variant used by the slice, run old-V and new-concrete code on the same
   states plus random corner states (known/unknown operands, absent memory bytes, MMR addresses,
   circular wraps) and compare registers, memory overlay, `rf.pc`, `pending`, `ic`, the exit code,
   the `Trap` and the undo log. This is the check that exits are identical (invariant 2), which the
   PCM sweep cannot see.
2. Region level: same states through `r_<PC>` of both gens, compare the committed `St` (the
   mini-gen `check` command compares text only; this compares behaviour).
3. In situ: `mise run scenarios` (pins), `scenarios:check` (61 tests, `--jobs 6`, about 200 s) and
   `scenarios:sweep` (191 tests) in the default and the no-AOT config; `coupled.py scenario --all
   --jobs 6`; `coupled.py bisect` on any difference. Tier-equivalence gate: the AOT tier and the
   interpreter render identical PCM (134 of 134 scenarios equal today).
4. `tests/test_sharc_golden.py` unchanged; DT2 sample check when the DT2 core is regenerated;
   `cargo test --lib --tests` in `native/sharc`.
Rule: any PCM change is a bug, not an intended change; no pin is updated.

### 3.4 Staged slice proof (go/no-go after each)

- **Slice 0, measure before building (1 day).** Fix `region_bench`. Compile the mini gen for one
  hot region with `--emit=asm` (or `objdump` on the rlib) and count machine instructions, branches,
  stack spills and cold-exit bytes per SHARC instruction for 10 forms. Run xctrace on the
  regionbench loop of `1C399A` and `B8276C` to reproduce the category split on a warm loop.
  Decide: is `.m` folded already (no `cmp` on a mask in the hot path)? how large is the machine
  code per instruction? Gate: if `V` tests are already gone from the hot path, change A alone
  buys nothing and the plan reduces to B and C.
- **Slice 1, assume experiment (1 day).** In `rf_get`/`bnd::_ureg_rf` add
  `core::hint::assert_unchecked(v.m == u32::MAX)` for the 123 always-known registers and replace
  the AND entry check by per-register asserts (sound: the gate already enforced it). No generator
  change beyond that. Bench the mini gen. This is an upper bound on what folding the masks gives
  without any semantic edit. Gate: if warm ns/insn improves by < 5% and code size does not shrink,
  stop A.
- **Slice 2, B and C on the hot block set (3-4 days).** Ordering analysis, no-undo emission, one
  trap tail, conditional `rf.o`. Bench with `compare --rounds 9` on the 6 captured regions and the
  reduced production gen. Gate: warm >= 15% faster and machine code >= 1.5x smaller on the slice,
  per-form differential clean (target: 0 mismatches in 100k random states per form).
- **Slice 3, concrete registers and `bndc` (4-6 days).** Forms by frequency: `2a`, `3a`, `2a_short`,
  `4a`, `15b`, `5a_move`, `6b_shiftimm`, `15a` (82% of executed instructions), the rest falls back
  to the V path per variant (the generator picks per variant, so partial coverage is fine).
  Gate for the full regenerate: the reduced-gen A/B in situ shows >= 20% less AOT time on the
  covered share (equivalently: the model predicts default <= 1.2) and all of 3.3 passes.
- **Slice 4, full DN2 core and DT2 (1-2 days of work, then builds).** Regenerate with `--concrete`,
  full oracle suite and sweep, timed exclusive A/B against the backup (`gen-pre-...`, the usual
  rename), then the PGO retrain, then the timed baseline. Keep the old core as a backup directory
  and keep `--concrete` switchable.

Total, if every gate passes: about 2 weeks of work, matching the finding 18 estimate of 1-2 weeks
for a smaller scope; the stopping points above cost 1, 2 and 6 days.

### 3.5 Risks

1. **The mask is already folded** (slice 0/1 finding): gain is only B and C, about 10-15% warm.
   Mitigated by measuring first; cost of being wrong 1-2 days.
2. **The gap is cache, not instructions.** If the in-situ 2x over warm is I-cache, B and C help
   through code size but A does not; the central estimate in the table (2.4-3.0) assumes this.
   Slice 0 measures bytes per SHARC insn; the check that decides it is L1I misses from an Instruments
   CPU Counters run on the reduced gen (already used in the 10-05 handover).
3. **Exit equivalence bugs** that PCM cannot see (a different `Trap` code changes zero-progress
   bails, chain behaviour and interpreter share): the per-form differential and the exit-code
   histogram (compare `s.trap` counts per region between old and new core on the scenarios).
4. **Generator size.** The transpiler is 5.3k lines with a mypy-driven inference pass; a second
   translation mode touches ~40 `bnd` helpers and 274k variants. Partial coverage per variant
   keeps the blast radius small, and the V path stays as the oracle in the same tree.
5. **Compile time and DT2.** Smaller code should shorten the 12 min build; DT2 is regenerated by the
   same flag and needs its own sample check. A failed generation must not leave a half-new core:
   keep the backup directory convention.
6. **Timing validity.** Another agent holds the exclusive baseline and PGO retrain now; use that
   baseline (profile and binary hashes) for the final A/B, and only time under `benchlock`.
   The PGO profile is keyed to the old symbols; a new core needs the retrain before the headline
   number (a stale profile is used with a warning, so compare no-PGO against no-PGO until then).

## 4. Alternatives if mask-free does not hold up, ranked by ceiling

Default config, from 1.43, target <= 1.0. "Ceiling" is the full removal of the cost named, not a
prediction.

| # | lever | ceiling | effort | evidence |
|---|---|---:|---|---|
| 1 | AOT of fast-tier kernels: lower the hottest functions with the fast tier's guarded-window lowering offline (Cranelift to object code in the build), direct calls instead of region calls, "JIT-first" of the 10-06 handover | 0.65-0.8 | 3-5 weeks | kernels 0.5 ns/insn; hybrid-top shows run-time calls (112-266 ns each) kill it, so the offline version must chain without the engine; also gives the browser path a shipped core |
| 2 | Mask-free, section 2 (A+B+C) | 0.8-1.2 (central 1.05-1.2) | 2 weeks | this document |
| 3 | B and C only (no undo/snapshot copies, one trap tail, code size) | 1.2-1.3 | 3-4 days | ablations nosv 0-3%, nobud 7-9% warm; code-size effect unmeasured |
| 4 | PGO retrain on the new core | -5 to -8% | 6 min per retrain, milestone only | earlier retrains 0.92-0.96 |
| 5 | Fewer AOT block exits, larger regions (`--region-insns`, `--region-regs`, chain cap) | -2 to -5% | 1-3 days | full chain uncap gave 1.7% (finding 07); region entry about 14.5M per 3 s |
| 6 | Loop-to-callee inlining in the fast tier | 0.05 in the default config | 1-2 weeks | the 0.47 s of finding 18 is **no-AOT** (4.2M calls x 112 ns); in the default config all fast-tier calls together are 0.62M/s x 112-266 ns = 0.07-0.17 s. Worth it for the no-AOT/browser path, not for the desktop 1.0 target |
| - | Hot-function HLE | none | | not viable: 21 functions for 80% (finding 18) |

Order of attack if slice 0/1 say no: do 3 (cheap, same machinery), then 4, then decide on 1 with
the numbers of the new region benchmark in hand.

## 5. Decision for Em

Approve slice 0 and 1 (2 days, no generator risk, no change to exactness) and let their gate
decide, or skip mask-free for the offline-kernel AOT (alternative 1, 3-5 weeks, higher ceiling,
higher risk). My call: run slices 0 and 1. The one open question that could change the plan is
whether the 2x gap between warm and in-situ AOT is code size or the engine/region entry, and the
slice measures it. Expect 1.05-1.2 from mask-free alone, not 0.8; plan on mask-free plus PGO
plus either B/C or alternative 1 to reach <= 1.0.

## 6. Slices 0-1 results (2026-10-10) **[D]**

Tooling added: `tools/aot_mcount.py` (machine code per SHARC site from a binary and its gen),
`tools/aot_dyntrace.py` (lldb single-step of one region call: machine instructions executed, code
touched), `tools/xctrace_bottleneck.py` (CPU Counters bottleneck split per thread, no root),
`sharc_regionbench.py --features/FEATURES` (cargo features per variant). The upper-bound
experiment is the patch `out/native/regionbench/assume-known.patch` (features `assume-known` and
`assume-flags` in sharc-rt, sharc-native, coupled; `rf_get` returns `assert_unchecked(m == MAX)`).
It is unsound and is not in the tree. Machine: Apple silicon 16 cores (P and E), rustup 1.98.1,
regionbench profile (opt 3, cgu 16, no LTO/PGO), coupled-run default config. PCM
`e0e6c8af7a84c443` (the trig1-taps pin) in every coupled run below, including the experiment.

### 6.1 Slice 0

`region_bench` builds and runs as is (nothing to fix; dca9af1 and the denormal commit left it
sound). Mini gen `mf-base` of the six default regions (30 blocks, 6294 variants) builds in 53 s.

Static machine code (aarch64, `llvm-objdump` of the region functions):

| set | regions | SHARC sites | bytes | machine insns | bytes/site | insns/site |
|---|---:|---:|---:|---:|---:|---:|
| mini gen, 6 default regions + chained | 19 | 688 | 0.78 MB | 194k | 1130 | 283 |
| prod gen (non-PGO coupled-run), the 150 hottest in-situ AOT regions | 150 | 8716 | 7.25 MB | 1.81M | 832 | 208 |

(The mini gen uses the old profile inputs, so its transition arms inflate the cold part; use the
prod row.) A site is one `ic += 1` step. Loads 11-25%, stores 4-11%, branches 14-17%, stack-slot
accesses 5-21% of the machine instructions.

Dynamic (one warm call stepped in lldb, mini gen):

| region | SHARC insns | machine insns | per SHARC insn | distinct addresses | 64-byte lines touched |
|---|---:|---:|---:|---:|---:|
| 1C399A (loop, 25-insn body) | 1600 | 80263 | 50.2 | 1739 | 176 |
| 1C3862 | 2304 | 110452 | 47.9 | 2314 | 233 |
| 1C364F | 1984 | 92744 | 46.7 | 1991 | 199 |
| 1C5765 | 1216 | 65950 | 54.2 | 1410 | 152 |
| B8276C (straight line) | 832 | 74412 | 89.4 | 2891 | 289 |
| B823DB (straight line) | 537 | 55057 | 102.5 | 4309 | 442 |

So 47-54 machine instructions per SHARC instruction in the loops and 89-103 in the straight-line
regions, executed at high IPC when warm (2.0 ns/insn in the loops = 8 cycles for 50 instructions).
Only 15-25% of a region's bytes are executed in a call (hot 200 B of 830-1130 B per site): the rest
is trap tails (one `sv` restore block per Err site), the exit matches and the not-taken variants.

Hot set against the L1 instruction cache (in-situ AOT rows of the region profile, below): the
hottest 25 regions are 87.6% of the AOT instructions (2.3 MB static), 50 are 96.8% (3.9 MB), 100 are
99.9% (5.6 MB). At 16-22% touched that is 0.4 MB for 25 and 0.9-1.2 MB for 100 regions, against 192 KB
(P core) or 128 KB (E core) of L1i; L2 holds it.

Mask folding verdict. For ordinary registers LLVM already folds the masks. In the hot path of
1C399A the 3000 executed instructions I read contain no `.m` AND/compare of a register: the only
`cmn w, #1` are the memory `present` tests. The register prologue loads `m` as the constant
`u32::MAX`. What is not folded is the known-bit logic of ASTATX/ASTATY/STKYX/STKYY (118-121,
`PARTIAL_REGS`): every compute instruction merges `FlagUpdate` define/forget masks into a runtime
(value, mask) pair (`and/orr/csel` chains on `ands w, #0x100` style tests; `_apply_flag_update`,
`_astatx_define/forget`). That is the V plumbing that is left.

### 6.2 Slice 1: the upper-bound experiment

Warm, `compare --rounds 9`, paired median of ratio (experiment / base), same gen and states,
insns per call and exits identical (`constant yes`):

| region | A: ordinary registers assumed known | B: all registers assumed known (flags too) |
|---|---:|---:|
| 1C399A | 1.023 | 0.853 |
| 1C3862 | 0.985 | 0.844 |
| 1C364F | 0.997 | 0.929 |
| 1C2484/B8276C | 0.997 | 0.931 |
| 1C5765 | 1.002 | 0.939 |
| B823DB | 0.995 | 0.881 |

A is zero within the noise (+-2%) and changes the code size by -1.1%: the plan's experiment as
specified has nothing to fold. B (the flag registers assumed fully known as well) is 6-16% faster
warm and 2.4% smaller. In situ (coupled-run, whole core, default config, trig1-taps, exclusive,
alternating, load 2-3, 8 pairs): DSP CPU per audio s base 1.658 (1.525-1.721), B 1.532
(1.472-1.658), paired ratio median **0.935** (0.866-1.014); an earlier 6-pair series (first pair at
load 28 after the builds) gave 0.949. Block entries (31.91M), block traps (315000) and AOT
instructions (2457M) are identical between the two builds, so the exit set is unchanged, and PCM is
the pin. Ceiling of every mask route: **about 6% of the DSP thread in situ, 6-16% warm**, all of it
in the four flag registers. A in situ was not built (warm zero, asm folded).

### 6.3 Warm against in situ

In-situ per-region cost: `coupled-run --profile-regions` (a `region-profile` build, timer overhead
included), trig1-taps, default config: AOT 27.13M calls, 2457M SHARC insns, 12.40 s, 5.05
ns/insn (90.6 insns per call), the rest is fast tier. A least-squares fit of wall time on calls and
instructions over the 1243 AOT entry rows gives **34 ns per call + 4.02 ns per instruction**, so
region entry and exit are 0.4 ns/insn at the mean call size, not the gap. By call size:

| insns per call | calls | share of AOT insns | ns/insn |
|---|---:|---:|---:|
| < 20 | 10.7M | 3% | 12.3 |
| 20-50 | 7.8M | 12% | 5.5 |
| 50-100 | 2.6M | 8% | 6.9 |
| 100-300 | 4.4M | 25% | 5.6 |
| 300-1000 | 1.4M | 27% | 4.5 |
| >= 1000 | 0.17M | 25% | 3.5 |

Only 100 regions carry 99.5% of the AOT time. Important: the six regions of the default region
bench (1C399A, 1C3862, 1C364F, 1C5765, B823DB, B8276C) are fast-tier kernels in the default config
(0.5-1.6 ns/insn in situ); they are not AOT code at run time. Their warm AOT numbers (2.0 in the
loops, 2.9-3.6 straight line) are a fair comparison only for AOT code of the same shape. For the
in-situ AOT regions that the bench can reach (mini gen `mf-ins`, 15 regions, entry chosen by the
capture, not always the in-situ entry; timer resolution 41 ns, so only calls over 100 insns count):
1C9538 5.15 warm / 4.34 in situ, 1C1F44 3.11 / 4.08, 1C2271 3.21 / 6.63, 1C2166 3.87 / 5.55, 1C0C9D
5.05 / 5.48. In-situ is 1.1-1.5x warm (geometric mean about 1.3), not 2x.

Counters (CPU Counters template, CPU Bottlenecks mode, per thread, steady state; `xctrace` exports
four unlabelled values per 1 ms; order calibrated with synthetic programs: a dependent multiply
chain and a 64 MB pointer chase give 90-100% in the second value, 6000 small functions called in
random order (2.6 MB of code) raise the third to 29% from 0, so the values are useful, back end,
front end (instruction delivery), other; the first and last labels are probable):

| thread | useful | back end | front end | other |
|---|---:|---:|---:|---:|
| warm loop 1C399A / 1C3862 / B823DB | 69 / 67 / 71 | 29 / 32 / 27 | 1.4 / 1.5 / 1.6 | 0.3 |
| in situ DSP thread, default config, no PGO (2 runs) | 56 / 52 | 14 / 14 | **28 / 32** | 2 |
| in situ DSP thread, PGO (`pgo-cr-new`) | 61 | 19 | **17.7** | 2 |
| in situ CF thread (interpreter) | 70 | 10 | 14 | 6 |

The hardware retired-instruction and L1i-miss counters themselves are not available (the CLI
cannot select events; kperf needs root). The bottleneck split answers the question: the DSP thread
loses 28-32% of its cycles to the front end in situ (17.7% after PGO), against 1.5% warm.

Where the 4.0 ns goes (in situ, per AOT instruction), from these measurements: about 3.0-3.5
ns is the warm cost of the same-shaped code (89-103 machine instructions per SHARC insn at high
IPC for straight-line forms); about 1.1 ns is front-end stall (28% of the cycles); 0.4 ns
region entry/exit. Inside the warm part: flag known-bit logic 6-16% (B), the per-instruction budget
check 7-9% and undo copies 0-3% (earlier ablations `abl-nobud`, `abl-nosv`, finding above), the
`rf.o` snapshot copies unmeasured (`abl-noo` is broken), the rest is the semantics (float flags, denormal
flush, memory path).

### 6.4 Gate

Is the in-situ cost dominated by mask handling? **No.** Ordinary-register masks are already folded
(0%); the flag registers' masks are worth about 6% in situ. Full mask-free (concrete `u32` registers,
`bndc`, slice 3) has a ceiling of that 6% plus whatever C and B give, and it is the most expensive
route: not worth building. Is it undo copies and trap tails? **Partly, but small as time**: undo
0-3%, budget 7-9% (a different exactness question), snapshots unknown; as code, trap tails are
most of the 75-85% cold bytes. Is it code size, entry/exit or I-cache? **Entry/exit no (0.4
ns/insn); front-end stalls yes, the single largest identified item**: 28-32% of DSP cycles before
PGO, 17.7% after, and the straight-line regions pay it every call.

Ceilings per route, default config, trig1-taps, DSP CPU per audio s, from the two bases (no PGO
1.59, PGO 1.17); a ceiling removes the named cost completely, none is a prediction:

| route | what it removes | ceiling (no PGO / PGO) | basis |
|---|---|---:|---|
| full mask-free (A+B+C, concrete registers) | flag masks, nothing else from A | 1.49 / 1.10 | measured 0.935 |
| sound flag-known gate only (ASTAT/STKY m == MAX at entry, cold exit when a forget would drop a bit) | same 6% at 1 day of work | 1.49 / 1.10 | B above; exits identical in this scenario |
| undo copies + trap tails as code (B, C) | 0-3% time, 15-25% of the bytes' density | 1.45-1.55 / 1.07-1.14 | ablations |
| per-body budget check | 7-9% of warm AOT | 1.50 / 1.12 | ablation, needs the exactness argument of section 2 |
| smaller hot code (fewer variants, outlined cold paths, shared trap tail, hot/cold layout) | up to all of the front-end stall | 1.08-1.14 / 0.96 | 28-32% / 17.7% of the thread; half of it is a realistic aim: 1.35 / 1.07 |
| offline-AOT kernels with chaining (section 4 alternative 1) | AOT time itself | 0.65-0.8 | unchanged, still the only route to <= 0.9 |

Decision: **do not do slice 3 (concrete registers)**. The cheap, sound part of B (flags known at
entry) is worth about 6% and is a one-day slice; the larger lever is hot-code density (front
end), which also pays for itself on the browser path. Mask-free as designed would not reach 1.0 even
with PGO (1.10 at best); 1.0 needs density plus the flag gate plus the budget check (about 1.0-1.05
with PGO, optimistic), or the offline-kernel route.

### 6.5 Next slice

Slice 2' (2-3 days, measured on the same `mf-base` and `mf-ins` mini gens with `aot_mcount`,
`aot_dyntrace` and `xctrace_bottleneck`, then one exclusive in-situ A/B and one PGO retrain at
the end): (1) sound flag-known gate: ASTATX/ASTATY/STKYX/STKYY must be fully known at region entry
(extend the entry AND) and a flag update that would forget a bit becomes a cold exit with the
same trap code as `rf_set` (check the exit histogram on the scenarios, they are identical in
trig1-taps today); (2) one shared trap tail per region and no `sv` restores where every Err
precedes the first commit (section 2 B and C, byte-checked per form); (3) put the rarely taken
exit matches and tails out of line (`#[cold]` functions or a region-end block), then read
bytes/site (target <= 400), lines touched and the front-end share before timing anything. Gate for
continuing: front end below 12% without PGO and warm >= 10% faster; otherwise stop and go to the
offline-kernel route.

Open **[O]**: the order of the bottleneck values rests on the synthetic calibration, not on a
documented mapping; hardware retired-instruction counts were not obtained, the instruction counts
are from single-stepping; the in-situ vs warm region comparison used entry points that differ from
the in-situ ones; the flag-known gate's soundness (that no scenario forgets a flag bit in AOT code)
is shown for trig1-taps only (identical traps and entries), not for the sweep.
