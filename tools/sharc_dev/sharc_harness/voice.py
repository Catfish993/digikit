"""Voice setup, sample writing and block rendering."""

from __future__ import annotations

import struct
from collections.abc import Sequence

from tools.sharc_dev import sharc_run as sr
from tools.sharc_dev import sharc_trace as st

from .layout import (
    COEFF_TABLE_STRIDE_BYTES,
    COEFF_TABLE_TAPS,
    FIELD_ACTIVE,
    FIELD_END,
    FIELD_FADE_IN,
    FIELD_LOOP,
    FIELD_LOOP_START,
    FIELD_PHASE,
    FIELD_PREVIOUS_SAMPLE,
    FIELD_RESEED,
    FIELD_REVERSE,
    FIELD_SAMPLE_LENGTH,
    FIELD_SAMPLE_LENGTH_HI,
    FIELD_SAMPLE_PTR,
    FIELD_SEED_PENDING,
    FIELD_START,
    FIELD_STEP,
    FIELD_WORK_BUFFER,
    FIELD_ZERO_CROSS_MUTE,
    Q31,
    SESSION_TICK_ADDR,
    VOICE_RECORD_COUNT,
    profile,
    voice_record_address,
)
from .runner import InitResult, _poke, _read_byte, _write_q31_pair, new_runner

# Bytes between successive encoded samples in DM, by sample_format --
# *not* the same as the number of bytes this lane actually writes per
# sample (see ``_SAMPLE_WRITE_WIDTH_BYTES``).
#
# **Corrected (2026-09-25) after tools/sharc_dev/sharc_core/forms_dag.py's Type 7a
# MODIFY fix (w/l honoured, w=bit39, l=bit23) landed**: every MODIFY in
# this loop is (sw) (see the module docstring's wave-2 note), so
# sw 0x1c50af's ``modify(I4, M3)`` now steps I4 in *short-word* (2-byte)
# units, not 4. A concrete, register-level single-step of FUN_1c4f81's DO
# 64 loop against the fixed decoder (this lane's own scratch trace) shows
# I4 landing at 0x310000, 0x310002, 0x310004, ... for successive raw-tap
# reads within one iteration -- packed int16 PCM, two bytes apart, not one
# per 4-byte slot. The previous "4 bytes apart" claim on file here was
# measured *before* the MODIFY fix, when the (sw)/(nw) distinction was not
# yet honoured and every MODIFY silently scaled as if raw-byte, landing 4
# bytes apart by coincidence (still a smoothly-varying, plausible-looking
# int16 run -- not garbage -- which is why the wrong stride survived
# uncaught). It also now matches ``read_coeff_table()``'s own
# already-validated convention for the polyphase table stride (24
# bytes/phase, 6 taps): that address math goes through the *byte-addressed*
# ``memory.read()`` path directly, never through a (sw) MODIFY, so it was
# never affected by this bug either way.
_SAMPLE_STRIDE_BYTES = {"float32": 4, "int16": 2}
# Bytes actually written at each sample's own address: "int16" writes only
# the low 2 bytes of its 4-byte slot (a plain short-word poke); the slot's
# high 2 bytes are left unwritten (explicit_memory_model: reads back 0),
# matching what a short-word-sign-extended *read* of that same address
# looks at -- the DO 64 loop's own reads never touch the high half.
_SAMPLE_WRITE_WIDTH_BYTES = {"float32": 4, "int16": 2}


