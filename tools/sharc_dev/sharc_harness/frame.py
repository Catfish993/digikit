"""Frame setup, frame calls and companding records."""

from __future__ import annotations

import time
from collections.abc import Sequence

from tools.sharc_dev import sharc as sharcmod
from tools.sharc_dev import sharc_run as sr
from tools.sharc_dev import sharc_survey as sv

from .layout import profile
from .runner import _dm_word, _poke, run_init
from .voice import _write_samples, setup_voice

# --- Frame render entry point (lane L13, 2026-09-25) ------------------
#
# render_blocks()/call_render() above call FUN_1c4ecf (one voice's render)
# directly. This section instead calls FUN_1c2b24 (render_frame, the whole
# frame's orchestrator -- docs/findings/06's "FUN_1c2b24 is the per-frame
# render orchestrator") the way the firmware actually reaches it: through
# block_handler (0x1c74cd), the audio task's real command dispatcher,
# rather than jumping into render_frame bare with hand-set R4/R12/R8/stack
# registers. Concretely tracing the real dispatch (this lane's own
# byte-level read of block_handler/FUN_1c75d8's cmd_handler_3 body, cross-
# checked by running it) found register plumbing that a purely static
# read of "the call site" (this lane's own task brief) could not resolve
# from FUN_1c75d8 alone: R12 at the CALL 0x1c2b24 site is `I3`, and I3 is
# never assigned anywhere in cmd_handler_3's own body or in the dispatch
# preamble (0x1c7501-0x1c7521) that runs before it -- it is `DM(I6-3)`,
# one of *two* by-reference outputs command_dispatch_fn (0x1c778a) writes
# into block_handler's own stack frame (`R4=&(I6-3)`, `R8=&(I6-2)` passed
# into that CALL at 0x1c74f8); the *other* output, `DM(I6-2)`, is exactly
# the "R1 = DM(I6-2) of the task frame" this lane's brief already named.
# Getting both right by hand (let alone the ring-buffer/`command_word`
# plumbing above them) is exactly the kind of bookkeeping a *real* call
# gets for free -- so `setup_frame()` only pokes the three DM cells that
# select which of the 4 audio-task commands runs (docs/findings/06's
# "Task loop": `command_word`/`command_word_shift_src`/`ring_flag`), and
# `call_frame()` lets the concrete run compute everything else, the same
# way `run_init()` already does for FUN_1c15e3.
#
# Reaching render_frame this way (from a run_init() Runner, one voice
# already set up via setup_voice()) lands on the *same three* stops this
# lane's task brief already knew from a bare `--root 0x1c2b24` run
# (0xb88e4b, 0x1c4969, 0x1c088e) -- strong cross-check that they are real
# properties of an (almost) empty 16-track frame, not artifacts of calling
# render_frame bare. See FRAME_STOPS below and this lane's handover for
# the full stops table (real source found, or reported as a tools/
# sharc_core gap this lane must not fix itself).


def setup_frame(state, image: str, *, command: int = 3, ring_flag: int = 0) -> int:
    """Poke the audio task's command dispatch (docs/findings/06's "Task
    loop") so block_handler's own command-table jump selects `command`
    (3 = "renders from 0x1c7671", the only one that reaches render_frame)
    with ring half `ring_flag`, and return `profile(image).block_handler`
    -- the address `call_frame()` should `fresh_call()` at.

    `command_word_shift_src` (`DM(0x261ca4)`, the shift block_handler folds
    into the command word's address -- "0x1c7578..0x1c7586" in
    docs/findings/06) is left at 0 so the command word's address is
    `command_word` itself, with no per-track offset.

    This does not touch a voice record; call `setup_voice()` (or
    `render_frame()`'s own default) first on the same STATE so render_frame
    finds at least one active voice.
    """
    p = profile(image)
    _poke(state, p.command_word_shift_src, 0)
    _poke(state, p.command_word, command)
    _poke(state, p.ring_flag, ring_flag)
    return p.block_handler


