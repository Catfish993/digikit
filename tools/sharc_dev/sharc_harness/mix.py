"""Master and track mix reading, injection and ring A output."""

from __future__ import annotations

import math
import struct
import time
from collections.abc import Callable, Mapping, Sequence

from emu import sharc_capture
from tools.sharc_dev import sharc_dac
from tools.sharc_dev import sharc_run as sr
from tools.sharc_dev import sharc_survey as sv
from tools.sharc_dev import sharc_trace as st

from .audio import decimate, write_wav
from .capture import (
    DEFAULT_CAPTURE_FRAME_INDEX,
    capture_frame_bytes_for_render,
    find_settled_capture_frame,
    write_capture_frame,
)
from .check import _default_sample_len
from .frame import FRAME_PATCH_TABLE, setup_frame
from .layout import FIELD_WORK_BUFFER, SOURCE_SAMPLE_RATE, profile
from .runner import _float_to_word, _poke, run_init
from .voice import _write_samples, setup_voice


def read_master_mix(memory, image: str, runner: sr.Runner) -> list[float]:
    """The 64 floats at `profile(image).master_mix`'s own input buffer,
    0x25f180 (docs/findings/06's "Master stage": `0x1c207b` gets `R4 =
    0x25f180`; `setup_frame()`/`call_frame()` never write there themselves,
    so any nonzero content came from render_frame's own run) -- for
    checking whether an active voice's signal reached the mix after a
    frame call. 64 words matches the per-block work-buffer size
    (FIELD_WORK_BUFFER) render_frame's own per-track sum accumulates into;
    unlike that field, 0x25f180 is a fixed address, not a voice-record
    offset.
    """
    floats = []
    for i in range(64):
        raw = st._dm_read(runner.state, 0x25F180 + i * 4, 4)
        floats.append(
            struct.unpack("<f", struct.pack("<I", raw.value & 0xFFFFFFFF))[0]
            if raw is not None
            else 0.0
        )
    return floats


def read_ring_a(memory, image: str, runner: sr.Runner, count: int = 64) -> dict:
    """The DAC output ring: `tools/sharc_dev/sharc_dac.py` owns the format itself
    (module docstring there has the full static + dynamic evidence for
    "FUN_1c74a1 converts DM(0x25f180)/DM(0x25f200) to Q31, L/R interleaved,
    into ring A half RING_A_BASE + flag*RING_A_HALF_BYTES") -- this
    function is now just the `Runner`-shaped adapter over it, for checking
    whether an active voice's signal reached the DMA-facing output buffer
    after a frame call, one step past `read_master_mix`'s pre-conversion
    float sum.

    `profile(image).ring_flag` resolves the literal DM address
    `setup_frame()` already pokes through (this module's own "Task loop"
    comment); `sharc_dac.ring_half_just_written()` applies the
    post-toggle-flag correction (see its own docstring) that used to be
    hand-rolled here.

    `count` is kept for backward compatibility with callers that pass it,
    but `sharc_dac` always reads a full 64-word (32 stereo sample) half --
    a partial COUNT only truncates the returned lists, it does not change
    how many words are actually read from memory.

    Returns {"flag_after": the current (post-toggle) ring_flag value,
    "half_written": the byte offset of the half the last frame wrote (None
    when `flag_after` itself is None, e.g. a Runner whose loader never
    mapped `ring_flag`), "q31": raw Q31 ints (interleaved L0,R0,L1,R1,...;
    an unmapped word reads as 0, matching `sharc_dac.decode_ring_a_words()`
    -- real SHARC+ SRAM after reset, not "unknown"), "float": the same
    words as -1.0..1.0 floats (Q31's own full-scale convention), plus
    "left"/"right" (32 de-interleaved floats each) and
    "left_raw"/"right_raw" (their Q31 ints) -- `sharc_dac.RingAFrame`'s own
    fields, for a caller that wants one channel without re-deriving the
    interleave.}
    """
    flag_raw = st._dm_read(runner.state, profile(image).ring_flag, 4)
    flag_after = flag_raw.value & 1 if flag_raw is not None else None

    def dm_read(address: int) -> int | None:
        raw = st._dm_read(runner.state, address, 4)
        return None if raw is None else raw.value & 0xFFFFFFFF

    frame = sharc_dac.read_ring_a_pcm(dm_read, flag_after)
    q31 = frame.interleaved_q31()[:count]
    floats = [v / sharc_dac.Q31_FULL_SCALE for v in q31]
    return {
        "flag_after": flag_after,
        "half_written": None if flag_after is None else frame.half_written,
        "q31": q31,
        "float": floats,
        "left": frame.left,
        "right": frame.right,
        "left_raw": frame.left_raw,
        "right_raw": frame.right_raw,
    }


def write_ring_a_wav(
    path: str,
    rings: Sequence[dict],
    *,
    sample_rate: int = 48000,
    stereo: bool = True,
) -> None:
    """Concatenate `read_ring_a()`'s own per-frame return dicts (RINGS, in
    frame order) into one WAV at PATH: stereo (both of ring A's own L/R
    channels, `sharc_dac.write_wav_stereo()`) by default, or a mono L+R
    average (`tools/sharc_dev/sharc_harness/`'s own `write_wav()`, the same
    downmix `render_frames_to_ring_a()` already computes for
    `measure_tone()`) when STEREO is False.

    This is ring A's own WAV writer -- `write_wav()` above stays the
    voice-render (mono-only) one; a caller with several frames' worth of
    `read_ring_a()` dicts (one call per rendered frame) uses this instead
    of hand-concatenating "left"/"right" itself."""
    left: list[float] = []
    right: list[float] = []
    for ring in rings:
        left.extend(ring["left"])
        right.extend(ring["right"])
    if stereo:
        sharc_dac.write_wav_stereo(path, left, right, sample_rate=sample_rate)
    else:
        mono = [
            0.5 * (l_sample + r_sample)
            for l_sample, r_sample in zip(left, right, strict=True)
        ]
        write_wav(path, mono, sample_rate=sample_rate)


