"""Memory layout profile, voice record field offsets and shared constants."""

from __future__ import annotations

from tools.sharc_dev import sharc as sharcmod
from tools.sharc_dev import sharc_symbols

# Firmware addresses (functions, tables) come from tools/sharc_dev/sharc_symbols.py's
# resolve(), not a hardcoded dict: cached per image so repeated calls in one
# process (render_blocks() in a loop, the test suite) reuse one resolution.
_PROFILE_CACHE: dict[str, sharc_symbols.Profile] = {}


def profile(image: str) -> sharc_symbols.Profile:
    """This image's resolved symbol table (tools/sharc_dev/sharc_symbols.py), cached.
    Raises sharc_symbols.SymbolResolutionError if a symbol this harness
    needs (voice_render, voice_records, coeff_table, ...) does not resolve
    -- loudly, at call time, rather than silently rendering from a stale
    hardcoded address."""
    if image not in _PROFILE_CACHE:
        _PROFILE_CACHE[image] = sharc_symbols.resolve(
            sharcmod.load(image), device="dt2"
        )
    return _PROFILE_CACHE[image]


# Voice record layout: not independently-addressed symbols, so these stay
# as constants here rather than moving into sharc_symbols.py.
#
# 32 voice records, 0x1d8 bytes apart, at profile(image).voice_records
# (docs/findings/06's voice record contract, two-agent-checked on the 1.16
# bytes).
VOICE_RECORD_STRIDE = 0x1D8
VOICE_RECORD_COUNT = 32

# Voice record field offsets, bytes, from docs/findings/06's voice record
# contract table (two-agent-checked on the 1.16 bytes) plus this lane's own
# byte-level trace of FUN_1c4ecf/FUN_1c4f81 (see this module's docstring)
# for FIELD_SAMPLE_PTR, previously undocumented ("[O]" in the contract
# table) and empirically mis-set.
FIELD_SAMPLE_PTR = 0x0  # word 0: pointer to raw PCM; null = zero-fill gate
FIELD_WORK_BUFFER = 0x4  # 64 floats, written by render_body
FIELD_PREVIOUS_SAMPLE = 0x180  # word 0x60
FIELD_RATE = 0x184  # word 0x61, u32
# Word 0x62-0x63 (bytes +0x188/+0x18C): this lane's own finding, task 3.
# docs/findings/06 only documents +0x188 as an *init* write ("+0x188 =
# 0x170" -- 368, matching the ROM default sample's length) and does not
# say what a real trigger does with it. FUN_1c4f81's past-limit test reads
# it back (this lane's task brief's static facts: "combined with words
# 0x62/0x63 (+0x188/+0x18C, merged as (w63<<31)|(w62>>1))") *before* the
# END/LOOP_START-derived span, so it is compared as an independent bound,
# not derived from END. Left at init's stale 368 while ``setup_voice()``
# points the record at a differently-sized buffer, this lane's own
# ``check_correctness()`` deactivated the voice after exactly one block
# regardless of the real END/START/LOOP_START span (this module's own
# test asserts against this regression); writing FIELD_SAMPLE_LENGTH here
# to the same length ``end``/FIELD_END uses removes that spurious
# deactivation in every case this lane tried (unity, half and double
# pitch, sine and ramp) -- see check_correctness()'s docstring for the
# margin this in turn requires. Not yet run against a real trigger, so
# this is this lane's own finding, not a docs/findings/06 correction --
# record it there once a second agent checks it against the image bytes.
FIELD_SAMPLE_LENGTH = 0x188  # word 0x62, u32 (not Q31-paired with 0x18C)
FIELD_SAMPLE_LENGTH_HI = 0x18C  # word 0x63; 0 in every case seen so far
FIELD_LOOP_START = 0x190  # words 0x64-65, Q31 int64 low-first
FIELD_START = 0x198  # words 0x66-67
FIELD_END = 0x1A0  # words 0x68-69
FIELD_STEP = 0x1A8  # words 0x6a-6b, signed Q31 int64
FIELD_PHASE = 0x1B0  # words 0x6c-6d, Q31 int64
FIELD_FADE_IN = 0x17C
FIELD_ZERO_CROSS_MUTE = 0x17D
FIELD_RESEED = 0x17E
FIELD_ACTIVE = 0x1B8
FIELD_SEED_PENDING = 0x1BA
FIELD_REVERSE = 0x1BB
FIELD_LOOP = 0x1BC

# 128-phase, 6-tap polyphase coefficient table layout at
# profile(image).coeff_table: 6 (swse) int16 taps per phase, one per 4-byte
# (normal-word) slot, 24 bytes/phase (docs/findings/06's voice record
# contract, "Render and declick").
COEFF_TABLE_TAPS = 6
COEFF_TABLE_STRIDE_BYTES = 24