def call_frame(
    runner: sr.Runner,
    image: str,
    *,
    patch_table: sv.PatchTable | None = None,
    max_steps: int = 4_000_000,
) -> tuple[sr.Runner, sr.RunResult]:
    """`Runner.fresh_call()` at `profile(image).block_handler` (see
    `setup_frame()`) on RUNNER's current State (typically a `run_init()`
    Runner with `setup_voice()`/`setup_frame()` already applied to its
    State) -- the real firmware call chain: block_handler ->
    command_dispatch_fn -> cmd_handler_3 -> render_frame -> ... -- instead
    of calling render_frame bare with hand-set registers (see this
    section's own module-level note for why that matters here).

    `patch_table`, if given, is `tools/sharc_dev/sharc_survey.py`'s PatchTable
    convention (`{pc: [(kind, target, value), ...]}`), applied every time
    its pc recurs via `sharc_survey.run_with_patches()` -- this lane's own
    hypotheses for the stops an (almost) empty synthetic frame hits belong
    there (see FRAME_PATCH_TABLE), not hardcoded in this function. Without
    one, this is a plain `Runner.run()`: it stops at the first fork/mmr/
    return exactly like any other survey root.

    Returns (the fresh_call Runner, its RunResult) -- the Runner stays
    positioned at the halt (or at the return) for a caller to inspect
    registers/memory afterward (e.g. dumping the master mix buffer).
    """
    p = profile(image)
    new_runner = runner.fresh_call(p.block_handler, diagnose_unknown=True)
    if not patch_table:
        return new_runner, new_runner.run(max_steps=max_steps)
    start_pc_sw = new_runner.state.pc_sw
    t0 = time.perf_counter()
    halt = sv.run_with_patches(new_runner, patch_table, max_steps)
    elapsed = time.perf_counter() - t0
    result = sr.RunResult(
        halt=halt,
        instructions=new_runner.instructions,
        elapsed=elapsed,
        form_counts=new_runner.form_counts,
        start_pc_sw=start_pc_sw,
        final_pc_sw=new_runner.state.pc_sw,
        max_call_depth_reached=new_runner.max_call_depth_reached,
        watch_log=new_runner.watch_log,
    )
    return new_runner, result


def call_frame_collect_all(
    runner: sr.Runner,
    image: str,
    *,
    patch_table: sv.PatchTable | None = None,
    max_steps: int = 4_000_000,
    img: sharcmod.Image | None = None,
    watchpoints: Sequence[sr.Watchpoint] = (),
) -> tuple[sr.Runner, sv.CollectAllResult]:
    """Like `call_frame()`, but drives the call with
    `tools/sharc_dev/sharc_survey.py`'s `run_collect_all()` instead of
    `run_with_patches()`: every stop this frame call passes is recorded
    (category, pc, form, unknowns), not only the first -- the same "collect
    every blocker on one path" behaviour `sharc_survey`'s own CLI gives a
    bare root, applied to a real frame call through `block_handler` instead.
    `patch_table` still resolves a fork the same way (a `"reg"`/`"mem"`
    entry unconditionally, before the instruction executes); a fork with no
    matching entry is continued through with the documented default
    (not-taken) rather than stopping the whole replay, so one frame's own
    report is a full list of what it depended on instead of only the
    nearest one.

    `watchpoints`, if given, are attached to the fresh_call Runner before it
    runs (`Runner.attach_watchpoints()` -- not carried over from `runner`
    itself, since `fresh_call()` never carries watchpoints either): a
    caller wanting to know which pc, if any, wrote a given DM range during
    this one frame call reads them back off the returned Runner's own
    `watch_log` (`sr.Runner.watch_log`), which `fresh_call()` always starts
    empty.

    Returns (the fresh_call Runner, positioned at the terminal stop, its
    `sv.CollectAllResult`) -- `result.terminal` is the same kind of Halt
    information `call_frame()`'s own `RunResult.halt` would have stopped
    at first.
    """
    p = profile(image)
    new_runner = runner.fresh_call(p.block_handler, diagnose_unknown=True)
    if watchpoints:
        new_runner.attach_watchpoints(list(watchpoints))
    result = sv.run_collect_all(new_runner, patch_table or {}, max_steps, img=img)
    return new_runner, result