# --- Voice -> track -> master mix -> ring A (lane A2, 2026-09-25) ---------
#
# render_frames()/call_frame() above reach a genuine RETURN (FRAME_MILESTONE)
# from a run_init() state with one voice set up, but the master mix
# (0x25f180) and both DAC rings stay zero: docs/findings/06's "the mix
# gate" -- FUN_1c642a's own per-track accumulate block (0x1c6b3c-0x1c6b8a)
# is skipped whenever the "shared context" word DM(0x252d3c) reads 0, which
# it does from any run_init() state (init writes the literal 32 at
# 0x1c1643, then FUN_1cb336 resets it to 0 later in the same init run,
# before FUN_1c15e3 returns -- confirmed by execution, see that function's
# own docstring).
#
# **This lane's own whole-image writer/reader scan (tools/sharc_dev/sharc.py's
# writers()/readers(), not just run_init()'s own reach) finds one more
# writer and two more readers of DM(0x252d3c), none reported before:**
#
#   - A writer at 0x1cdc23 (``DM(I3, M6) = R11``, I3 = the passed-in
#     0x252d3c pointer), inside FUN_1cdbb2 (0x1cdbb2-0x1cdcab) -- an
#     IIR/all-pole recursion this lane did not fully characterize. It is
#     reached only dynamically, through FUN_1c642a's own out-of-line
#     dispatch FUN_1c71ec and two nested jump tables (0x8055c840 ->
#     0x8055c858 -> 0x8055c874, whose index 1 selects FUN_1cdbb2 --
#     docs/findings/06's "Table 0x8055c874 picks one stage per slot type"),
#     for whatever per-track "slot type" selects that stage -- which is why
#     ``img.callers()`` reports none (a real indirect-call gap, not
#     evidence this path is dead: see this repo's CLAUDE.md). Every capture
#     this project has (idle and play) reads every track's own machine
#     type/selector fields as 0, so this path was never exercised by
#     execution here, and this lane could not reach it from a synthetic
#     frame either (see the report). Reported as [O], not adopted.
#   - Two readers, previously undocumented: 0xb82696 (FUN_b82680, an
#     "orchestrator/dispatcher" in the same block group as the dynamics
#     stage) and 0xb82d54 (FUN_b82d41, the thin wrapper `0x1c207b` calls
#     as `0xb82d41 -> 0xb82cba`, docs/findings/06's "dynamics [D]" stage --
#     confirmed by disassembly: ``R0 = DM(I4, M5)`` at 0xb82d54, I4 = the
#     caller's R12 = the same 0x252d3c pointer, pushed as one of
#     FUN_b82cba's own stack arguments). So this word is not read only by
#     FUN_1c642a's accumulate gate: the dynamics/compressor stage consumes
#     it too. This lane's own experiment (see the report) is consistent
#     with FUN_b82cba's envelope math going degenerate when this input is
#     0 -- a hand-set-nonzero gate reached a NEW, unresolved fork
#     (FUN_b8037f, blk69) instead of restoring multi-frame signal, so this
#     is reported as a plausible explanation, not a fix.
#
# **This lane's own execution trace also corrects one specific claim in
# docs/findings/06's own "[V]" description of the accumulate loop.** A
# concrete, register-level single-step of 0x1c6b3c-0x1c6b8a (patched so
# DM(0x252d3c) reads 1, avoiding the skip) shows R8 -- the operand
# docs/findings/06 calls "per voice/track index R8" -- staying the SAME
# constant (0) for all 32 hardware DO-loop iterations; nothing in the loop
# body ever increments it. The per-iteration axis is instead I4, which
# walks a 32-entry workspace *pointer table* at 0x24ef2c (matching
# docs/findings/06's own "I5 = DM(I6-4) + 0xdc64 = 0x24ef2c" workspace
# confirmation), and the writes this lane observed through it land in
# per-voice SDRAM addresses (e.g. 0x8045b3c0 for voice 0 -- in the same
# 0x8045.... range as the documented voice sample pointer), not in
# 0x252df8 + t*0x100 (the master-stage per-track buffer). This needs a
# second agent's check before correcting docs/findings/06 -- it may mean
# the loop is walking *voices*, writing each one's own decimator-state
# scratch (record +0x104's target), rather than the master-stage per-track
# sum this lane initially assumed from the doc's own framing; reported
# here, not adopted as a doc correction, since this lane's own DM(0x252d3c)
# value (1) may not be the real one the doc's own confirmatory run used.
#
# **Given the above, this lane could not establish a real runtime path
# that sets DM(0x252d3c) to a value that both (a) avoids a new fork and
# (b) makes FUN_1c642a's own accumulate loop deposit a voice's decimated
# output at 0x252df8 + t*0x100.** Per this lane's own task brief ("record
# any remaining hand-set value in FRAME_PATCH_TABLE / a documented setup
# function with its evidence"), the functions below instead inject a
# voice's own ALREADY-FIRMWARE-RENDERED decimated buffer (FIELD_WORK_BUFFER,
# populated every frame by the firmware's own FUN_1c4ecf/FUN_1c4f81 call
# inside FUN_1c642a's per-voice dispatch loop -- this part needs no hand-
# set value at all) directly into one track's master-mix input, bypassing
# only the not-yet-resolved accumulate stage -- confirming, by execution,
# that the DOWNSTREAM path (track buffer -> master stage 0x1c207b -> ring
# A) is intact and unconditional once that one buffer has real content.
#
# --- Continuity across frames, root-caused (lane B2, 2026-09-25) ----------
#
# The frame-0-only problem above is NOT the "one independent post-init
# render per frame" workaround's own two original blockers (`state.trace`'s
# unbounded growth and the Type14a odd-UREG-pair stop at 0x1c32ad): both are
# fixed on this branch (7e5e8c4), and a genuinely continuous Runner (one
# `run_init()`, repeated `Runner.fresh_call()` at block_handler, checked by
# watchpoint on this lane's own multi-frame runs) now completes any number
# of frames cleanly. Ring A still goes silent from frame 1 onward anyway --
# root cause, traced with watchpoints across two frames of one continuous
# Runner (see the report):
#
# `FUN_1c207b`'s own per-track summation loop (`0x1c2353`, `DO ... UNTIL
# LCE` over all 16 tracks' `0x252df8`-range input) multiplies every track's
# contribution by three scalar coefficients held in R1/R2/R12 for the whole
# loop. Those scalars are computed a few hundred instructions earlier
# (`0x1c22d7`-`0x1c2353`) from `FUN_b82d41`/`FUN_b82cba`'s dynamics/
# compressor stage output (`0x254978`/`0x2549f8`, fed by `DM(0x252d3c)` --
# docs/findings/06's still-open mix gate) and from a ~26-field master-bus
# parameter block this SAME function decodes fresh every frame at
# `0x1c2e00`-`0x1c2fb0` (source table `0x255fb6`-`0x2560d0`; confirmed by
# execution to read all zero from a synthetic `run_init()` state -- no real
# kit/mixer configuration is ever loaded here). Frame 0's own compressor
# output is a plausible near-unity gain/trim (R1=0.953, R2=0.984,
# R12=-0.029 as IEEE floats); by frame 1 it has collapsed to a degenerate
# all-zero-multiplier state, independent of the injected signal's own
# amplitude (checked: scaling the injected tone down 10x does not change
# this). **This is real firmware behaviour given this lane's own state (a
# synthetic frame with a blank master-bus parameter table and no real
# per-track "slot type" ever reaching `FUN_1cdbb2`, docs/findings/06's mix
# gate), not an emulator bug**: with a genuinely unconfigured mix bus, the
# compressor's own steady state is silence, and frame 0 is riding on one
# frame's worth of "not yet converged" grace period, not a working master
# bus.
#
# **Hand-set value (documented hypothesis, not a discovered real gate).**
# Fully deriving the real per-frame scalars needs the master-bus parameter
# table's own bit layout and `FUN_b82cba`'s own 5-stage recursive state --
# out of this lane's own budget (see the report's "Open" section). Pinning
# R1/R2/R12 to frame 0's own real, execution-confirmed values at every
# recurrence of `pc=0x1c2353` -- the same "register-only override at one
# pc" technique docs/findings/06 already recommends for this exact
# situation, via the same `PatchTable` mechanism `FRAME_PATCH_TABLE` already
# uses -- keeps the compressor's own downstream multiply-accumulate at its
# frame-0 output indefinitely. This is a static, pc-keyed patch applied
# automatically every time this pc recurs (`sharc_survey.apply_patches()`),
# not a per-frame reset performed by this module's own Python loop: the
# cause (a value this lane could not derive) is what gets a fixed
# assumption, not the Runner's state. Known limitation: this freezes the
# compressor's own gain at whatever it computed for frame 0's specific
# transient, so it does not adapt to a different injected level the way a
# real compressor would -- checked and accepted for this lane's own
# single-tone deliverable, not a general fix.
CONTINUOUS_MIX_SCALAR_PC = 0x1C2353
CONTINUOUS_MIX_SCALAR_PATCH: sv.PatchTable = {
    CONTINUOUS_MIX_SCALAR_PC: [
        ("reg", "R1", 0x3F740000),  # ~0.953 -- frame 0's own computed value
        ("reg", "R2", 0x3F7C0000),  # ~0.984
        ("reg", "R12", 0xBCEE0000),  # ~-0.029
    ],
}