def _sample_to_word(value: float, sample_format: str) -> int:
    """Encode one synthetic sample as the raw DM word this harness writes
    at ``sample_base`` -- what FUN_1c4f81's DO 64 loop reads through
    record+0 as PCM (see ``_SAMPLE_STRIDE_BYTES``'s docstring for the
    address-space reasoning).

    ``"int16"`` (this lane's own finding, now the default -- see
    ``_SAMPLE_STRIDE_BYTES``) packs VALUE as a signed Q15 fixed-point word
    (round(value * 32768), clamped to the int16 range, masked to 16 bits):
    the same (swse) int16 / 2**15 convention ``read_coeff_table()`` already
    uses for the polyphase taps (docs/findings/06's "Render and declick"),
    which this lane's static facts also give for the raw sample reads
    themselves ("short-word sign-extended (int16)", sw 0x1c50b4 etc).

    Confirmed empirically, not just by the decode-table type name: with
    ``"float32"`` (this harness's previous default) at a 4-byte stride, a
    rendered block's values had no resemblance to the input sine at all.
    Switching the encoding alone to this Q15 int16 packing was necessary
    but not sufficient: a large mismatch against ``reference_render()``
    remained even after this fix and ``read_coeff_table(phases=256)`` --
    tracked down (this lane, 2026-09-25) to ``read_coeff_table()`` itself
    reading the wrong half of each 4-byte coefficient slot (see its own
    docstring), not to ``tools/sharc_dev/sharc_core``'s MR-accumulator housekeeping
    or ``float_by``, both of which a register-level trace confirmed
    correct (``alu_float_convert_scaled``'s rx/ry operand order matches the
    raw instruction bytes exactly at sw 0x1c50e3 -- rn=2, rx=2, ry=6 --
    contradicting an earlier lane's manual byte decode that had rx and ry
    swapped; no fix was needed there).

    ``"float32"`` packs VALUE as an IEEE-754 float word instead -- kept
    only for an explicit before/after comparison (see the module's tests),
    not because any evidence supports it as the real format.
    """
    if sample_format == "float32":
        return struct.unpack("<I", struct.pack("<f", value))[0]
    if sample_format == "int16":
        quantized = max(-32768, min(32767, int(round(value * 32768.0))))
        return quantized & 0xFFFF
    raise ValueError("unknown sample_format %r" % (sample_format,))


def _dequantize_sample(value: float, sample_format: str) -> float:
    """The float value actually stored (and later read back) after
    ``_sample_to_word(value, sample_format)`` -- the inverse quantization,
    used to build ``reference_render()``'s input from exactly what the
    emulated read will see, so ``check_correctness()`` measures
    interpolation-math error rather than format-quantization noise on top
    of it. ``"float32"`` round-trips through the same IEEE-754 packing
    (a no-op for any value already representable, which every
    ``_make_test_input()`` value is)."""
    word = _sample_to_word(value, sample_format)
    if sample_format == "float32":
        return struct.unpack("<f", struct.pack("<I", word))[0]
    if sample_format == "int16":
        signed = word - 0x10000 if word & 0x8000 else word
        return signed / 32768.0
    raise ValueError("unknown sample_format %r" % (sample_format,))


def _write_samples(
    state, base: int, values: Sequence[float], sample_format: str = "int16"
) -> None:
    stride = _SAMPLE_STRIDE_BYTES[sample_format]
    width = _SAMPLE_WRITE_WIDTH_BYTES[sample_format]
    for i, value in enumerate(values):
        _poke(
            state, base + i * stride, _sample_to_word(value, sample_format), width=width
        )


def setup_voice(
    state,
    image: str,
    voice: int,
    *,
    sample_len: int,
    pitch_step: float = 1.0,
    start: int = 0,
    end: int | None = None,
    loop_start: int = 0,
    loop: bool = False,
    reverse: bool = False,
    sample_base: int = 0x310000,
) -> int:
    """Write one voice record's fields per docs/findings/06's voice record
    contract (plus FIELD_SAMPLE_PTR, this lane's own finding -- see this
    module's docstring), and return its address. ``pitch_step`` is in input
    samples per output (pre-interpolation) step; 1.0 is unity speed.
    ``sample_base`` is the raw-PCM address stored at record+0 -- the caller
    is responsible for having written samples there (``call_render`` no
    longer pokes a register for this; see the docstring's "Corrected voice
    call convention").

    This writes the net effect finding 06's "Flags and seed" documents for
    a genuine trigger (the arm ``0x1c4eaf`` -- ACTIVE=1, SEED_PENDING=1 --
    immediately followed by one of the four position/step setters, which
    tests SEED_PENDING, clears it, and seeds PHASE to ``start`` or
    ``end - 1`` sample when REVERSE is set): ACTIVE, SEED_PENDING (ends
    cleared), REVERSE, LOOP, STEP, START/END/LOOP_START and PHASE are all
    set here from a documented writer. ``loop``/``reverse`` default to
    False/False, matching what an unset (explicit_memory_model) record
    would read as -- pass them explicitly once a real trigger's argument
    values are known.

    Fields this harness still has no documented trigger-path writer for --
    FIELD_SAMPLE_PTR (finding 06: "[O] the firmware writer of word +0 for a
    playing voice is not yet found"), FIELD_SAMPLE_LENGTH (this lane's own
    finding -- see its docstring), fade-in/zero-cross-mute/reseed (the
    contract table marks their writer "not checked"), and previous-sample
    (documented writer is the render function ``0x1c4f81`` itself, not a
    setter) -- are still poked here to a fixed value (sample_base; sample_len;
    0; 0) rather than left to whatever ``run_init``'s own FUN_1c7442 wrote,
    so a render from init state is comparable to one from bare state
    instead of differing for reasons unrelated to the trigger. This is a
    deliberate hand-poke, not a claim about what the real trigger writes
    there.
    """
    record = voice_record_address(image, voice)
    end = sample_len if end is None else end
    _poke(state, record + FIELD_SAMPLE_PTR, sample_base)
    _poke(state, record + FIELD_SAMPLE_LENGTH, sample_len)
    _poke(state, record + FIELD_SAMPLE_LENGTH_HI, 0)
    _poke(state, record + FIELD_ACTIVE, 1, width=1)
    _poke(state, record + FIELD_SEED_PENDING, 0, width=1)
    _poke(state, record + FIELD_REVERSE, 1 if reverse else 0, width=1)
    _poke(state, record + FIELD_LOOP, 1 if loop else 0, width=1)
    _poke(state, record + FIELD_FADE_IN, 0, width=1)
    _poke(state, record + FIELD_ZERO_CROSS_MUTE, 0, width=1)
    _poke(state, record + FIELD_RESEED, 0, width=1)
    _write_q31_pair(state, record + FIELD_LOOP_START, loop_start << 31)
    _write_q31_pair(state, record + FIELD_START, start << 31)
    _write_q31_pair(state, record + FIELD_END, end << 31)
    _write_q31_pair(state, record + FIELD_STEP, round(pitch_step * Q31))
    _write_q31_pair(
        state, record + FIELD_PHASE, ((end - 1) if reverse else start) << 31
    )
    _poke(state, record + FIELD_PREVIOUS_SAMPLE, 0)
    return record