# --- The companding record (lane F2, 2026-09-26) --------------------------
#
# docs/findings/06's "Lane E2" traces the never-taken machine-type-compare
# branch (the one that would call FUN_1c60a2) back to `I5 = DM(0x254d78)`
# reading a null pointer, itself written (as one of four words,
# 0x254d78/80/88/90) by `render_frame` (FUN_1c2b24, sw 0x1c2c5e-0x1c2c67)
# from `M9 = DM(I6-18)` -- render_frame's own saved copy of `R1`, the SECOND
# by-reference output `command_dispatch_fn` (0x1c778a) writes into
# block_handler's stack frame before its CALL into render_frame (the FIRST
# being `command_word` itself, `DM(I6-3)`, per docs/findings/06's "Frame
# call path"). This lane's own task: find where `command_dispatch_fn` gets
# THIS pointer from, and whether it can be set up "the firmware's way".
#
# **Traced to a fixed, shift-selected table, not the SPI-slave control
# block at 0x261a10 (lane D2) and not a ring.** Reading
# `command_dispatch_fn`'s own raw bytes (sw 0x1c778a-0x1c77b0) end to end:
# it writes `DM(I6-3) = command_word + (DM(command_word_shift_src) << 12)`
# (the SAME formula docs/findings/06 already gives for the address
# block_handler reads ITS OWN pending command from -- `DM(I6-3)` literally
# IS the address of the command word, not a separate "ring C") and, right
# after, `DM(I6-2) = command_record_table + (DM(command_word_shift_src) <<
# 12)` -- a DIFFERENT, previously-unnamed base (`0x266220` on DT2 1.16, now
# `sharc_symbols.SYMBOLS`'s own `command_record_table`, resolved the same
# way `command_word` is: `LiteralAt('command_dispatch_fn', 0x1d,
# form='17a')`). With this harness's own `setup_frame()` (which always
# leaves `command_word_shift_src` at 0, per that function's own docstring),
# `command_record_table`'s shift is 0, so the record address is simply
# `command_record_table` itself -- confirmed by execution (a breakpoint at
# `0x1c2c51`, `I1 = M9`, reads `I1 == 0x266220` exactly).
#
# **The table is empty, and this lane found no writer of it anywhere
# reached from `run_init()` or a real capture-driven frame call.**
# `img.writers()` over `[0x266220, 0x266260)` returns only `base_only`
# (unresolved-offset) hits from `FUN_1c80f2`'s own unrelated master-mix
# ramp/crossfade loop (a `DO`-loop with a runtime-only trip count the tool
# can't bound, so it conservatively reports every address in the whole
# image as a possible target of that ONE loop -- confirmed by reading
# `FUN_1c80f2` itself, entry 0x1c80f2, called from `render_frame`'s own
# post-master-mix cleanup per docs/findings/06's "Master stage": not a real
# writer of this table). This corrects/pins down E2's own "no writer of a
# non-null value ... found" conclusion with the record's exact, now-named
# address, rather than the unresolved `M9`/`DM(I6-18)` chain E2 could not
# trace further. `command_word_shift_src` itself (the table's own selector)
# has exactly one writer, `FUN_1c77b4` (sw 0x1c77b4-0x1c77c8, `has_static_
# caller: 0` -- confirms docs/findings/06's own "no static caller" note),
# gated by a runtime condition (`JUMP IF NOT SZ` on `DM(I6,M7)`) this
# harness's own `setup_frame()` never drives, so even the SHIFT is not
# something a synthetic frame call exercises realistically.
#
# **Net for this lane's own task:** the record is very likely populated the
# same way the RX frame itself is (docs/findings/06: "The ColdFire frame is
# mapped into SHARC DM at 0x2558dc") -- written into DM directly by a DMA
# transfer this project's SHARC-side tracing cannot see (no SHARC store
# instruction needs to exist for a host-mapped region), OR by a SHARC
# "session start"/note-on preparation routine this project has never
# executed (not part of `FUN_1c15e3`, and not reached by any capture this
# lane or lane E2 replayed). Neither could be confirmed or ruled out within
# this lane's own scope; report, per this lane's own task brief, rather
# than fabricate a value for it (the same call E2 already made for
# `FUN_1c4eaf`'s own trigger site). `companding_record_address()` and
# `companding_record_fields()` below are the reusable, exact form of this
# finding for a future lane to build on (e.g. once a real writer is found,
# poking the SAME four fields these read would let `FUN_1c2b24`'s own
# companding loop reach `FUN_1c60a2` for the first time).

COMPANDING_RECORD_FIELD_OFFSETS: tuple[int, ...] = (
    8,
    16,
    24,
    32,
)  # bytes: +2,+4,+6,+8 words
COMPANDING_RECORD_TARGETS: tuple[int, ...] = (0x254D78, 0x254D80, 0x254D88, 0x254D90)


def companding_record_address(state, image: str) -> int:
    """The address `render_frame` (`FUN_1c2b24`) itself computes for the
    "companding record" it reads via `M9` (`DM(I6-2)`, one of
    `command_dispatch_fn`'s two by-reference outputs to block_handler) --
    see this module's own section note above for the full trace.
    `profile(image).command_record_table + (DM(command_word_shift_src) <<
    12)`, matching `command_dispatch_fn`'s own raw bytes exactly."""
    p = profile(image)
    shift = _dm_word(state, p.command_word_shift_src)
    return (p.command_record_table + (shift << 12)) & 0xFFFFFFFF


def companding_record_fields(state, image: str) -> list[int]:
    """The four words `render_frame` copies from
    `companding_record_address()` (word offsets +2, +4, +6, +8 --
    `COMPANDING_RECORD_FIELD_OFFSETS`) into `COMPANDING_RECORD_TARGETS`
    (`DM(0x254d78)`/`0x254d80`/`0x254d88`/`0x254d90` -- the first of which
    is the pointer docs/findings/06's Lane E2 traces the never-taken
    machine-type-compare branch back to). All-zero on every state this
    lane found reachable from `run_init()` -- see this module's own section
    note above."""
    base = companding_record_address(state, image)
    return [_dm_word(state, base + off) for off in COMPANDING_RECORD_FIELD_OFFSETS]