# docs/findings/06's "Master stage": `0x1c207b` sums 16 tracks at
# `0x252df8 + t*0x100` bytes each (32 L floats then 32 R floats, plain
# IEEE float32 words -- the same format `read_master_mix()` already reads
# back at `0x25f180`).
TRACK_MIX_BASE = 0x252DF8
TRACK_MIX_STRIDE = 0x100
TRACK_MIX_CHANNEL_WORDS = 32

# The call site `CALL 0x1c207b` inside `FUN_1c2b24` (render_frame): disasm
# `0x1c3099 CALL (linked, delayed) target=0x1c207b`, immediately after
# `CALL 0x1c642a` (`0x1c3083`, the per-voice/accumulate stage) and
# `CALL 0x1c18a6` (`0x1c3090`), and *before* `CALL 0x1c14e7` (`0x1c30a0`,
# the end-of-frame zero-clear of the same 0x252df8 range -- confirmed by
# execution to run *after* this point, so a poke placed here survives into
# this frame's own master-stage read and is only cleared in preparation
# for the *next* frame, the same way a real accumulate would be). Not a
# resolved symbol in tools/sharc_dev/sharc_symbols.py's Profile (this call site, not
# a function entry), so kept as a literal here like the voice record
# offsets above.
MASTER_STAGE_CALL_PC = 0x1C3099


def track_mix_address(track: int, *, channel: str = "L") -> int:
    """The DM address of TRACK's own 32-word L or R half of the master
    stage's per-track input buffer (see TRACK_MIX_BASE's docstring)."""
    if channel not in ("L", "R"):
        raise ValueError("channel must be 'L' or 'R', got %r" % (channel,))
    base = TRACK_MIX_BASE + track * TRACK_MIX_STRIDE
    return base if channel == "L" else base + TRACK_MIX_CHANNEL_WORDS * 4


def read_voice_work_buffer_decimated(state, record: int) -> list[float]:
    """RECORD's own work buffer (FIELD_WORK_BUFFER, 64 floats -- this
    frame's real interpolated render, already produced by the firmware's
    own per-voice dispatch call, whether or not FUN_1c642a's own accumulate
    stage ever sums it into a track buffer), decimated 2:1 the same way
    `decimate()`/the firmware's own `0xb80000` does. Read-only: this is
    "the voice's own output this frame" for measurement (lane E1's own
    per-stage comparison), factored out of `inject_track_buffer()` so a
    caller can read it without also writing it anywhere."""
    work = []
    for i in range(64):
        raw = st._dm_read(state, record + FIELD_WORK_BUFFER + i * 4, 4)
        work.append(
            struct.unpack("<f", struct.pack("<I", raw.value & 0xFFFFFFFF))[0]
            if raw is not None
            else 0.0
        )
    return decimate(work)