def call_render(
    runner: sr.Runner, image: str, record: int
) -> tuple[sr.RunResult, list[float]]:
    """Call FUN_1c4ecf the way FUN_1c642a's dispatch loop calls it -- R4 =
    the record address itself, not record+4 (see this module's docstring's
    "Corrected voice call convention") -- on RUNNER's already-set-up State
    (see ``new_runner``/``setup_voice``), and return (RunResult, the 64
    work-buffer floats).

    Resets RUNNER's control-flow state (pc_sw, call stack, loops, pending
    delay slot) as a fresh call needs, but deliberately keeps its DM
    overlay (the voice record ``setup_voice`` wrote) and every other
    register (the callee saves and restores the ones it uses -- see this
    module's docstring's trace of FUN_1c4ecf's own prologue/epilogue), so
    calling this again for the next block is a closer match to what
    FUN_1c642a's own dispatch loop does than starting over from
    ``new_runner`` would be.

    The raw sample pointer is not a register here (see ``setup_voice``'s
    ``sample_base``): it is read by the callee from record+0
    (FIELD_SAMPLE_PTR), a persistent record field, not reset per call.

    R4 is the record address (the real call convention -- see this
    module's docstring). R12 is the real caller's second argument too (see
    ``SESSION_TICK_ADDR``'s docstring): read fresh from DM every call
    (never advanced by this harness -- an open point, not a claim that a
    real multi-block run holds it fixed) instead of left at whatever
    register value the previous run (``run_init()`` or a prior
    ``call_render()``) happened to leave R12 at.

    **Loop-wrap ACTIVE (task 3, 2026-09-25; fixed at the source by lane
    W1, 2026-09-25).** docs/findings/06 claims "a wrap copies +0x1bc
    [FIELD_LOOP] to +0x1b8 [FIELD_ACTIVE]" (0x1c50fe forward, 0x1c530a/
    0x1c5325 reverse). The wrap's read at 0x1c50fb/0x1c5322
    (``R2 = DM(I1, M6)``, a Type3d instruction, fields l=0, x=0, w=0,
    ex=0) used to be misdecoded by ``tools/sharc_dev/sharc_core/forms_move.py``'s
    ``_type_3d``, which hardcoded ``access_width = "normal-word"`` for
    its w=0/ex=0 branch instead of consulting ``ACCESS_WIDTHS[(l, x, 0)]``
    the way the sibling Type4d store at the very same site already did --
    see that function's own docstring for the PRM citation and the
    register-override confirmation. With that fixed, this run's own
    Type3d load now performs the correct 1-byte read, and ACTIVE tracks
    FIELD_LOOP across a wrap on its own, with no harness-side fixup
    needed; no ``call_render()`` code doctors ACTIVE any more.
    """
    from tools.sharc_dev.sharc_core.encoding import UREG_CODES

    state = runner.state
    state.pc_sw = profile(image).voice_render
    state.stopped = None
    state.pending = None
    state.call_stack = []
    state.loops = []
    state.status_stack = []
    state.at_loaded_entry = False
    tick = st._dm_read(state, SESSION_TICK_ADDR, 4)
    state.uregs[UREG_CODES["R4"]] = st.Const(record)
    state.uregs[UREG_CODES["R12"]] = st.Const(
        tick.value & 0xFFFFFFFF if tick is not None else 0
    )
    runner.instructions = 0
    runner.form_counts.clear()
    runner.max_call_depth_reached = 0

    result = runner.run(max_steps=20_000)
    floats = []
    for i in range(64):
        raw = st._dm_read(state, record + FIELD_WORK_BUFFER + i * 4, 4)
        floats.append(
            struct.unpack("<f", struct.pack("<I", raw.value & 0xFFFFFFFF))[0]
            if raw is not None
            else 0.0
        )

    return result, floats


