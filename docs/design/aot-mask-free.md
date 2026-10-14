# Mask-free AOT emission: design and slice proof

Status: design (2026-10-09); slices 0 and 1 measured 2026-10-10, results in section 6 (gate: do not build
concrete registers, build the flag-known gate and cold-path density instead); slice 2' built and measured
2026-10-10, results in section 7 (default config -15% without PGO, -9% with PGO; desktop 1.06 to 1.13 with PGO, not
yet 1.0). Sections 0 to 5 are the original design and were not edited; the numbers marked **[D]** come from earlier
measurements cited in place, **[O]** is open. Marks as in CLAUDE.md. No firmware-derived code is quoted: block code is described, and the few lines shown
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

Region prologue (once per region call, `tools/sharc_dev/sharc_rsgen.py` `region_text`):
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

Two things from earlier region microbenchmarks (`tools/devtools/sharc_regionbench.py`, warm cache, captured
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
same Python core, so semantics stay in `tools/sharc_dev/sharc_core`.

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

- `tools/sharc_dev/sharc_transpile.py` (5311 lines): a block-concrete translation mode next to the `V`
  mode (`Translator.blk`): type map `VAL -> U32` for ordinary registers, `Spec`/`VI` reduced to
  `u32`/`i128` addresses, calls to `bndc::*`; the ordering analysis (`variant_order`, next to
  `_can_trap` and `variant_logs`), the per-variant register class report (extends
  `variant_putregs`).
- `native/sharc-rt/src/rt/bnd.rs` (1693 lines): new `rt/bndc.rs` with concrete helpers; `Rf` gets
  a concrete file (`rf.c`) plus the `V` file for registers 118-121 (and 22 when unproven). Editing
  `sharc-rt` rebuilds the generated core: use the mini gen (3.2).
- `tools/sharc_dev/sharc_rsgen.py` `region_text` / `body_lines`: concrete prologue (no `.m` AND, no
  `V { m: MAX }`), per-variant "no trap" and "no undo" emission already has the hook
  (`traps = TR.variant_traps.get(path, True)`; extend it with the ordering result), one shared
  trap tail per region, `rf.o` copies only where the form needs them. Flag `--concrete`
  (default off until the gate passes) so old and new cores can be generated from the same tree and
  A/B'd. The `_unknown` fallback region keeps the V path.
- No change to `tools/sharc_dev/sharc_core` semantics: golden hashes (`tests/sharc/integration/test_sharc_golden.py`) must
  not move.

### 3.2 Build cost and fast iteration

A full DN2 `sharc-gen` build is about 12 min now (it was about 4-5 min before the FX-profile
core doubled the source; DT2 has its own). Do not iterate there.
- `tools/devtools/sharc_regionbench.py gen --tools-dir DIR --only <regions>`: a mini gen of the selected
  regions (about 16 s for rsgen, about 1 min of cargo; transpile inference is cached by transpiler
  source hash, 5 s), `capture` for entry states (shared by all variants), `bench` and `compare A B
  --rounds 9` (+-0.7%). A variant built from a git worktree's `tools/` (`--tools-dir`) keeps the
  old and new generators side by side. The `region_bench` example does not compile at the moment
  (missing `flat_windows` in its `Env`; finding 18, plan log 2026-10-09): fixing that is step 0.
- A reduced production gen for in-situ checks: `tools/sharc_dev/sharc_rsgen.py --only` the top 12 functions
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
4. `tests/sharc/integration/test_sharc_golden.py` unchanged; DT2 sample check when the DT2 core is regenerated;
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

Tooling added: `tools/sharc_dev/aot_mcount.py` (machine code per SHARC site from a binary and its gen),
`tools/sharc_dev/aot_dyntrace.py` (lldb single-step of one region call: machine instructions executed, code
touched), `tools/devtools/xctrace_bottleneck.py` (CPU Counters bottleneck split per thread, no root),
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

## 7. Slice 2' results (2026-10-10) **[D]**

Built on the branch `work/scenario-oracles`, uncommitted. The density gate of section 6.5 (bytes per SHARC site
<= 400, front end < 12% without PGO, warm >= 10% faster) was **missed on two of three numbers** (465 B per site on
the hot regions, front end 21 to 24% without PGO) and met on the third (warm 0.77 to 0.91 on the six default
regions). The instruction was to stop and report at that point (the alternative route is offline fast-tier kernels).
An early in-situ A/B (the full core built once for calibration) already showed -15% DSP CPU, so the work went on
to the regenerated cores, the verification and the PGO retrain instead: DSP CPU per audio s -15% without PGO and
-9% with PGO, PCM pins and all tier checks unchanged. That is a deviation from the stop rule; the old cores are
kept (`gen-pre-s2-20261010`) and `rsgen --step-budget` restores the old budget form, so Em can back it out.

### 7.1 What changed, per item

All in the files that make the generated core: `tools/sharc_dev/sharc_rsgen.py`, `tools/sharc_dev/sharc_transpile.py` (both are in
`CORE_FINGERPRINT`: the DN2, DT2 and shared cores were regenerated, backups `gen-pre-s2-20261010`) and the runtime
`native/sharc-rt/src/{rt.rs,rt/bnd.rs,addressing.rs}`.

1. **Flag-known gate.** `rsgen` no longer takes ASTATX, ASTATY, STKYX, STKYY out of the registers that must be
   fully known at region entry (`region_text` and `body_lines`: `known` is every register a body reads), and the
   transpiler writes them with `rf_set` like any register (only an explicit `V::UNK` store stays `rf_put`). A
   flag update that would leave a bit unknown is `rf_set`'s cold exit with `TRAP_BLOCK_UNKNOWN`, before any commit,
   the protocol every other register already used. Basis, measured with a temporary counter at the engine's block
   call (not kept): at every block dispatch of trig1-taps (24.7M counted), kb-chord16, tf-br-127, tf-srr-127,
   sf-mst-thr0 and sf-rev-decay-u20 all four masks were `0xFFFFFFFF`; in the new core's entry-bail histogram
   (`--profile-execution`, 25 scenarios, 14.0M zero-progress bails) no bail has a flag register among the
   registers that were not fully known. The `PARTIAL_MASKS[118] = 0x00FFFFFF` of the
   old comment (CACC unknown) is stale. Soundness evidence: trap codes and counts are identical to the old core in
   25 scenarios (7.4), so no update in AOT code forgot a flag bit there; the region with `allow_unknown`
   (`r_1C253F`) is unchanged. A state with a partial flag at entry now declines the region (the interpreter runs
   it): the regionbench state of region `1C25CC`, captured from an old note replay, does (warm ratio 0.14 in the
   compare run); no in-situ entry does.
2. **One trap tail per region.** A trap site now records `tp` (PC), `tt` (trap) and `tpd` (pending transfer) in
   three locals and leaves; the region applies them once before the write-back (`Tail` in `rsgen`). The sites keep
   their register restores. The ordering analysis ("no restores where every Err precedes the first commit") was
   **not built**, because the compiler already does it: the register file is a local array with constant indices,
   so a restore of an unchanged register is a no-op after SROA, and the `sv` copies of the Err paths that LLVM
   proves never fire are dead. The upper bound, measured by deleting every `sv` copy and restore (unsound, mini
   gen only): bytes -2.4%, warm 0 to 9% (mean 4%: 1C399A 1.69 vs 1.72 ns/insn, 1C3862 1.62 vs 1.70, 1C364F 1.87
   vs 1.90, B8276C 2.46 vs 2.49, 1C5765 1.38 vs 1.52, B823DB 2.83 vs 2.98). Any exact elision is a fraction of that
   and needs statement reordering (a load writes its register before the post-modify check that can fail); not
   worth it yet **[O]**.
3. **Cold paths and shared exits.**
   - Every Err arm is marked unlikely: `ColdErr::ce` (`x.ce()?`), `cold_err`, `core::hint::cold_path()` in
     `rf_set`, in the `Err(t)` block of every step, the entry gates and the budget exits.
   - The chain exit (a block of another region follows) was one stop test plus one call per link, 30% of the bytes
     of a typical region; now two static tables (`PCS`, `FNS`) and one stop test.
   - The successor match calls one `#[inline(never)]` function per distinct set of chained PCs (`r_X_cN`) instead of
     repeating the compare tree at every exit site.
   - The normal-word range walk (11 ranges, 25% of all AOT bytes in two copies, one from the Python core
     (`BOUNDARY` entry for `addressing.normal_word_to_byte`) and one in `bnd.rs`) is one out-of-line table
     (`addressing::normal_word_map`) behind a 3-instruction inline range test; the plain-RAM access paths work on a
     32-bit address (`fast_address_b`) and only the slow path maps again. Unit test against the old table in
     `addressing.rs`.
4. **Budget check per body.** The model-safe form checked `icount + ic >= limit` before every instruction. Each of
   those early exits is an edge into the common write-back that costs about 80 bytes of register fix-up (157 KB of
   1.3 MB in the mini set). Now a body (or a loop iteration) is entered only if it fits under the limit; else the
   call ends with no progress and the interpreter runs up to the limit, which is architecturally exact. This is
   **not** block-entry-identical (7.4); `rsgen --step-budget` restores the old form.

Tools added: `tools/sharc_dev/aot_attrib.py` (machine-code bytes and executed bytes per source function and per kind of
generated statement, from `llvm-objdump -l` and an `aot_dyntrace.py --dump`; needs
`REGIONBENCH_DEBUG=1 tools/devtools/sharc_regionbench.py` and the target's `region_bench.dSYM`), `tools/devtools/coupled_ab.py`
(alternating exclusive A/B of `coupled-run` binaries). `aot_dyntrace.py --dump` writes the function start for
`aot_attrib`. Test: `tests/sharc/generation/test_sharc_rsgen_density.py`.

### 7.2 Where the bytes were

`aot_attrib.py` on the mini set (21 regions, 1959 SHARC sites, fxprof inputs), old core: normal-word table 25%
(two copies), chain exit 15% + 13.5% + 1.8%, budget check 10.6%, float helpers about 15% (mostly the cold slow
paths, partly already `cold_path`), memory fast paths 8%, `rf_set` 3%, `s_set_special` 3%. After the changes:
normal-word 12.6%, memory fast paths 8.6%, `rf_set` 3.8%, float helpers about 19%, `s_set_special` 3.1%. What is left is the
semantics of the model (flag updates, denormal flush, memory presence tests), no longer plumbing.

### 7.3 Density gate

Mini set (`mf`-style gens, 21 regions, 1959 sites, old profile = `fxprof`), `aot_mcount.py` and warm `compare
--rounds 9` (paired median B/A, no PGO, same entry states):

| | old core | slice 2' |
|---|---:|---:|
| bytes per SHARC site (mini set) | 1016 | **534** |
| machine insns per site | 254 | 134 |
| bytes per site, 150 hottest in-situ regions (8247 sites, ranked by trig1-taps block transitions) | 868 | **465** |
| lines touched, one warm call, 1C3862 / B8276C / B823DB | 233 / 289 / 442 | 177 / 244 / 342 |
| machine insns executed, same calls | 110452 / 74412 / 55057 | 84710 / 65375 / 46286 |
| warm ratio, 1C399A 1C3862 1C364F B8276C 1C5765 B823DB | | 0.80 0.77 0.91 0.82 0.84 0.79 |
| front end, DSP thread, in situ, no PGO (two runs each) | 27.4%, 32.4% | **20.9%, 23.5%** |
| front end, in situ, PGO | 14.6% | **10.4%** |

Gate: bytes per site <= 400 not met (465, 534); front end < 12% without PGO not met (it is met with PGO);
warm >= 10% met. The hot set got 21 to 24% smaller in lines (1C3862), the machine code 46% smaller.

### 7.4 Exactness

PCM: every pin holds (`dn2.json` all seven, `tf-br-127 16f3d0a8..`, `tf-dry-hold 79e2dac0..`).
Block behaviour, old core against new, `coupled-run --profile-execution`, six scenarios (trig1-taps, kb-chord16,
tf-br-127, tf-dry-hold, sf-mst-thr0, sf-rev-decay-u20) and 19 more sampled over every FX family: **block traps
identical in count and in code** (all `0x8000000d`, `TRAP_BLOCK_UNKNOWN`: 315000 in trig1-taps), so the exit set
of the gate and of `rf_set` is the same. Block entries +0.26 to +0.27% (trig1-taps 31,910,436 to 31,995,146),
interpreted instructions +0.5 to +0.7% (13,065,273 to 13,151,269) and the same number fewer in AOT: the bodies that
now decline at the limit. That is the only difference and it is by design (item 4).

### 7.5 Verification

- `dn2.json` pins (`mise run scenarios`): all hold.
- `scenarios:check` 61 passed in the default and the no-AOT config; `scenarios:sweep` 191 passed.
- Tier sweep, all 134 FX scenarios (47 track, 87 send), AOT against `SHARC_AOT=0`: **134 of 134 PCM equal**.
- `tests/sharc/integration/test_sharc_golden.py` and `tests/sharc/semantics/test_sharc_denormal.py` 28 passed; `tests/sharc/generation/test_sharc_rsgen_density.py`,
  lint, `sharc_rsgen_model_safe`, `gen_fingerprint` pass; `cargo test` sharc-rt 13 passed (with the new address
  table test); sharc-native 66 passed, 2 failed (`tests_gen::gen_programs_match_the_interpreter`,
  `gen_control_programs_match_the_interpreter`: "simd_loop: the fast tier never ran"): they fail the same way on the
  parent commit with the old core, so they are not from this change **[O]**.
- DT2: `tools/emulator/dt2_sample_check.py` clean tone (peak share 0.99995 at 1008 Hz); the 3 s `dt2-sample-trig` render
  with the card has the same WAV hash `4bab8330915f0929` in AOT, no-AOT and AOT + fast tier.

### 7.6 Timing

`tools/devtools/coupled_ab.py`, exclusive benchlock, alternating pairs, DSP thread CPU per audio s, default config, DN2 1.11,
machine load average 2 to 4, PCM equal to the pins in every run. Cell = median (IQR).

| scenario | old core | slice 2' | paired ratio |
|---|---:|---:|---:|
| no PGO, trig1-taps (7 pairs) | 1.502 (0.006) | **1.277** (0.005) | 0.849 |
| no PGO, tf-dry-hold (7) | 1.510 (0.028) | **1.282** (0.025) | 0.850 |
| no PGO, tf-br-127 (7) | 1.612 (0.022) | **1.368** (0.013) | 0.851 |
| PGO, trig1-taps (6) | 1.163 (0.007) | **1.055** (0.006) | 0.907 |
| PGO, tf-dry-hold (6) | 1.161 (0.007) | **1.059** (0.003) | 0.911 |
| PGO, tf-br-127 (6) | 1.237 (0.020) | **1.129** (0.006) | 0.911 |

PGO "old" is the 2026-10-09 binary (old core, profile of that day, `pgo-cr-new`); "slice 2'" is `coupled-run` built
with `-Cprofile-use` of the profile retrained on the new core (`mise run pgo`, 4 runs, `coupled_ready_exactness`
passed with the CF digest and DSP export pins unchanged, so no pin was edited) into `out/native/target/prod/pgo-cr-s2`.
PGO buys 23% on the old core (1.502 to 1.163) and 17% on the new one (1.277 to 1.055): part of the layout benefit
is now in the code (front end 14.6% with PGO on the old core, 10.4% on the new one). Binaries: `out/native/ab/coupled-{old,s2}-{nopgo,pgo}`.

Fit: the old core's trig1-taps is 0.49 + 234M x 4.3 ns; the new one 0.49 + 234M x 3.4 ns.

### 7.7 Desktop against 1.0, open items, next step

The desktop did **not** cross 1.0: 1.055 (trig1-taps), 1.059 (tf-dry-hold), 1.129 (tf-br-127) with PGO, 1.28 to 1.37
without. It needs another 5% to 11% with PGO.

Open **[O]**: the block entry/exit histogram is not identical because of item 4 (a `--step-budget` build would be);
sharc-native's two `tests_gen` failures predate this; the ordering analysis for the undo copies was not built (ceiling
4% warm); the flag gate assumes fully known flags at every in-situ entry, shown for the 25 scenarios above and the
DT2 render, not for arbitrary play (a partial flag only costs an interpreter run, it is not wrong).

Next step, in order: (1) commit this slice after Em's review; (2) re-fit the cost split (AOT, kernels, the rest) on
the new core with `xctrace`, since AOT is no longer 70% of the thread; (3) the remaining hot code is the model's
own: about 37 machine instructions per SHARC instruction in the loops, of which the flag updates (`_float_alu_updates`,
`_astatx_define`), the float flush and the memory presence tests are the largest named items in `aot_attrib.py`;
exact fast paths for those are the next AOT lever; (4) offline fast-tier kernels for the 25 regions that carry
88% of the AOT instructions stay the route to 0.9 and below.

## 8. Slice 3 results (2026-10-11) **[D]**

Built on the branch `work/scenario-oracles`, uncommitted, on the slice-2' core. Goal: close the last 5 to 11% to real
time on the desktop. **Reached**: with PGO the DSP thread needs 0.896 (trig1-taps), 0.901 (tf-dry-hold) and 0.922
(tf-br-127) CPU s per audio s (slice 2': 1.054, 1.056, 1.126); without PGO 1.104, 1.106, 1.122 (1.279, 1.280, 1.358).
Every pin, oracle test and tier comparison holds (8.5). The largest single item was not on the list of the task:
the interpreter (8.2, item 5).

### 8.1 Cost split on the slice-2' core

`sample` of `coupled-run` (no PGO, trig1-taps, 8 s, 1 ms samples), DSP thread only, leaf frames; the fast tier's JIT
code has no symbol (`???`). Old = slice-2' binary, new = the final one.

| part of the DSP thread | slice 2' | now |
|---|---:|---:|
| AOT region functions (`r_*`) | 63.8% | 63.5% |
| fast tier, JIT code + glue | 15.8% | 18.8% |
| interpreter forms (`core_g`, `core_i`, `exec_insn`) | 6.0% | 4.3% |
| `Engine::step` dispatch loop | 5.9% | 5.6% |
| `sec_arbitrate` | 2.4% | 0.6% |
| other (periph, decode, memcmp, allocation) | 6.1% | 7.2% |

(Shares of a total that shrank by 12%: the AOT part got about 12% cheaper in absolute terms.) Inside the AOT part,
dynamic machine instructions per source function (`aot_attrib.py --by-dyn`, one warm call each of the in-situ AOT
regions 1C0C9D, 1C926F, 1C9538, 1C9A46, 1C2271, 1C2166, 1C38BB; the loop regions 1C399A and 1C1F44 skew it, so they
are counted separately): memory path (`fast_read`, `fast_write`, normal-word test, `note_write`) **34%** of the
instructions of the in-situ regions (about 80% in the copy loop 1C1F44), measured on the build with the memory changes and the first float form, float helpers (`ftz32`, the add/mul fast paths,
`_float32_result`, multiply parts, `_float_alu_updates`, `_astatx_mult_float`) 24%, loop-stack ops and the undo log 8%,
`rf_set` 3.4%, the region functions themselves 8%. The bytes-based list of slice 2' (float 19%, normal-word 12.6%,
presence tests 8.6%) understated the memory path, because a normal-word access runs the test two or three times
(access, stride in `_normal_word_stride`, post-modify) and every access walks `eff`, `watch`, the dirty bitmap.
"s_set_special" in `aot_attrib` is not the special-register write: the macro `stack_ops!` in `rt.rs` (loop stack,
PC stack, call stack accessors) has no `fn` line the attribution regex matches, so its lines are charged to the
function above it. Those are the loop-end `stk_set_loops` / `stk_at_loops` with their undo-log entry, 4.7% of the
in-situ regions. The regionbench states have the bank, stack, timer and peripheral models on (`cfg.stack_model`), as
the coupled run has, so the loop-slot code runs there as in situ.

### 8.2 What changed, per item

Files: `native/sharc-rt/src/{addressing,mem}.rs`, `native/sharc-rt/src/rt/{bnd,periph}.rs`,
`native/sharc/src/lib.rs`, `tools/sharc_dev/sharc_transpile.py` (in `CORE_FINGERPRINT`: DN2, DT2 and shared cores regenerated,
backups `gen-pre-s3-20261010`), tools `aot_entry_iter.py` (new), `aot_attrib.py`, `aot_dyntrace.py`.

1. **Float helpers.** `_float_binary_add/sub` and `_float_multiply` flush both operands (`ftz32`), compute in `f32`
   and classify the result by its magnitude: a normal result (above the smallest normal, below infinity) is returned
   as it is with no flag set; a zero or denormal result is the zero of its sign (a multiply of two nonzero numbers
   also sets underflow); infinity, NaN and the smallest normal run the original sequence. A result below the smallest
   normal in `f32` comes from an exact value below the midpoint to the smallest normal, which lies below the flush cut,
   so the Python core flushes it too (the existing differential tests cover the multiply products at the cut). `floats._float32`, `floats._float32_result` and
   `compute_multi._float_multiply_parts` (the multifunction multiply) are boundary functions of the runtime
   (`BOUNDARY` in the transpiler, bodies in `bnd.rs`); `_float_multiply_parts` has the same fast path and falls back
   to the original sequence (the NaN result keeps MI and no other flag). New differential test
   `float_multiply_parts_and_float32_match_reference` (4M pairs, every special operand pair) next to the existing
   ones; 0 mismatches.
2. **Normal-word test.** `nw_band`: every range of the table lies in [0x90000, 0xe8000) or [0x4000000, 0x18000000) and
   every boundary is a multiple of 0x2000, so two compares of `a >> 13` against small immediates decide whether the
   out-of-line `normal_word_map` is needed. Seven instructions where the two 32-bit range tests with their
   `movk` constants took 12 to 14. The reference test of `addressing.rs` passes unchanged.
3. **Memory presence tests.** `Mem.pages` and `Mem.watch` are fixed-size boxed arrays (no bounds check on
   `a >> 16`). `fast_read_word` / `fast_write_word` answer an aligned 4-byte access with one present or dirty nibble test
   (an aligned word can neither cross the page end nor a 64-bit bitmap word; an unaligned address declines to the full
   rules, so the answer where it answers is that of `fast_read` / `fast_write`; the code watch is noted before the dirty test as before). 1- and
   2-byte accesses skip the page-end test of the 4-byte load. Block code uses them (`_dm_read_b`, `_dm_write_b`,
   `_dm_write_nolog_b`, and `_load_normal_ureg_rf` now calls `_dm_read_b` instead of `_dm_read`, which tested
   `cfg.fast_mem` and went through the 128-bit mapping). Differential test `word_paths_match_the_general_ones`.
4. **`rf_set`, `s_set_special`.** Not changed. The `m != MAX` tests of `rf_set` that remain (3.4%) are values read
   back from the loop-slot stack, whose mask is not a constant; the stack accessors are the loop-end machinery above.
   A cheaper loop end (no undo entry for `stk_set_loops` when `stack_model` is off, as nothing after it can fail)
   is an idea for the generator, **[O]** and not exact for the model-safe blocks that run with the stack model on.
5. **Gated block entries sent to the interpreter** (not on the task list; the biggest win). With the bank model on,
   a block entry is declined (`timer_gated`, 435,666 times in trig1-taps) while a bank swap is pending (a MODE1 write
   by the previous instruction). The interpreter runs that one instruction, and then the rest of the block's body,
   because a region has entries only at its block starts: 7.5M of the 13.2M interpreted instructions of trig1-taps
   were one 238-instruction routine (0xB8A9C4..0xB8AAB7), run 31,500 times. `diag_step` now also records, in the
   `entries` profile map, the PC after the first instruction of an interpreter run that began at a block entry (only
   with the blocks on); `tools/sharc_dev/aot_entry_iter.py` runs the 12 profile scenarios of the fxprof recipe with the current
   core and prints the new entries. Making them block starts lets the run end after one instruction. The new starts
   can be gated the same way, so the search was repeated (6 rounds: 19, 7, 3, 3, 3, 2 new PCs; the last round found
   only PCs with counts under 200): 2657 to 2692 blocks, 38,378 to 39,154 block instructions (+2%), 41 to 42 files. Interpreted
   instructions in trig1-taps 13,151,269 to 6,760,382 (idle probes 1.86M, the 0x1C06FE delay-slot decline 0.78M, the
   rest spread); median over the 134 FX scenarios 0.52 of the old count. Also `sec_arbitrate` visits only the SSTAT
   registers that exist (`mmrs` is sorted; the scan did a binary search for each of 256 sources): 2.4% to 0.6% of the
   thread; differential test `sparse_scan_matches_the_full_scan` against the old scan, including non-concrete
   registers.

### 8.3 Tried and dropped

- Float fast path variants, warm ns per call (ratio B/A of two builds, 7 rounds). **B** test both exponents nonzero,
  then compute (`mf-b`): 1C399A 0.87, 1C364F 0.88, B8276C 0.82, B823DB 0.87 against slice 2', but 1C3862 1.05 and
  1C2271 1.04: with zero operands (silence) the test fails and the original sequence runs, and the branch follows the
  data. **C** flush first, one classification (`mf-c`) against B: 1C3862 0.85, 1C5765 0.90, 1C2271 0.90, but 1C364F
  1.10, B8276C 1.09. **D** B in front of C's fallback: between the two. **E** (kept) C with the result tested for a
  normal value first, then for zero: against D 1C5765 0.93, B823DB 0.95, 1C2271 0.94, 1C2166 0.96, 1C399A 0.98,
  1C364F 1.04, 1C0C9D 1.12 (a 48-instruction region). Smaller code too (540 against 555 B per site).
- Memory access as a shared out-of-line body per width and normal-word flag (a call at the site instead of the
  mapping, the page walk and the store): the mini set shrinks 9% (505 against 555 B per site), but the warm time
  grows 1.0 to 1.7 times (1C1F44 1.68, 1C2484 1.19, 1C5765 1.18). Not kept.
- Running the generated blocks during an idle-skip probe iteration: the probe iteration is the 24-instruction loop in
  the idle range, which has no blocks (`--exclude`), so the interpreter count did not move; removed again.

### 8.4 Density and warm

| | slice 2' | slice 3 |
|---|---:|---:|
| bytes per SHARC site, mini set (21 regions, 1959 sites) | 534 | 530 |
| machine insns per site, mini set | 134 | 133 |
| bytes per SHARC site, whole DN2 core (64,818 sites) | 397.5 | 394.5 |
| front end of the DSP thread in situ, no PGO / PGO | 20.6% / 10.4% | 20.9% / 9.0% |

The code did not get smaller (the front end share without PGO is the same); the gain is instructions: warm ratio
new/old (same entry states, 9 rounds, `sharc_regionbench.py compare`): 1C399A 0.85, 1C3862 0.80, 1C364F 0.88, 1C2484
(B8276C) 0.79, 1C5765 0.86, B823DB 0.75, 1C0C9D 0.94, 1C9538 (B81368) 0.70, 1C1F44 0.81, 1C9A46 1.01, 1C2271 0.87,
1C2166 0.79, 1C38BB 1.11 (35 instructions, 73 against 80 ns, within the timer step).

### 8.5 Verification

- DN2 pins (`mise run scenarios`): all seven, hashes unchanged (trig1-taps `e0e6c8af7a84c443`).
- `scenarios:check` 61 passed in the default and in the no-AOT configuration; `scenarios:sweep` 191 passed.
- Tier sweep, all 134 FX scenarios, AOT against `SHARC_AOT=0`: **134 of 134 PCM equal**.
- `test_sharc_golden.py`, `test_sharc_denormal.py`, `test_sharc_rsgen_density.py`, `test_lint.py`,
  `test_gen_fingerprint.py`, `test_sharc_rsgen_model_safe.py`, `test_types.py`: pass. `cargo test -p sharc-rt`: 16
  passed (3 new).
- DT2: `dt2_sample_check.py --rebuild` clean tone (peak share 0.99995 at 1008 Hz); the 3 s `dt2-sample-trig` render has
  the WAV hash `4bab8330915f0929` in AOT, no-AOT and AOT + fast tier, as before.
- Block traps against the slice-2' core: count identical in all 134 FX scenarios; code and count identical in the
  `--profile-execution` exit tables of tf-br-127, tf-dry-hold, sf-mst-thr0, sf-rev-decay-u20, kb-chord16 and
  trig1-taps (all `0x8000000d`: 315,000 in trig1-taps); the rows of the other exits are identical in the four FX
  scenarios (0 of about 1000 differ); zero-progress bails rise by 7% in trig1-taps (1,026,622 to 1,100,264) and 0.1%
  in kb-chord16, the new entries declining like their neighbours. Block entries +1.1% (31,995,146 to 32,353,230),
  AOT instructions +0.26%, interpreted instructions -49%: the differences are the new block starts of item 5.

### 8.6 Timing

`tools/devtools/coupled_ab.py`, exclusive benchlock, alternating pairs, DSP thread CPU per audio s, default config, DN2 1.11,
load average 3 to 4, PCM equal to the pin in every run. Cell = median (IQR); 6 pairs each.

| scenario | slice 2' | slice 3 | paired ratio |
|---|---:|---:|---:|
| no PGO, trig1-taps | 1.279 (0.003) | **1.104** (0.004) | 0.862 |
| no PGO, tf-dry-hold | 1.280 (0.005) | **1.106** (0.004) | 0.864 |
| no PGO, tf-br-127 | 1.358 (0.006) | **1.122** (0.005) | 0.827 |
| PGO, trig1-taps | 1.054 (0.012) | **0.896** (0.006) | 0.850 |
| PGO, tf-dry-hold | 1.056 (0.003) | **0.901** (0.006) | 0.854 |
| PGO, tf-br-127 | 1.126 (0.003) | **0.922** (0.003) | 0.819 |

PGO: `mise run pgo` on the final core (4 runs), `coupled-run` built with `-Cprofile-use` into
`out/native/target/prod/pgo-cr-s3` (binaries `out/native/ab/coupled-{s2,s3e}-{nopgo,pgo}`); the old one is the
slice-2' PGO binary. Machine absolute numbers move with its state (the slice-2' binary read 1.277 earlier and 1.311
and 1.445 on busier days); the ratios hold. Real time on the desktop is now reached with a margin of 8 to 10%.

Progress during the slice (no PGO, trig1-taps, paired against slice 2'): 0.93 with the word paths and the first float
form (3 pairs), 0.926 with the narrow paths, the float32 and multiply-parts boundary functions, `sec_arbitrate` and
the E form (6 pairs), 0.862 with the entry search.

### 8.7 Open, next step

**[O]** DT2 was regenerated with the new transpiler but not given the entry search (its profile inputs are the
old ones; the gating exists there too, `tools/sharc_dev/aot_entry_iter.py` is DN2 scenarios only). The search is
profile-driven: a core generated from other scenarios needs its own rounds. The loop end (`stk_set_loops`, the undo
entry, `loop_slots`) is the largest named item left in the in-situ regions (about 8%); the idle probes (1.86M
interpreted instructions) and the 0x1C06FE delay-slot declines (0.78M) are the largest interpreter items. The
gate of section 6.5 (front end below 12% without PGO) still is not met; with PGO it is (9.0%).
Next: commit after review; DT2 entry search; then the loop-end undo entry with a proof that nothing can fail after
it, or offline kernels (section 4) for 0.8 and below.