def inject_track_buffer(state, record: int, *, track: int = 0) -> list[float]:
    """Read RECORD's own work buffer (see `read_voice_work_buffer_decimated`)
    and write the 32 decimated samples as float32 words into both the L
    and R halves of TRACK's master-stage input buffer
    (`track_mix_address()`). Mono only (both channels get the same
    samples) -- there is no evidence yet for a real per-channel pan value
    to set instead. Returns the 32 decimated floats written, for a caller
    to inspect or assemble into a WAV alongside ring A."""
    decimated = read_voice_work_buffer_decimated(state, record)
    for i, value in enumerate(decimated):
        word = _float_to_word(value)
        _poke(state, track_mix_address(track, channel="L") + i * 4, word)
        _poke(state, track_mix_address(track, channel="R") + i * 4, word)
    return decimated


# --- Why the track buffer never reaches the master mix as audio (lane D1,
# 2026-09-25), and injecting past it instead -----------------------------
#
# `inject_track_buffer()`'s own format/address assumption (32 planar L
# floats then 32 planar R floats at `0x252df8 + t*0x100`, `TRACK_MIX_BASE`
# above) is CORRECT -- confirmed by execution: `FUN_1c207b`'s own per-track
# dispatch loop (`0x1c20e7`-`0x1c212a`, a `DO...UNTIL LCE` over all 16
# tracks) walks `I13` from `TRACK_MIX_BASE` in `TRACK_MIX_STRIDE` steps and
# reads/writes exactly the byte offsets `track_mix_address()` computes.
# **The problem is downstream of that: this loop's own per-track branch
# never carries the track's own sample VALUES into the master mix at all,
# under any of the three states this lane exhaustively tried.** Per track,
# `R2` (the loop counter, 0..15) is bit-tested against two masks read once
# per track: `R8 = DM(0x252728)` (`0x1c20e7`) and `R4 = DM(0x252538)`
# (`0x1c20b9`), giving exactly three outcomes, all checked by execution
# with watchpoints on `0x252df8`-`0x253df8` and `0x25f180`-`0x25f280`:
#
#   1. `btst(R8, R2)` bit set -> `JUMP IF NOT SZ` (`0x1c20f4`) skips
#      straight to `0x1c2121` (next track): the track buffer is never
#      touched at all.
#   2. Bit clear in (1), then `btst(R4, R2)` bit clear -> `JUMP IF SZ`
#      (`0x1c20f9`) falls through into `FUN_1c238a` (`0x1c245b`, reached by
#      a plain in-function JUMP, not a CALL -- `tools/sharc_dev/sharcdb/`'s own
#      function-boundary split puts it in a *different* `functions` row,
#      which is why an earlier pass of this lane's own pc-range filter
#      wrongly reported zero track-buffer activity here). This is the
#      DEFAULT outcome from any synthetic `run_init()` state (both masks
#      read all-zero -- see below), and it DOES read the track buffer
#      (confirmed: `0x1c246c`/`0x1c2472` fire on `DM(0x252df8)` range with
#      no patches at all).
#   3. Bit clear in (1), bit set in (2) -> falls through to `0x1c20fc`,
#      the "normal" path this lane originally assumed was the summer: also
#      reads the track buffer (via a hand patch setting `DM(0x252538)`
#      bit 0, since it never happens by default).
#
# Both (2) and (3) run the SAME 16-iteration copy idiom (`0x1c2106`-
# `0x1c211b` / `0x1c2466`-`0x1c247b`): per iteration the loop advances its
# own OWN track-side pointer by only 2 words (8 bytes), so across 16
# iterations it visits word offsets `{0, 2, 4, ..., 30}` of each 32-word
# channel half and NEVER `{1, 3, 5, ..., 31}` -- a real, confirmed
# half-decimation of whatever the track buffer holds. But that isn't even
# the dominant effect: dumping the copy's own destination scratch buffer
# (`0x254878` for path (3)) after a full run shows the copied VALUES are
# exactly the injected samples (still smooth, still correlated with a
# ramp/sine's own shape) -- so the copy itself is faithful, just to a
# SCRATCH buffer, not to the master mix. **The master mix's own 64 words
# are separately, unconditionally overwritten every frame by a THIRD piece
# of this same function** (`0x1c2200`-`0x1c2385`, `CALL 0xb82d41`/
# `CALL 0x1cb3d8` -- the dynamics/compressor stage docs/findings/06 and
# lane B2 already named, and the same `R1`/`R2`/`R12` scalars
# `CONTINUOUS_MIX_SCALAR_PATCH` pins), which reads/writes `DM(0x25f180)`/
# `DM(0x25f200)` directly (via `I2 = R4`, `FUN_1c207b`'s own first
# argument, confirmed `== 0x25f180` at its call site `0x1c771b`/`0x1c771e`
# in the block handler) through a recursive envelope-follower/soft-clip,
# NOT a read of the per-track copy's own destination buffer. **Proven by a
# discriminating experiment, not just by reading the disassembly:**
# injecting a CONSTANT level (0.2 into every one of a track's 64 samples)
# in place of the ramp/sine produces the SAME sparse addressing pattern
# (identical indices populated) but DIFFERENT, smoothly-decaying values at
# those indices (a decay trajectory shaped like an RC/compressor
# time-constant, not a repeated constant) -- so what reaches the master
# mix tracks the compressor's own internal envelope state, not the track
# buffer's per-sample content, regardless of stride or branch taken. Two of
# the three masks feeding this whole dispatch (`0x1c2e0e`'s own write of
# `DM(0x252728)`, `0x1c2e58`'s own write of `DM(0x252538)`) are themselves
# decoded every frame from a >0x6ac-byte span of DM memory
# (`0x255908`-`0x25600a`) that reads all-zero in every capture and every
# synthetic `run_init()` state this project has -- the same class of
# "no real kit/mixer configuration is ever loaded here" gap
# `CONTINUOUS_MIX_SCALAR_PATCH`'s own note and lane B2's report already
# flagged for the neighbouring master-bus parameter table. **This lane did
# not find a way to make the real per-track summation carry audio-rate
# content into the master mix** with the information available (the
# control table's real bit layout, and whether a genuine "no real mixer
# configured" state ever produces a clean signal on real hardware, are
# both still open -- see the report). Given that, and this lane's own
# brief (get a clean tone into ring A), the fix taken here is to inject
# PAST this entire unresolved stage, at the one place C1's own report
# already proved is a lossless, bit-exact copy of whatever the master mix
# holds (`tools/sharc_dev/sharc_dac.py`'s own module docstring) -- the same kind of
# "bypass the not-yet-resolved stage, document the hypothesis" move
# `inject_track_buffer()` above already makes for `FUN_1c642a`'s own
# accumulate stage, one stage further downstream.
#
# `FUN_1c74a1` (the Q31/ring-A converter) is called at sw `0x1c7734`,
# inside the audio task's block handler, immediately after `FUN_1c2b24`
# (the whole render orchestrator -- `CALL` at sw `0x1c771e`, `R4 =
# 0x25f180` set at `0x1c771b`) has returned for this frame; the only code
# between those two calls (`0x1c7721`-`0x1c7731`) sets up the ring-A
# destination pointer/flag and touches nothing in `0x25f180`-`0x25f280`
# (confirmed by disassembly and by execution). A write placed exactly here
# lands after `FUN_1c207b`'s own dynamics loop has already finished
# clobbering the master mix for this frame, and before anything reads it
# back for conversion -- the same "confirmed by execution to run after
# this point" placement rule `MASTER_STAGE_CALL_PC` above already
# documents, one call frame up.
MASTER_MIX_INJECT_PC = 0x1C7734