def render_blocks(
    memory,
    image: str,
    voice: int,
    n_blocks: int,
    *,
    pitch_step: float = 1.0,
    sample_len: int = 4096,
    sample_base: int = 0x310000,
    samples: Sequence[float] | None = None,
    sample_format: str = "int16",
    init: InitResult | None = None,
) -> tuple[list[list[float]], list[sr.RunResult]]:
    """N call_render() calls against one freshly set-up voice record,
    sharing a single Runner/State (see ``new_runner``/``call_render``) so
    the record's own persistent fields (phase, previous sample, ...) carry
    over between calls the way they would across real render-loop
    invocations -- see ``check_correctness()`` for this measured against an
    independent reference, including phase continuity across blocks.

    ``samples``, if given, is written to ``sample_base`` before the first
    call (FIELD_SAMPLE_PTR then points at it), e.g. a synthetic sine for an
    audible WAV, encoded per ``sample_format`` (see ``_sample_to_word``). If
    omitted, the record still points at ``sample_base`` but nothing is
    written there, so unwritten DM reads as 0 (State.explicit_memory_model):
    a silent (but not zero-fill-gated, since FIELD_SAMPLE_PTR is still
    non-null) render, useful for an instructions/block measurement alone.

    ``init``, if given (an ``InitResult`` from ``run_init()``), starts the
    render from a copy of init's own post-return state instead of a bare
    one -- see ``new_runner``.
    """
    runner = new_runner(memory, image, init=init)
    state = runner.state
    if samples is not None:
        _write_samples(state, sample_base, samples, sample_format)
    record = setup_voice(
        state,
        image,
        voice,
        sample_len=sample_len,
        pitch_step=pitch_step,
        sample_base=sample_base,
    )
    blocks, results = [], []
    for _ in range(n_blocks):
        result, floats = call_render(runner, image, record)
        blocks.append(floats)
        results.append(result)
    return blocks, results


def scan_voice_active(
    state, image: str, *, exclude: Sequence[int] = ()
) -> dict[int, int]:
    """The ACTIVE byte (`FIELD_ACTIVE`, record `+0x1b8`) of every voice
    record except `exclude`, keyed by voice index -- for telling a
    hand-set-up voice (this harness's own `setup_voice()`) apart from one
    the firmware itself activated while a replay ran. Returns only entries
    that read non-zero; an empty dict means no *other* voice was ever
    marked ACTIVE."""
    active = {}
    for voice in range(VOICE_RECORD_COUNT):
        if voice in exclude:
            continue
        record = voice_record_address(image, voice)
        value = _read_byte(state, record + FIELD_ACTIVE)
        if value:
            active[voice] = value
    return active


# --- reference 6-tap polyphase interpolator + 2:1 decimator ----------------