# This lane's own hypotheses for the stops render_frame hits when called
# (via setup_frame()/call_frame() above) from a run_init() state with one
# voice set up and no other per-track frame data at all (an almost-empty
# 16-track frame) -- each keyed by the pc sharc_survey's diagnose_unknown
# names as the fork trigger, evidence in this module's git history/the
# lane's handover, not re-derived here. Two of these patch a REGISTER
# immediately after the read that should have produced it, rather than the
# DM cell a static read of the instruction suggests, because a direct
# `_dm_write()` at that raw address does not reach whatever the actual
# "(sw)" instruction resolves (confirmed empirically: identical instruction
# count patched vs unpatched) -- a `tools/sharc_dev/sharc_core` address-canonicalisation
# question this lane could not resolve within its own scope; report it,
# do not guess further at it here.
#
# **[C] Lane F2 (2026-09-26): NEITHER entry is actually about missing frame
# data -- both are `tools/sharc_dev/sharc_core` gaps, confirmed present even with a
# REAL captured RX frame (out/captures/dt2-1.16-play-pretracks-fulltx.dt2cap,
# frame 20, track 2, master-bus table UNMASKED so the real per-track gain
# table is nonzero).** This corrects the two entries' own comments below,
# which described the ORIGINAL (all-empty synthetic frame) hypothesis before
# this lane checked it against real data; both forks recur at the identical
# pc with identical evidence once real data is present, so the original
# "this lane's synthetic frame never populated it" framing undersold the
# actual cause. A breakpoint at each fork's own upstream write (`0xb88e47`,
# `0x1c4965`) and a direct read of the live symbolic registers there
# (`Runner.state.uregs`, not `img.print_slice()`'s own separate STATIC
# slice, which reports a different, looser "[unresolved: ...]" for values
# a backward slice can't follow past a call boundary) shows precisely:
#
# - `0xb88e4b` (the `0x1C2FEC` entry): `R8` (the divisor a shared
#   recips-based fixed-point divide helper, `FUN_b88e04`, receives) is a
#   genuine, concrete `Const(0)` -- one of the OTHER 15 tracks this real
#   capture's kit does not assign a machine to legitimately has zero gain,
#   same as the empty synthetic frame did. `recips(0.0)` is well-defined
#   (PRM p.19-16: +-zero input -> +-infinity, PRM p.417-418's own
#   IEEE-754-compatibility rules), but the Newton-Raphson refinement that
#   follows propagates that infinity into a NaN (`infinity * 0.0` inside the
#   helper's own iteration), and `tools/sharc_dev/sharc_core`'s `fix()` handler
#   (`compute_alu.py`) has no modeled saturation rule for a NaN/infinity
#   input (`Unknown(reason='fix F0 (unsaturated NAN/infinity fix)')`) --
#   real SHARC+ hardware has a documented saturated result for this case
#   (PRM's own IEEE-754-compatibility section), `tools/sharc_dev/sharc_core` just
#   does not implement it yet. Real per-track gain data does not remove
#   this: every genuinely-silent track (15 of 16, in this real capture)
#   hits the identical zero-divisor case a real device would also see, so
#   the gap is inherent to the FIX-of-non-finite path, not to a data gap
#   this lane could fill by tracing further.
# - `0x1c4969` (the `0x1C4965` entry): `F4`/`F6` are Unknown, and the write
#   at `0x1c4965` traces (through `F15=fsub(F15,F7)`, `F2=F10*F15`,
#   `F4=fmax(F2,F13)`) to `Unknown(reason='bitext: undefined (bitlen 95 >
#   32)')` -- a `tools/sharc_dev/sharc_core` BITEXT decode that refuses a field wider
#   than 32 bits, hit while `FUN_1c4afe` (one of the four Step/Positions
#   setters, called from `FUN_1c642a`'s own per-voice "Trigger" dispatch
#   `0x1c6553` EVERY frame, not only at a real note trigger -- confirmed by
#   the fork's own call stack: `FUN_1c75d8 -> FUN_1c2b24 -> FUN_1c642a ->
#   FUN_1c4afe -> FUN_1c4914`) derives a note/tune-interpolated value from
#   an undocumented voice-record field pair (`DM(I4+0x62)`/`DM(I4+0x63)`,
#   NOT in "The voice record contract" table -- an `[O]` gap in its own
#   right) through a ~150-instruction pitch-table routine this lane read
#   but did not fully reverse (out of this lane's own scope: doing so would
#   mean hand-replicating that routine's arithmetic as a harness patch,
#   which is the same kind of guess this repo's `sharc_core`-fix rule
#   (a PRM page, a test, or a widthaudit 0-mismatch proof) exists to rule
#   out). Confirmed present with the SAME real capture/frame/track as above.
#
# **[C] Lane G2 (2026-09-26): the `0x1C2FEC` entry is REMOVED -- its
# `tools/sharc_dev/sharc_core` gap is fixed.** `tools/sharc_dev/sharc_core/floats.py`'s
# `_float_to_fixed`/`_double_to_fixed` (the shared FIX/TRUNC[ BY RY]
# helper) now implements the SC58x/2158x PRM's own FIX/TRUNC pages verbatim
# (out/refs/sc58x-2158x-prm/all.txt pp.20-11..20-14 for the 32-bit forms,
# pp.20-28..20-32 for the 64-bit `Fx:y` forms -- identical wording on all
# six pages): a NAN input is unconditional on MODE1.ALUSAT ("A NAN input
# returns a floating-point all 1s result" is its own sentence, not part of
# the following "If saturation mode is not set" clause) and returns
# `0xFFFFFFFF` with `invalid=True` either way; an infinity input or a
# result that numerically overflows saturates to `0x7FFFFFFF`/`0x80000000`
# when ALUSAT=1 (`invalid=False`, since the AI bullet's infinity/overflow
# clause is itself gated "when saturation mode is not set") or returns the
# same `0xFFFFFFFF` all-1s pattern with `invalid=True` when ALUSAT=0 (see
# `_float_to_fixed`'s own docstring for the full flag derivation). Checked
# live: removing this entry and re-running the SAME real-frame call this
# table's own module note above describes (capture
# `dt2-1.16-play-pretracks-fulltx.dt2cap`, frame 20, track 2, master-bus
# table masked) reaches the identical final halt ("return without followed
# call" at `0x1c75d3`) as with the entry present -- the fork at `0xb88e4b`
# no longer occurs because `fix()` on the genuine `Const(0)`-divisor NaN
# now resolves to a concrete `0xFFFFFFFF` instead of `Unknown`. The
# instruction count differs slightly (82,325 vs. 82,263 -- the real
# `0xFFFFFFFF`/AI=1 result take a different, but still successful, path
# than this entry's own arbitrary `0x4000` override did), so
# `RealFrameRingAMilestoneTest`'s own ring-A digest changes too (see
# `tests/sharc/integration/test_sharc_harness_frame.py`'s own note on that hash for why -- an
# intended fix, not a regression).
#
# **[C] Lane G2: `0x1C4965` stays, and corrects docs/findings/06's own
# "BITEXT, still open" `[V]` claim that "the BITEXT result R0 from the
# call at 0x1c4949 is overwritten before use."** That claim is INCOMPLETE,
# not a false trace: a live, single-instruction-stepped register trace
# through this exact fork (capture/frame/track as above, `0x1C4965`
# removed) shows `R0` genuinely does get a fresh value from the
# `Rn=comp(Rx,Ry); Rm=Rn`-shaped instruction at `0x1c4950` (form
# `5a_move`) that follows the `CALL 0xb88f70` at `0x1c4949` -- but
# `forms_move.py`'s own `_type_5a_move` comment ("The Type 5a data move and
# compute both consume the pre-instruction file") means the PARALLEL
# `R15 = R0` ureg-copy in that SAME instruction reads `R0`'s
# PRE-instruction value, not the compute's newly written one. Traced live:
# `0xb88fa4` (inside `FUN_b88f70`, the call target -- a leading-zero-count/
# normalize helper, not the fixed-point divide `0xb88e04` unrelated to it)
# is a genuine `BITEXT (NU)` (shiftimm opcode `0x19`) with `BITLEN12=95`
# (one of docs/findings/06's own five sightings), written to `R0` as
# `FUN_b88f70`'s own return value; `0x1c4950`'s `R15 = R0` copies that
# SAME still-`Unknown('bitext: undefined (bitlen 95 > 32)')` value into
# `R15` BEFORE `0x1c4950`'s own compute half gets to overwrite `R0` --
# confirmed by a register snapshot at every intervening pc: `R15` reads
# `Unknown('bitext: ...')` starting at `0x1c4953` (the very next step),
# then flows `F15=fsub(F15,F7)` (`0x1c4961`), `F2=F10*F15` (`0x1c495a`,
# `R10` itself a genuine, unrelated `Const`), `F4=fmax(F2,F13)`
# (`0x1c4963`), into the fork's own `F6=fcomp(F6,F4)` at `0x1c4965` --
# matching lane F2's own trace element for element. So the earlier "[V]"
# framing was right that `R0` itself gets clobbered, but wrong to treat
# that as discarding the BITEXT taint: this SAME instruction is what
# smuggles it into `R15` first. **This is still not a `tools/sharc_dev/sharc_core`
# fix this lane may make**: PGR p.11-91 documents `BITLEN12 > 32` as
# "prohibited" for BITEXT's own FEXT-based pseudocode step and says only
# that SV is set, without documenting what numeric result (if any) real
# silicon produces for that width -- unlike the FIX/TRUNC NaN/infinity
# case above, there is no PRM/PGR page to cite for a specific value here,
# so implementing one would be exactly the undocumented guess this
# project's own rule (a PRM page, a test, or a widthaudit 0-mismatch
# proof) exists to rule out. The frame path's pitch bug (`FIELD_PHASE`
# advancing a fixed 40.43 samples/frame regardless of `pitch_step`,
# docs/findings/06 lane E1) is therefore UNCHANGED: `0x1C4965`'s forced
# `1.0f`/`1.0f` operands still discard the real (buggy) Positions
# computation every frame, so `render_frames_to_ring_a()`'s frame-path
# output still does not match `check_correctness()`'s `freq * pitch_step`.
FRAME_PATCH_TABLE: sv.PatchTable = {
    # 0x1c4965 -> fork at 0x1c4969: FUN_1c4914 (the voice record contract's
    # "Positions" setter helper) computes ASTATX.AN/AZ/AV from
    # `F6=fcomp(F6,F4)` over an fmax/fsub chain of BITEXT-decoded voice
    # record position fields. **[C] Lane F2, corrected further by Lane
    # G2 (see this table's own module-level note above for the full,
    # execution-confirmed trace):** the real gap is a genuine, architecturally
    # undocumented `tools/sharc_dev/sharc_core` BITEXT(NU) result for `BITLEN12=95 >
    # 32`, hit on every frame's call into `FUN_1c4afe` (real note-trigger
    # call path or not) -- not specific to this lane's own synthetic voice.
    # Forces both fcomp operands to a finite 1.0f -- still a discard of the
    # real, buggy Positions computation (see the frame-path pitch bug note
    # above), not a fix.
    #
    # **[C] Lane H2 (2026-09-26): BITLEN12>32 is confirmed correctly decoded
    # (not a mis-extracted field) and cannot be resolved by reinterpreting
    # its length -- and doing so would not fix the frame-path pitch bug
    # anyway.** Task: use the frame-path pitch as the oracle for
    # `BITLEN12>32`'s undefined result. Checked against BOTH manuals this
    # project cites for the shifter (PGR `adsp-2136x_2137x_214xx` rev2.4
    # p.11-91 and the SHARC+ Core PRM `sc58x-2158x` p.24-18 -- byte-for-byte
    # identical wording): "the DATAEX field adds 4 MSBs to the DATA field,
    # creating a 12-bit immediate value" (PGR all.txt:19044-19046) is
    # exactly `tools/sharc_dev/sharc_core/compute_shift.py`'s own
    # `(dataex[3:0] << 8) | data8` -- the SAME combined 12-bit field the
    # already-implemented, presumably-correct FEXT opcode (0x10) splits
    # into `position = X & 0x3F` / `length = (X >> 6) & 0x3F` bit-for-bit.
    # So `BITLEN12` genuinely is a 12-bit immediate as encoded, and this
    # image's five sightings (95, 384, 192, 256, 535) are not a decode bug;
    # neither manual defines a numeric BITEXT result for a value over 32,
    # only that SV is set ("prohibited").
    #
    # More importantly, length is not even the blocking variable here: `grep
    # -rn 'special\['` across `tools/sharc_dev/sharc_core` shows `"BFF_HI"`/`"BFF_LO"`
    # are written ONLY by BITEXT/BITDEP's own handlers in
    # `compute_shift.py`, and no BITDEP instruction (the only other writer
    # the PRM/PGR document) ever decodes anywhere in dt2-1.16 -- so
    # `_bff_words()` returns Unknown at every BITEXT site in this image, and
    # `_bff_extract()`'s `isinstance(hi, Const)` guard discards ANY length
    # argument once the FIFO itself is Unknown. Checked live: monkeypatching
    # in each of the four length candidates the task named (mod 64, low 6
    # bits, clamp to 32, truncate a full 64-bit extract) without also
    # assuming a known FIFO reproduces today's exact halt/instruction count
    # for all four -- they cannot be distinguished from "stays Unknown" or
    # from each other this way. The ONLY assumption under which BITEXT
    # produces a concrete value at all here (the FIFO reset to Const(0), the
    # sole state consistent with "never deposited into") makes the four
    # candidates identical to EACH OTHER too (a 64-bit all-zero register
    # reads 0 at any length) -- so this task's own candidate list cannot be
    # discriminated between by any oracle test, only by an unrelated claim
    # about the FIFO's own reset value, which no manual documents either.
    # Implemented that one combined candidate as a throwaway (uncommitted)
    # opt-in anyway, removed this entry, and ran the harness's real-frame
    # render (`render_frames_to_ring_a`, freq=1000 Hz, `pitch_step` in
    # {1.0, 0.5}): the render completes with no new stop both times (so a
    # concrete BITEXT result does let `FUN_1c4afe`'s own fcomp chain run
    # unforced) -- but a coarse frequency scan of ring A finds the SAME
    # dominant tone, ~630 Hz (power_ratio 0.99), at BOTH pitch steps,
    # essentially unchanged from lanes E1/F2/G2's own 631.7191123962402 Hz
    # figure measured with this entry in place; direct correlation at the
    # REQUESTED 1000/500 Hz finds negligible power (ratio 0.006/0.039). So
    # resolving `BITEXT` here (however it is resolved) is necessary for the
    # frame to complete on its own but is NOT the frame-path pitch bug's
    # cause: retarget that bug's own next step at `FUN_1c4afe`'s
    # undocumented voice-record input pair `DM(I4+0x62)`/`DM(I4+0x63)` (an
    # `[O]` gap noted under lane F2's own Task 1) -- this harness's
    # `setup_voice()` has no documented writer for it, so it never reflects
    # the caller's own `pitch_step` argument regardless of what `BITEXT`
    # does. `0x1C4965` stays; no `tools/sharc_dev/sharc_core` change was made (no
    # citable manual value exists, and the one testable candidate would not
    # have fixed the actual bug).
    #
    # **[C] 2026-09-28: the `0x1C4965` entry is REMOVED; there was no
    # BITEXT.** ShiftImm opcode 0x19 is `Rn = Rn or fdep Rx by bit6:len6`,
    # not BITEXT (NU): PGR Table 12-11 (pp.12-10/12-11) gives or-fdep as
    # 0110 0100 and a ShiftImm opcode as its upper six bits; the PRM's
    # Table 17-9 6-bit column is off by one row there
    # (tools/sharc_dev/sharc_core/compute_shift.py). The five "BITLEN12" sightings
    # are bit6:len6 fields, e.g. 0xb88fa4 "R0 = R0 or fdep R12 by 31:1"
    # sets the sign bit of FUN_b88f70's integer-to-float result. With that
    # decoded, the fcomp at 0x1c4965 gets concrete operands and the frame
    # returns with no patch (tests/sharc/integration/test_sharc_harness_frame.py's
    # test_render_frames_without_patches_*).
    # 0x1C4965: [("reg", "R6", 0x3F800000), ("reg", "R4", 0x3F800000)],
}