# --- Ring A's own L channel is deliberately negated after conversion
# (lane D1, 2026-09-25; new finding, not a fix) ---------------------------
#
# The first render through `MASTER_MIX_INJECT_PC` produced a "silent" mono
# downmix (`ring_a_mono` all zero, `power_ratio` 0.0) even though
# `injected_max_abs` was clearly nonzero and `read_master_mix()` showed the
# expected samples, in BOTH the L and R halves (this lane writes the same
# value to both, matching `inject_track_buffer()`'s own mono convention).
# Traced with a write watchpoint on the ring-A destination word
# (`0x261cc8`, ring A L position 0): `FUN_1c74a1` itself stores the
# CORRECT, positive Q31 value there (confirmed: raw store value
# `214748368` for a `0.1` input, matching `0.1 * 2**31` to rounding) --
# `FUN_1c74a1`/`fix_by` is NOT the bug lane C1's own isolated test already
# said it wasn't. But a SECOND write to the SAME address happens moments
# later, before the frame's own `RETURN`: a dedicated 16-iteration loop at
# sw `0x1c758b`-`0x1c75a2` (`I4 = ring-half-offset + 0x261cc8`, i.e. the
# SAME half `FUN_1c74a1` just wrote) that does `R1 = neg(R2)` /
# `R2 = neg(R2)` on every word it touches and stores the results straight
# back -- reached unconditionally every frame between `CALL FUN_1c74a1`
# (`0x1c7734`) and `FRAME_MILESTONE` (`0x1c75d3`), confirmed by execution
# to touch only the L-channel (even) words of the half just written, never
# the R-channel (odd) ones (watched: only `0x261cc8` got a second write;
# `0x261ccc`, R position 0, got exactly one write, from `FUN_1c74a1`
# itself). **This is real, deliberate firmware code, not an emulator
# artifact** -- a plain `neg()` compute op, not a decode gap or a width
# mismatch, executed on data `FUN_1c74a1` had already written correctly.
# The most likely explanation is a genuine hardware/output convention
# (e.g. an inverting stage on the L channel only, or an intentional
# stereo-phase convention); this lane did not chase why, only confirmed
# that it happens, unconditionally, every frame, and that it means: ring
# A's own L channel is the exact negation of `read_master_mix()`'s own L
# channel, while R matches directly.
#
# **Consequence for measurement, not a bug to route around:** a coherent
# signal written identically to both master-mix channels (as
# `inject_master_mix()` does) arrives at ring A as `(-L, +R)` with equal
# magnitude -- so the OLD `ring_a_mono = 0.5*(left+right)` downmix
# `render_frames_to_ring_a()` used to compute cancels to exactly zero for
# such a signal (which is what made this lane's own first full render look
# completely silent). `render_frames_to_ring_a()` below now also returns
# `ring_a_left`/`ring_a_right` (undownmixed) so a caller measures each
# channel on its own -- exactly what the report's own brief asks for
# ("THD+N per channel") -- instead of through a downmix that assumes the
# channels are in phase. `ring_a_mono` is kept for callers that still want
# it (e.g. comparing against noise-like, non-phase-related content from an
# earlier lane), documented here as unreliable for a coherent tone.


def inject_master_mix(state, samples: Sequence[float]) -> list[float]:
    """Write SAMPLES (mono; up to `sharc_dac.MASTER_MIX_CHANNEL_WORDS`,
    zero-padded if shorter) as identical float32 words into both the L and
    R halves of the master mix (`sharc_dac.MASTER_MIX_BASE`/
    `MASTER_MIX_R_OFFSET_WORDS`, lane C1's own confirmed format) at
    `MASTER_MIX_INJECT_PC` -- see this section's own module note for why
    the master mix, not the track buffer, is the right injection point
    for a clean tone today. Returns the (possibly zero-padded) list
    actually written, for a caller to log or compare."""
    channel_words = sharc_dac.MASTER_MIX_CHANNEL_WORDS
    padded = list(samples[:channel_words])
    padded += [0.0] * (channel_words - len(padded))
    for i, value in enumerate(padded):
        word = _float_to_word(value)
        _poke(state, sharc_dac.MASTER_MIX_BASE + i * 4, word)
        _poke(
            state,
            sharc_dac.MASTER_MIX_BASE + (sharc_dac.MASTER_MIX_R_OFFSET_WORDS + i) * 4,
            word,
        )
    return padded