def read_coeff_table(memory, image: str, phases: int = 256) -> list[list[float]]:
    """The firmware's own polyphase coefficients (docs/findings/06's
    "Render and declick": 6 taps per phase, one per 4-byte slot, 24
    bytes/phase), as float taps (signed 32-bit fraction, value / 2**31 --
    see this lane's own finding below, correcting the previous "int16 low
    half" and "int16 high half" guesses).

    ``phases=256`` is this lane's own finding, correcting the previous
    128-phase assumption (docs/findings/06 marks the phase count "[O]";
    128 was a guess "consistent with the 128-entry tables found elsewhere
    in the render chain", not evidence about this table specifically).
    Two independent checks: (1) a concrete, register-level single-step of
    FUN_1c4f81's DO 64 loop reads the coefficient pointer I5 right after
    sw 0x1c50b6's ``modify(I5, M2)`` and finds ``(I5 - coeff_table) / 24``
    (the row this function would read) landing exactly on
    ``int(fractional_phase * 256) % 256`` for every iteration checked
    (multiple non-power-of-2 pitch_step values) -- not the "% 128" a
    128-phase table would need, and not merely double by coincidence: rows
    computed this way went as high as 230, past any 128-row table
    entirely. (2) independent of any addressing question at all, the raw
    bytes at phase index 128+k equal phase index (128-k)'s six taps in
    *reverse* (e.g. row 128 == reversed(row 120), row 127 == reversed(row
    121)) -- the textbook mirror-image second half of a linear-phase
    polyphase filter bank, and not a pattern a wrong stride into unrelated
    data would produce by chance.

    **Each 4-byte slot is one 32-bit Q31 tap, not a 16-bit tap in either
    half (this lane's own finding, 2026-09-25, correcting an intermediate
    "upper 16 bits" guess made while chasing the same evidence).** Both
    16-bit halves of every slot independently satisfy check (2)'s mirror
    property (a 32-bit-symmetric table is trivially symmetric in each half
    separately, and so is the full 32-bit value), so that check alone
    cannot tell a 16-bit-slice reading from the true 32-bit one -- it is
    necessary but not sufficient. What actually distinguishes them: a
    register-level trace of FUN_1c4f81's DO 64 loop's MAC chain
    (``tools/sharc_dev/sharc_core/compute_mult.py``'s ``_mr_product_raw``/
    ``_mr_extract32``, already correct and unchanged by this fix) computing
    ``(sample_raw * coeff_slot_raw32 << 1) >> 32`` and comparing that to
    the emulator's own concrete SAT-MRF result for a one-tap-nonzero
    impulse case: using the coefficient slot's *full* 32-bit raw value
    (e.g. ``0x31f09d09`` = 837852425 for phase 0/tap 2) as the multiplier
    input reproduces the emulator's exact SAT-MRF integer (6392, from
    sample_raw=16384) bit-for-bit; using only the slot's upper 16 bits
    (12784, an earlier version of this finding) does not feed the real
    multiplier at all -- SHARC's ``Rn = MR (+-) Rx*Ry`` reads Rx/Ry as
    whole 32-bit registers, and ``R0 = DM(I5+2), long`` (sw 0x1c50c4) loads
    the *whole* slot into R0, not a truncated half. Treating that raw
    32-bit slot as a signed Q31 fraction (``/2**31``) then gives a *float*
    coefficient that, multiplied by the sample as a plain float product
    (this module's ``reference_render``), reproduces the fixed-point
    result to about 2e-5 -- consistent with the one real remaining gap:
    ``_mr_extract32``'s bits-63:32 extraction is a PRM-documented *floor*,
    not a round (p.3-10: "using bits 63-32 for a fractional result"), so a
    pure-float reference is architecturally unable to match it bit-exactly
    without reimplementing the same truncation. The earlier "upper 16
    bits" reading was closer to that *already-truncated* single-tap
    result by coincidence (discarding the coefficient's own low 16 bits
    happens to land near where the hardware's own floor lands for some
    but not all sample/coefficient magnitudes) but is off by 4-8x more
    than the Q31 reading once several taps sum together (this lane's own
    ``check_correctness()`` run: max error dropped from ~1.1e-4 to ~3e-5,
    the SAT floor's own ~1-ULP-of-Q15 floor this module's tests assert
    once this fix landed). See
    ``/private/tmp/claude-501/-Users-em-src-digi-digitakt2/89c5e6c8-2a58-496e-84a0-527f96e6f55a/scratchpad/verify_fixedpoint.py``
    and ``verify_q31.py`` for the traces this is drawn from. This was the
    residual this lane's task brief pointed at ("this lane's evidence
    points at tools/sharc_dev/sharc_core's MR-accumulator housekeeping ... and/or
    float_by ... not at this harness's own sample encoding or
    addressing") -- neither of those was actually at fault (both verified
    correct by direct trace); this function's own coefficient extraction
    was.
    """
    from tools.sharc_dev.sharcldr import SW_ALIAS_BASE

    base = profile(image).coeff_table
    table = []
    for phase in range(phases):
        taps = []
        for tap in range(COEFF_TABLE_TAPS):
            addr = SW_ALIAS_BASE + base + phase * COEFF_TABLE_STRIDE_BYTES + tap * 4
            raw = memory.read(addr, 4)
            value = 0 if raw is None else int.from_bytes(raw, "little", signed=True)
            taps.append(value / 2147483648.0)
        table.append(taps)
    return table