# A further, DIAGNOSTIC-ONLY hypothesis for the third known stop
# (0x1c088e, FUN_1c0874's saturating fix<->float round-trip helper): not
# in FRAME_PATCH_TABLE because forcing it this way desyncs the run rather
# than fixing it (see this module's own handover). Confirmed root cause
# (not a hypothesis): forms_system.py's `_type_18a` bit-test handler
# (`bop in (4, 5)`) requires `isinstance(source, Const)` to produce a
# concrete BTF -- it never uses `_astatx_known_bit()`-style per-bit
# reasoning against a `PartialConst` source, so once ASTATX carries even
# one unrelated unknown bit (near-universal after any float op; confirmed
# by this lane: the tested bit itself, AI, was concretely known here) a
# `bit-test`/`xor-test` against ASTATX/ASTATY always reports Unknown. This
# is a `tools/sharc_dev/sharc_core` gap this lane must not fix itself -- report it
# instead: `_type_18a` (tools/sharc_dev/sharc_core/forms_system.py, the `bop in (4,
# 5)` branch) should determine `result` from the PartialConst's own
# mask/bits over exactly `mask`'s bits when `source` is a `PartialConst`,
# falling back to Unknown only when a bit `mask` needs is itself
# unresolved -- the same reasoning `_astatx_known_bit()` already applies
# one bit at a time. Setting this lane's diagnostic patch
# (`{"ASTATX": 0x400}` at 0x1c0885, replacing ASTATX with a plain Const
# that happens to match its own known bits) does get past 0x1c088e, but
# the run then hits a "return-mismatch" category a few hundred thousand
# instructions later (0xb82a08, a repeated call stack entry suggesting an
# IIR/limiter loop) -- discarding ASTATX's other tracked bits this way
# desyncs a later branch, not a step toward the frame returning. Left out
# of FRAME_PATCH_TABLE for that reason.
FRAME_DIAGNOSTIC_ASTATX_PATCH: sv.PatchTable = {0x1C0885: [("reg", "ASTATX", 0x400)]}