def call_frame_with_track_injection(
    runner: sr.Runner,
    image: str,
    record: int,
    *,
    track: int = 0,
    patch_table: sv.PatchTable | None = FRAME_PATCH_TABLE,
    max_steps: int = 4_000_000,
    write_master_mix: bool = True,
    inject_track: bool = True,
    frame_bytes: bytes | None = None,
    watchpoints: Sequence[sr.Watchpoint] = (),
    post_step_hook: Callable[[sr.Runner], None] | None = None,
) -> tuple[sr.Runner, sr.RunResult, list[float]]:
    """Like `call_frame()`, but (when INJECT_TRACK, the default) calls
    `inject_track_buffer()` the instant `MASTER_STAGE_CALL_PC` is about to
    execute -- see this section's own module note for why (the accumulate
    stage's real enable value was not established from a synthetic, empty
    frame; see FRAME_BYTES below for using a real one instead). Steps one
    instruction at a time (not `sharc_survey.run_with_patches()`/
    `run_collect_all()`) applying `patch_table` the same way `call_frame()`
    does; a fork this halts on is a genuinely new, unresolved gap (report
    it, do not add a guess here), not something to drive through with a
    default branch pick, since this function's whole point is to observe
    the master stage's own, unperturbed behaviour once it has real
    per-track input.

    `FRAME_BYTES`, if given (`capture_frame_bytes()`'s own return, lane E1),
    is written at CAPTURE_FRAME_BASE (`write_capture_frame()`) before
    `fresh_call` -- so this one frame's own render sees a real device's
    mixer/gate tables, not a blank synthetic frame, for everything except
    the one voice this harness itself drives via `record`.

    `INJECT_TRACK` (default True) controls whether `inject_track_buffer()`
    actually pokes TRACK's master-stage input; when False, the voice's own
    decimated output is still read every call (via
    `read_voice_work_buffer_decimated()`, the same 32 floats
    `inject_track_buffer()` would have written) for the returned
    `injected`/measurement value, but nothing is written to
    `track_mix_address()` -- for measuring whether real frame data alone
    (FRAME_BYTES) makes FUN_1c642a's own accumulate loop carry the voice
    there without this hack.

    When WRITE_MASTER_MIX is true (the default) and INJECT_TRACK is also
    true, the SAME 32 samples `inject_track_buffer()` just computed are
    also written straight into the master mix (`inject_master_mix()`) the
    instant `MASTER_MIX_INJECT_PC` is about to execute, later in this same
    step loop -- see the "why the track buffer never reaches the master
    mix as audio" module note above `MASTER_MIX_INJECT_PC` for why this was
    necessary to get a clean tone out of a blank synthetic frame. When
    INJECT_TRACK is False but WRITE_MASTER_MIX is still True, the voice's
    own read-only decimated output is written to the master mix directly
    (bypassing the track buffer and its gate entirely) instead of the
    track-buffer injection's own result, so the two hacks can be toggled
    independently. Pass WRITE_MASTER_MIX=False to get the old (pre-lane-D1)
    track-buffer-only behaviour, or both False to run with no injection
    hack at all (real frame data, real gate, real dispatch only).

    `WATCHPOINTS` (lane K1, 2026-09-26), if given, is attached to the
    `fresh_call()` Runner this makes internally (`Runner.attach_watchpoints()`
    -- `fresh_call()` itself never carries watchpoints over, see its own
    docstring), the same way `tools/sharc_dev/sharc_replay.py`'s
    `call_frame_collect_all_with_hits()` already does for its own
    `target_watch` -- so a caller driving several frames through this
    function on one continuous Runner can log writes (e.g. a voice
    record's own fields) across the whole run without a second pass.

    `POST_STEP_HOOK` (lane N2, 2026-09-26), if given, is called as
    `post_step_hook(new_runner)` immediately after every single
    `new_runner.step()` in this function's own instruction loop (i.e. at
    genuine per-instruction granularity, the same loop `MASTER_STAGE_CALL_PC`/
    `MASTER_MIX_INJECT_PC` above already use, not a once-per-frame callback).
    This exists because `tools/sharc_dev/sharc_replay.py`'s own voice keep-alive
    (`replay_armed_voice(inject_real_sample=True)`) needs to react to a
    `WATCHPOINTS` hit -- a voice record field the firmware's own per-frame
    "clear" utility (docs/findings/06 lane K1's `0x1c4e70`) just zeroed --
    and re-poke it *before* the same frame's own render check runs a few
    instructions later, which a poke applied only between frames (after
    `call_frame_with_track_injection` already returned) is too late for:
    the very same generic clear runs again at the *start* of next frame's
    dispatch, before this function would get another chance to intervene.
    This module deliberately does not know what the hook does (voice
    field semantics belong in `tools/sharc_dev/sharc_replay.py`, this function only
    provides the instruction-granular timing hook) -- see that module's
    own `_make_voice_rearm_hook()`.

    Returns `(new_runner, RunResult, injected)` where `injected` is the 32
    decimated floats (written or not, depending on the flags above), or
    `[]` if `MASTER_STAGE_CALL_PC` was never reached (an earlier stop)."""
    p = profile(image)
    new_runner = runner.fresh_call(p.block_handler, diagnose_unknown=True)
    if watchpoints:
        new_runner.attach_watchpoints(list(watchpoints))
    if frame_bytes is not None:
        write_capture_frame(new_runner.state, frame_bytes)
    start_pc_sw = new_runner.state.pc_sw
    injected: list[float] = []
    steps = 0
    halt: sr.Halt | None = None
    t0 = time.perf_counter()
    while steps < max_steps:
        pc_sw = new_runner.state.pc_sw
        if pc_sw == MASTER_STAGE_CALL_PC:
            if inject_track:
                injected = inject_track_buffer(new_runner.state, record, track=track)
            else:
                injected = read_voice_work_buffer_decimated(new_runner.state, record)
        elif write_master_mix and pc_sw == MASTER_MIX_INJECT_PC:
            inject_master_mix(new_runner.state, injected)
        if patch_table:
            sv.apply_patches(new_runner, patch_table)
        try:
            new_runner.step()
        except sr.Halt as exc:
            halt = exc
            break
        if post_step_hook is not None:
            post_step_hook(new_runner)
        steps += 1
    else:
        halt = sr.Halt("max-steps", new_runner.state.pc_sw)
    result = sr.RunResult(
        halt=halt,
        instructions=new_runner.instructions,
        elapsed=time.perf_counter() - t0,
        form_counts=new_runner.form_counts,
        start_pc_sw=start_pc_sw,
        final_pc_sw=new_runner.state.pc_sw,
        max_call_depth_reached=new_runner.max_call_depth_reached,
        watch_log=new_runner.watch_log,
    )
    return new_runner, result, injected


