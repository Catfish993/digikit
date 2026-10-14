"""Correctness check against recorded voice state."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from tools.sharc_dev import sharc_trace as st

from .audio import decimate, reference_render
from .layout import FIELD_ACTIVE, FIELD_PHASE, Q31, VOICE_RECORD_STRIDE
from .runner import InitResult, _read_byte, _read_q31_pair, new_runner
from .voice import (
    _dequantize_sample,
    _write_samples,
    call_render,
    read_coeff_table,
    setup_voice,
)


def _make_test_input(
    kind: str, length: int, *, freq_cycles_per_sample: float = 1.0 / 32
) -> list[float]:
    """A synthetic, bounded-amplitude input for ``check_correctness``.
    ``kind`` is "sine" (the same generator the previous version of this
    module used) or "ramp" (a periodic sawtooth at the same period, in
    [-1, 1)) -- a discontinuous waveform the 6-tap interpolator's frequency
    response treats very differently from a sine, so agreement on both is
    a much stronger cross-check than either alone."""
    if kind == "sine":
        return [
            math.sin(2 * math.pi * freq_cycles_per_sample * i) for i in range(length)
        ]
    if kind == "ramp":
        period = max(2, round(1.0 / freq_cycles_per_sample))
        return [2.0 * ((i % period) / period) - 1.0 for i in range(length)]
    raise ValueError("unknown check_correctness input kind: %r" % (kind,))


# (input kind, pitch_step) cases check_correctness() runs by default: unity
# speed, one downward and one upward pitch step (both exact powers of 2, so
# every phase lands on exactly 0 -- coeff row 0 only), one non-power-of-2
# step (0.75, added this lane, 2026-09-25 -- exercises every coeff row a
# quarter-integer step touches, not just row 0) on the sine, and unity on
# the ramp (a discontinuous waveform the 6-tap filter treats very
# differently from a sine).
_DEFAULT_CASES: tuple[tuple[str, float], ...] = (
    ("sine", 1.0),
    ("sine", 0.5),
    ("sine", 2.0),
    ("sine", 0.75),
    ("ramp", 1.0),
)


def _read_record_words(state, record: int) -> list[int]:
    """The full 0x1D8-byte voice record as 32-bit words (VOICE_RECORD_STRIDE
    is word-aligned: 0x1D8 / 4 = 118 exactly), for ``check_correctness()``'s
    per-block before/after diagnostic."""
    words = []
    for offset in range(0, VOICE_RECORD_STRIDE, 4):
        raw = st._dm_read(state, record + offset, 4)
        words.append(raw.value & 0xFFFFFFFF if raw is not None else 0)
    return words


def _diff_record_words(
    before: Sequence[int], after: Sequence[int]
) -> list[dict[str, int]]:
    """Word offsets (bytes, from the record base) where AFTER differs from
    BEFORE -- word, not byte, granularity (coarser than the named FIELD_*
    constants), but enough to see which part of the record one block's
    render actually touched without guessing which field it must have
    been."""
    return [
        {"offset": i * 4, "before": b, "after": a}
        for i, (b, a) in enumerate(zip(before, after, strict=True))
        if b != a
    ]


def check_correctness(
    memory,
    image: str,
    *,
    n_blocks: int = 8,
    cases: Sequence[tuple[str, float]] = _DEFAULT_CASES,
    freq_cycles_per_sample: float = 1.0 / 32,
    sample_format: str = "int16",
    init: InitResult | None = None,
) -> dict[str, dict]:
    """For each (input kind, pitch_step) in ``cases``, render ``n_blocks``
    consecutive 32-sample blocks from one voice and compare each against
    ``reference_render()`` run over the same input with its own phase
    accumulator carried forward the same way the firmware's FIELD_PHASE
    record field persists across ``call_render()`` calls -- so this checks
    both per-block numerical accuracy and block-to-block continuity (phase
    advancing by pitch_step*64 per block, matching the DO 64 loop's 64
    raw interpolation points; ACTIVE staying 1 with real sample data and a
    non-null FIELD_SAMPLE_PTR).

    ``init``, if given (an ``InitResult`` from ``run_init()``), starts every
    case from an independent copy of init's own post-return state (see
    ``new_runner``) instead of a bare one -- each case gets its own copy,
    so one is never affected by another's writes.

    Returns one dict per case, keyed "<kind>_step<pitch_step>", including a
    "record_diffs" list (one entry per block: the word offsets that block's
    render changed in the record, via ``_diff_record_words``) for seeing
    where a field such as ACTIVE actually gets written without re-deriving
    it from the ``active``/``phase`` summaries alone.

    **Sizing the buffer past deactivation, not just past the tap window.**
    FUN_1c4f81's past-limit test (this lane's task brief's static facts;
    FIELD_SAMPLE_LENGTH's own docstring) clears ACTIVE up to about one
    whole block *early*: empirically (this lane's own bisection, four
    pitch_step values), the render needs roughly ``pitch_step * 64 + 4``
    samples of buffer *past* the last one a block actually reads, not the
    fixed +32 tap-window pad alone -- a fixed pad sized for the slowest
    case (0.5x) silently deactivated the last of 8 blocks at 1x and 2x
    (this module's own test asserts against that regression). This sizes
    for one whole extra block at this case's own pitch_step, which covers
    the measured minimum with room to spare, plus the same tap-window pad
    as before.
    """
    sample_base = 0x310000
    coeff_table = None  # loaded once cases start, after profile() resolves
    results: dict[str, dict] = {}
    for kind, pitch_step in cases:
        sample_len = int(math.ceil((n_blocks + 1) * 64 * pitch_step)) + 32
        samples = _make_test_input(
            kind, sample_len, freq_cycles_per_sample=freq_cycles_per_sample
        )
        if coeff_table is None:
            coeff_table = read_coeff_table(memory, image)
        # reference_render() must compare against the same quantized values
        # the emulated render actually reads back (see
        # _dequantize_sample()'s docstring), not the ideal float input --
        # otherwise sample_format="int16"'s own Q15 rounding would show up
        # as "error" indistinguishable from a real interpolation mismatch.
        reference_samples = [_dequantize_sample(s, sample_format) for s in samples]

        runner = new_runner(memory, image, init=init)
        state = runner.state
        _write_samples(state, sample_base, samples, sample_format)
        record = setup_voice(
            state,
            image,
            0,
            sample_len=sample_len,
            pitch_step=pitch_step,
            sample_base=sample_base,
        )

        pos = 0.0
        halts, per_block_max, per_block_mean = [], [], []
        phases, actives = [], []
        record_diffs: list[list[dict[str, int]]] = []
        flat_errors: list[float] = []
        for _ in range(n_blocks):
            before_words = _read_record_words(state, record)
            result, rendered = call_render(runner, image, record)
            after_words = _read_record_words(state, record)
            record_diffs.append(_diff_record_words(before_words, after_words))
            decimated = decimate(rendered)
            reference = reference_render(
                reference_samples,
                coeff_table,
                step=pitch_step,
                n_output=len(decimated),
                start_phase=pos,
            )
            pos += pitch_step * 2 * len(decimated)  # = pitch_step * 64

            errors = [abs(a - b) for a, b in zip(decimated, reference, strict=True)]
            flat_errors.extend(errors)
            per_block_max.append(max(errors) if errors else None)
            per_block_mean.append(sum(errors) / len(errors) if errors else None)
            halts.append(result.halt.reason)
            phases.append(_read_q31_pair(state, record + FIELD_PHASE) / Q31)
            actives.append(_read_byte(state, record + FIELD_ACTIVE))

        phase_deltas = [b - a for a, b in itertools.pairwise(phases)]
        results["%s_step%g" % (kind, pitch_step)] = {
            "kind": kind,
            "pitch_step": pitch_step,
            "n_blocks": n_blocks,
            "halts": halts,
            "max_abs_error": max(flat_errors) if flat_errors else None,
            "mean_abs_error": sum(flat_errors) / len(flat_errors)
            if flat_errors
            else None,
            "per_block_max_error": per_block_max,
            "per_block_mean_error": per_block_mean,
            "phase": phases,
            "phase_deltas": phase_deltas,
            "phase_delta_expected": pitch_step * 64,
            "active": actives,
            "record_diffs": record_diffs,
        }
    return results


# Task 1's --sample-len auto-sizing, for main()'s CLI only. This is the same
# formula check_correctness() computes inline for each of its own cases
# (see that function's "Sizing the buffer past deactivation" docstring:
# FUN_1c4f81's past-limit test clears ACTIVE about one whole block *early*,
# empirically needing ~pitch_step*64+4 samples of buffer past the last one
# a block actually reads, not just the +32 tap-window pad) -- duplicated
# here rather than factored into a shared helper check_correctness() itself
# calls, so this lane's task 1 CLI change does not touch check_correctness()
# (kept unchanged per this lane's own task brief).
def _default_sample_len(n_blocks: int, pitch_step: float) -> int:
    """Auto-size a --blocks/--pitch-step render's source buffer with one
    whole block's look-ahead margin, so a long render does not run off the
    end of its own synthetic sample (task 1)."""
    return int(math.ceil((n_blocks + 1) * 64 * pitch_step)) + 32