# Where one render_frames() call with FRAME_PATCH_TABLE stops today, from
# run_init() state. Tests compare against this one pin: update it (and say
# why in the commit) when a fix moves the stop.
#
# 2026-09-25: forms_move.py's Type3a handler previously refused l=1 (long-
# word) outright ("unsupported Type3a long-word access" at 0x1c2920, 86,740
# instructions). Implementing it (the Type14a-style neighbor-register-pair
# access PRM p.2-4 documents for the (LW) modifier) let the frame run on
# instead of stopping there. The run now reaches a genuine RETURN, not
# another gap: "return without followed call" at 0x1c75d3 -- inside
# FUN_1c74cd (block_handler itself, entry 0x1c74cd, end 0x1c75d8, per
# tools/sharc_dev/sharc.py's img.func) -- is how tools/sharc_dev/sharc_run.py's
# fresh_call_state()/Runner.fresh_call() (return_address=None, see its own
# docstring) signals that a call started with an empty call_stack has
# unwound all the way back through its own entry point's RTS. Since
# call_frame() enters at block_handler with no real caller pushed, this
# halt fires exactly when block_handler's own return executes -- the frame
# render call chain (block_handler -> command_dispatch_fn -> cmd_handler_3
# -> render_frame -> ...) completed. tools/sharc_dev/sharc_widthaudit.py --root frame
# confirms 0 access-width mismatches across all load/store events checked
# in this run.
#
# 2026-09-26 (lane G2): the instruction count moved 95,982 -> 96,044 (+62)
# when the `0x1C2FEC` FRAME_PATCH_TABLE entry was removed (its
# `tools/sharc_dev/sharc_core` gap -- FIX-of-NaN/infinity saturation -- is fixed; see
# that entry's own former docstring and docs/findings/06's "Lane G2"
# section). The real, documented `fix()` result (`0xFFFFFFFF`, `AI` set)
# takes a slightly different, still-successful path than the removed
# entry's own arbitrary `0x4000` register override did; the final halt
# (pc, reason) is unchanged. tools/sharc_dev/sharc_widthaudit.py --root frame
# reconfirms 0 mismatches at the new count (17,475 load/store events).
#
# 2026-09-28: 96,044 -> 213,504 instructions, same halt, and no patch
# needed. Five tools/sharc_dev/sharc_core fixes change the frame's path: Type4b's
# (l, x, w) = (1, 1, 1) is a normal word, not (lw) (PRM p.13-32); (lw)
# modifiers scale like normal words (PRM p.6-9, Table 6-2); the plain
# MODIFY (Type7b, Type7a without (sw)/(nw)) does not scale (PRM p.6-10);
# ShiftImm 0x19 is Rn = Rn or fdep, not BITEXT (NU) (PGR Table 12-11), so
# FRAME_PATCH_TABLE's last entry is gone; and byte/short SIMD companions
# are modelled (PRM p.7-5). tools/sharc_dev/sharc_widthaudit.py --root frame:
# 0 mismatches over 46,128 load/store events.
#
# 2026-09-28 (later): 213,504 -> 213,247, same halt. The SIMD half of the
# move forms: Type3a's PEy companion (PRM p.13-15, Table 6-10) and the PEy
# compute of Type3a/4a/1a/5a (PRM p.101). With both switched off the count
# is 213,504 again (scratchpad report sharc-trig-arm.md).
FRAME_MILESTONE = {
    "pc_sw": 0x1C75D3,
    "instructions": 213247,
    "reason": "return without followed call",
}