def _merge_patch_tables(*tables: sv.PatchTable | None) -> sv.PatchTable:
    """Union TABLES' entries by pc, concatenating entry lists for a pc that
    appears in more than one (later tables' entries applied after earlier
    ones, at the same pc, by `sharc_survey.apply_patches()`'s own "list of
    entries" convention -- see its docstring). `None` entries are skipped,
    so a caller can pass an optional table straight through."""
    merged: sv.PatchTable = {}
    for table in tables:
        if not table:
            continue
        for pc, entries in table.items():
            merged.setdefault(pc, []).extend(entries)
    return merged


def read_track_mix(state, track: int) -> tuple[list[float], list[float]]:
    """TRACK's own 32+32 planar float32 (L, R) master-stage input words
    (`track_mix_address()`) -- the same address `inject_track_buffer()`
    writes and `FUN_1c207b`'s own per-track dispatch reads, for measuring
    that stage's own content directly (lane E1's "measure at each stage")
    instead of only inferring it from ring A/master-mix downstream."""

    def read_channel(channel: str) -> list[float]:
        base = track_mix_address(track, channel=channel)
        out = []
        for i in range(TRACK_MIX_CHANNEL_WORDS):
            raw = st._dm_read(state, base + i * 4, 4)
            out.append(
                struct.unpack("<f", struct.pack("<I", raw.value & 0xFFFFFFFF))[0]
                if raw is not None
                else 0.0
            )
        return out

    return read_channel("L"), read_channel("R")