# The real caller's second argument, found for this lane's task 1 by
# tracing FUN_1c642a's own call sites (out/sharcdb, tools/sharc_dev/sharc.py's
# last_def()/print_slice()): R4 is the record base (already documented,
# see the module docstring's "Corrected voice call convention"), and R12
# is *not* a magic constant -- it is DM(SESSION_TICK_ADDR) at the moment
# of the call. Evidence:
#
#   sw 0x1c6501  R13 = pass(R2); R15 = DM(I2, M5)   -- I2 resolves (ptr
#                table, tools/sharc_dev/sharcdb.py) to SESSION_TICK_ADDR; this read
#                happens once per FUN_1c642a call, before the 32-voice
#                dispatch loop, so it is loop-invariant across every voice
#                that loop calls.
#   sw 0x1c6aee  R4 = pass(R13); R12 = R15           -- selector 2's call
#   sw 0x1c6afd  R4 = pass(R13); R12 = R15           -- the "any other"
#                call (FUN_1c4ecf, the one this harness renders).
#
# Inside the callee, this is *not* read as R12 directly where the static
# facts' "R0" note describes it -- sw 0x1c4ef5 "R2 = pass(R12); DM(I6-8) =
# R2" copies it, and sw 0x1c4ef8 "R0 = add(R2, R2)" doubles it before
# FUN_1c4ecf's own zero-fill gate even runs. That doubled value is what
# sw 0x1c4f1b spills (DM(I6-5) = R0) and FUN_1c4f81's past-limit test later
# reloads three times (sw 0x1c4fb1/0x1c4fb7/0x1c4fbc, as R12/R14/R15) --
# so "the caller's R0" in this lane's brief is this harness's R12 argument,
# doubled by the callee itself, not a third register the caller sets.
#
# SESSION_TICK_ADDR itself: FUN_1c15e3 (init) seeds it to the *literal*
# 0x20 (32) -- sw 0x1c1621 "M3 = 0x20", sw 0x1c1643 "DM(0x252d3c) = M3",
# both inside init's own function body (img.func(0x1c1643) resolves to
# FUN_1c15e3) -- not a guess: this lane's own post-init snapshot reads back
# exactly 32 there. The only other writer in the covered call graph is
# FUN_1cdbb2 (docs/findings/06: "a conditional per-track state
# accumulator", called from FUN_1c642a itself, sibling to the render
# calls) -- img.reach(profile(image).init, FUN_1cdbb2's entry) is False,
# so init alone never reaches it and this harness, which never calls
# FUN_1c642a either, has no path to it either: SESSION_TICK_ADDR is read
# fresh at every call_render() (never advanced by this harness), which is
# an open point ([O]), not a claim that a real multi-block run holds it
# fixed at 32.
#
# Without this, call_render() left R12 at whatever register value
# run_init()'s own ~1.24M instructions happened to leave it at (observed:
# 0, on this lane's post-init snapshot -- a leftover, not a 0 the firmware
# ever intended), so the callee's doubled R0 was 0 instead of 64. That
# 0-vs-64 difference is this lane's root cause for the voice deactivating
# after block 0 -- see check_correctness()'s docstring and this module's
# test coverage for the before/after.
SESSION_TICK_ADDR = 0x252D3C

Q31 = 1 << 31

# The firmware's own sample-playback rate: the rate a raw PCM sample is
# assumed to be recorded at when pitch_step=1.0 means "native speed" in the
# DO-64 render domain FUN_1c4f81's interpolation loop and 0xb80000's 2:1
# decimator operate in. Two independent 96000.0 literals in the image, both
# cited in docs/findings/06-sharc-engine-and-startup.md:
#
#   - "The voice record contract"/"Step [C][V]" (line ~3385): the four
#     step-setters (0x1c4afe, 0x1c4bf9, 0x1c4d88 and, by the same words,
#     0x1c4a31) compute ``step = ratio * rate / 96000 * 2**31`` -- ``rate``
#     is the voice record's own word 0x61 (FIELD_RATE, "u32", "not checked"
#     in the contract table but read by exactly these three setters), so
#     96000 is the reference this per-voice sample rate is normalized
#     against to land in the same Q31 units as ``pitch_step`` here. This is
#     the direct evidence: it is the same voice-render engine's own step
#     computation, not an unrelated filter.
#   - A classifier false-positive investigation (line ~2152) independently
#     found the float immediates pi, pi/8 and 96000.0 together in a
#     different function (blk93@0x1c5ed4, a `2*pi*f/fs` filter-coefficient
#     calculator elsewhere in the DSP chain) and concluded "The sample rate
#     is 96 kHz" from that alone -- corroborating, but not this constant's
#     primary evidence (that function is not part of the voice-render path
#     this harness drives).
#
# Combined with 0xb80000's own documented 2:1 decimation ("Voices [V]":
# "`0xb80000` (state +0x104) decimates 2:1 to 32 outputs"), the DO-64
# interpolation domain runs at 96 kHz and this harness's own decimated
# output (decimate()/write_wav(), WriteWavTest's 48000 default) is 48 kHz --
# consistent, not a coincidence: write_wav()'s ``sample_rate`` default was
# already 48000 before this lane's task 1, it was just never derived from
# this constant. See main()'s own CLI semantics fix (task 1, 2026-09-25)
# for why this constant exists: the previous CLI generated the source PCM
# at ``pitch_step``-prescaled cycles/sample instead of at this fixed rate,
# so its ``--freq`` argument did not mean "the source is a --freq Hz tone"
# at all -- see main()'s own comment for the corrected math and the
# measured before/after.
SOURCE_SAMPLE_RATE = 96000.0


def voice_record_address(image: str, voice: int) -> int:
    if not 0 <= voice < VOICE_RECORD_COUNT:
        raise ValueError("voice must be 0..%d" % (VOICE_RECORD_COUNT - 1))
    return profile(image).voice_records + voice * VOICE_RECORD_STRIDE