def render_frames(
    memory,
    image: str,
    *,
    n_frames: int = 1,
    voice: int = 0,
    pitch_step: float = 1.0,
    sample_len: int = 4096,
    samples: Sequence[float] | None = None,
    sample_format: str = "int16",
    command: int = 3,
    ring_flag: int = 0,
    patch_table: sv.PatchTable | None = FRAME_PATCH_TABLE,
    max_steps: int = 4_000_000,
) -> tuple[sr.Runner, list[sr.RunResult]]:
    """`run_init()`, then N `call_frame()` calls against one voice set up
    via `setup_voice()` (VOICE active, the rest silent) -- the frame-level
    analogue of `render_blocks()`. `patch_table` defaults to
    FRAME_PATCH_TABLE (this lane's own hypotheses); pass `None` for a bare
    diagnostic run that stops at the first fork.

    `run_init()` failing (`InitResult.ran` False) raises ValueError: unlike
    render_blocks()'s optional `init`, a frame call has no bare-state
    fallback (setup_frame()'s docstring: render_frame needs post-init
    state; nothing about it was ever designed to run from a blank State).

    Returns (the Runner positioned after the last call_frame(), one
    RunResult per frame) -- inspect `runner.state` afterward (e.g.
    `st._dm_read(runner.state, profile(image).master_mix ...)`, or
    whatever ring the master stage feeds -- docs/findings/06's "Master
    stage": `render_frame` writes the mix at `R4`, i.e. 0x25f180, the same
    address `setup_frame()` never touches) to see what the frame actually
    produced.
    """
    init = run_init(memory, image)
    if not init.ran or init.runner is None:
        raise ValueError("render_frames: run_init failed: %s" % init.error)
    runner = init.runner
    state = runner.state
    if samples is not None:
        _write_samples(state, 0x310000, samples, sample_format)
    setup_voice(
        state,
        image,
        voice,
        sample_len=sample_len,
        pitch_step=pitch_step,
        sample_base=0x310000,
    )
    setup_frame(state, image, command=command, ring_flag=ring_flag)
    results = []
    for _ in range(n_frames):
        runner, result = call_frame(
            runner, image, patch_table=patch_table, max_steps=max_steps
        )
        results.append(result)
    return runner, results