def render_frames_to_ring_a(
    memory,
    image: str,
    *,
    n_frames: int = 32,
    voice: int = 0,
    freq: float = 1000.0,
    pitch_step: float = 1.0,
    track: int = 0,
    sample_format: str = "int16",
    patch_table: sv.PatchTable | None = FRAME_PATCH_TABLE,
    mix_scalar_patch: sv.PatchTable | None = CONTINUOUS_MIX_SCALAR_PATCH,
    max_steps: int = 4_000_000,
    write_master_mix: bool = True,
    inject_track: bool = True,
    capture: sharc_capture.Capture | None = None,
    capture_frame_start: int | None = None,
    mask_master_bus_table: bool = True,
    provisional_forms: Sequence[str] = (),
    provisional_interpretations: Mapping[str, str] | None = None,
) -> dict:
    """Render N frames of one voice's own `freq` Hz sine on ONE continuous
    Runner (one `run_init()`, N `Runner.fresh_call()`s at block_handler via
    `call_frame_with_track_injection()`), injecting each frame's real
    decimated output into `track`'s master-mix input (see
    `inject_track_buffer()`) AND, when WRITE_MASTER_MIX is true (the
    default), straight into the master mix itself (see
    `inject_master_mix()` and the module note above `MASTER_MIX_INJECT_PC`
    for why both are needed for a clean tone), and return ring A's own
    output -- the actual DAC-facing buffer, one stage past the master mix
    -- concatenated across frames as mono (L+R averaged) floats at the
    firmware's own decimated output rate (`SOURCE_SAMPLE_RATE / 2` =
    48 kHz).

    **One continuous Runner (lane B2, 2026-09-25; was N independent
    post-init renders under lane A2).** That version's own two blockers
    (`sharc_run.State.trace`'s unbounded growth across repeated calls on
    one Runner, and an "unsupported Type14a odd UREG pair" stop at
    0x1c32ad on the second call onward) are both fixed on this branch
    (7e5e8c4) -- checked here by running many frames on one Runner with no
    new stop and bounded memory. The frame-0-only silence this module's own
    "Continuity across frames, root-caused" note (above `TRACK_MIX_BASE`)
    documents was a THIRD, separate problem (the master stage's own
    per-track gain scalars collapsing to zero by frame 1, given this lane's
    blank synthetic mix configuration) -- fixed here via `mix_scalar_patch`
    (`CONTINUOUS_MIX_SCALAR_PATCH` by default; see that constant's own
    docstring for the full evidence and its documented-hypothesis status).
    Kept even though `write_master_mix` now overwrites the master mix
    downstream every frame: `FUN_1c207b` still runs its own compressor
    loop unconditionally either way, and pinning its own scalars is what
    keeps THAT loop itself (not our own override) from forking or
    otherwise misbehaving across many frames -- see the module note for
    why its own output is discarded rather than trusted.

    **Master mix is no longer trusted as an audio-rate mixer (lane D1,
    2026-09-25).** `FUN_1c207b`'s own per-track dispatch does not carry a
    track's sample values into the master mix under any state this lane
    found (see the module note above `MASTER_MIX_INJECT_PC`); the fix is
    to inject the SAME decimated samples a second time, downstream of that
    entire stage, directly into the master mix, at the point lane C1
    already proved is a lossless, bit-exact source for ring A.

    **Phase continuity comes from one long source buffer plus the voice
    record's own carried state**, both true to how the firmware actually
    plays a long sample: `setup_voice()`/`_write_samples()` run ONCE,
    before the frame loop, over a buffer sized for all `n_frames` (unlike
    the old per-frame-independent version, which needed the source tone
    itself to carry phase since every frame's record restarted at
    `start=0`).

    **CAPTURE (lane E1, 2026-09-25).** If given (an `emu.sharc_capture.load()`
    result), frame `i`'s own real RX bytes -- `capture_frame_bytes(capture,
    (capture_frame_start or find_settled_capture_frame(capture,
    min_frame=DEFAULT_CAPTURE_FRAME_INDEX)) + i)`, wrapping modulo
    `len(capture.dspi2_frames)` if `n_frames` runs past the end -- are
    written at CAPTURE_FRAME_BASE before that frame's own call (via
    `capture_frame_bytes_for_render()`, `MASK_MASTER_BUS_TABLE` -- default
    True -- zeroing MASTER_BUS_TABLE_RANGE; see that constant's own
    docstring for why: real content there deterministically trips a
    brand-new sharc_core gap on the second real-frame call), so FUN_1c642a's
    per-track dispatch and FUN_1c207b's master-stage compressor see a real
    device's own machine-type and per-track gate tables (not a blank
    synthetic frame) for every track except `voice`'s own synthetic tone.
    Without `capture` (the default, `None`), behaviour is unchanged from
    before this lane: no frame bytes are written at all.

    `INJECT_TRACK` (default True) and `WRITE_MASTER_MIX` (default True) are
    passed straight through to `call_frame_with_track_injection()` (see its
    own docstring): both True is this project's original hack combination;
    either or both False, together with `capture` given, is how this lane
    measures whether real frame data removes the need for a given hack --
    see this module's own report for the actual per-configuration results.

    **`PROVISIONAL_FORMS`/`PROVISIONAL_INTERPRETATIONS` (lane E1).** Passed
    to `run_init()` (see its own docstring), so they cover every frame this
    call makes, not just init's own run. Real CAPTURE bytes hit a genuinely
    new fork from frame 1 onward that an empty synthetic frame never
    reached: an "uncertain or undecodable form" halt at 0x1c32b0
    (`21p_undoc16`, this repo's scratchpad STATE.md's own "opt-in
    provisional interpretation ... stops at 0x1c32b1" note) -- pass
    `provisional_forms=["21p_undoc16"],
    provisional_interpretations={"21p_undoc16": "nop"}` to get past it (a
    `sharc_core`-provided, already-tested opt-in, not a fix made here); left
    off by default so a caller not using `capture` sees unchanged behaviour.

    Returns `{"ring_a_mono": [...], "ring_a_left": [...], "ring_a_right":
    [...], "per_frame": [...], "any_new_stop": bool}`; `per_frame[i]` adds
    `"track_mix_left"/"track_mix_right"` (`read_track_mix()`, TRACK's own
    master-stage input after this frame) and `"master_mix"` (`read_master_mix()`,
    64 floats) to `{"frame", "instructions", "halt", "injected_max_abs",
    "ring_a_nonzero"}`, for measuring each stage of voice -> track -> master
    -> ring A per frame, not just the final ring A output. Measure
    `ring_a_left`/`ring_a_right` separately, not `ring_a_mono` -- see the
    "ring A's own L channel is deliberately negated" module note above
    `MASTER_MIX_INJECT_PC` for why the mono downmix cancels to silence for
    a coherent tone.
    """
    init = run_init(
        memory,
        image,
        provisional_forms=provisional_forms,
        provisional_interpretations=provisional_interpretations,
    )
    if not init.ran or init.runner is None:
        raise ValueError("render_frames_to_ring_a: run_init failed: %s" % init.error)

    sample_len = _default_sample_len(n_frames, pitch_step)
    sample_base = 0x310000
    combined_patches = _merge_patch_tables(patch_table, mix_scalar_patch)

    capture_start: int | None = None
    if capture is not None:
        capture_start = (
            capture_frame_start
            if capture_frame_start is not None
            else find_settled_capture_frame(
                capture, min_frame=DEFAULT_CAPTURE_FRAME_INDEX
            )
        )
        if capture_start is None:
            raise ValueError(
                "render_frames_to_ring_a: capture has no frame past "
                "min_frame=%d satisfying SETTLED_FRAME_RANGES"
                % DEFAULT_CAPTURE_FRAME_INDEX
            )
        # Narrow for mypy: capture_start is int from here on (the loop
        # below re-checks `capture is not None`, a separate condition mypy
        # cannot tie back to this one across the intervening code).
        assert capture_start is not None

    runner = init.runner
    state = runner.state
    tone = [
        math.sin(2 * math.pi * (freq / SOURCE_SAMPLE_RATE) * i)
        for i in range(sample_len)
    ]
    _write_samples(state, sample_base, tone, sample_format)
    record = setup_voice(
        state,
        image,
        voice,
        sample_len=sample_len,
        pitch_step=pitch_step,
        sample_base=sample_base,
    )
    setup_frame(state, image, command=3, ring_flag=0)

    ring_a_mono: list[float] = []
    ring_a_left: list[float] = []
    ring_a_right: list[float] = []
    per_frame: list[dict] = []
    for frame_index in range(n_frames):
        frame_bytes = None
        if capture is not None:
            assert capture_start is not None  # established above; see its own note
            index = (capture_start + frame_index) % len(capture.dspi2_frames)
            frame_bytes = capture_frame_bytes_for_render(
                capture, index, mask_master_bus_table=mask_master_bus_table
            )
        runner, result, injected = call_frame_with_track_injection(
            runner,
            image,
            record,
            track=track,
            patch_table=combined_patches,
            max_steps=max_steps,
            write_master_mix=write_master_mix,
            inject_track=inject_track,
            frame_bytes=frame_bytes,
        )
        ring = read_ring_a(memory, image, runner)
        left = [v or 0.0 for v in ring["left"]]
        right = [v or 0.0 for v in ring["right"]]
        mono = [
            0.5 * (l_sample + r_sample)
            for l_sample, r_sample in zip(left, right, strict=True)
        ]
        ring_a_left.extend(left)
        ring_a_right.extend(right)
        ring_a_mono.extend(mono)
        track_left, track_right = read_track_mix(runner.state, track)
        per_frame.append(
            {
                "frame": frame_index,
                "instructions": result.instructions,
                "halt": result.halt.reason,
                "injected_max_abs": max((abs(v) for v in injected), default=0.0),
                "ring_a_nonzero": sum(1 for v in left if v)
                + sum(1 for v in right if v),
                "track_mix_left": track_left,
                "track_mix_right": track_right,
                "master_mix": read_master_mix(memory, image, runner),
            }
        )
    return {
        "ring_a_mono": ring_a_mono,
        "ring_a_left": ring_a_left,
        "ring_a_right": ring_a_right,
        "per_frame": per_frame,
        "any_new_stop": any(
            f["halt"] != "return without followed call" for f in per_frame
        ),
        "capture_frame_start": capture_start,
    }
