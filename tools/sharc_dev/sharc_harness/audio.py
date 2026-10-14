"""Tone measurement, WAV writing and reference rendering helpers."""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Sequence


def measure_tone(
    samples: Sequence[float], freq_hz: float, sample_rate: int, *, discard: int = 0
) -> dict:
    """This module's own direct-correlation tone measurement (the same
    technique `tests/sharc/integration/test_sharc_harness_cli.py`'s `CliFrequencyTest` uses):
    correlating SAMPLES against cos/sin at exactly `freq_hz` reconstructs
    that frequency's power with no spectral leakage regardless of whether
    the window holds a whole number of cycles (unlike an FFT bin readout),
    so this works on a short, non-coherently-sampled render.

    Returns `{"freq_hz", "power_ratio" (fundamental / total power, 1.0 =
    pure tone), "rms_dbfs", "peak_dbfs"}` -- both dBFS figures are relative
    to `1.0` (this module's own float/Q31 full-scale convention), `-inf`
    reported as `None` for a silent input."""
    values = list(samples[discard:])
    n = len(values)
    if n == 0:
        return {
            "freq_hz": freq_hz,
            "power_ratio": 0.0,
            "rms_dbfs": None,
            "peak_dbfs": None,
        }
    mean = sum(values) / n
    total_power = sum((v - mean) ** 2 for v in values) / n
    a_cos = sum(
        values[i] * math.cos(2 * math.pi * freq_hz * i / sample_rate) for i in range(n)
    ) * (2.0 / n)
    a_sin = sum(
        values[i] * math.sin(2 * math.pi * freq_hz * i / sample_rate) for i in range(n)
    ) * (2.0 / n)
    amplitude = math.hypot(a_cos, a_sin)
    fund_power = amplitude * amplitude / 2.0
    rms = math.sqrt(sum(v * v for v in values) / n)
    peak = max((abs(v) for v in values), default=0.0)
    return {
        "freq_hz": freq_hz,
        "power_ratio": fund_power / total_power if total_power else 0.0,
        "rms_dbfs": 20 * math.log10(rms) if rms > 0 else None,
        "peak_dbfs": 20 * math.log10(peak) if peak > 0 else None,
    }


def _thdn_dbc(power_ratio: float) -> float | None:
    """THD+N relative to the carrier (dBc), derived from
    `measure_tone()`'s own `power_ratio` (fundamental power / total power):
    THD+N power ratio = (total - fundamental) / fundamental =
    1/power_ratio - 1, expressed in dB. `None` for a silent or a perfectly
    pure (`power_ratio == 1.0`) signal (the latter -> -inf, not a finite
    number worth reporting)."""
    if power_ratio <= 0.0 or power_ratio >= 1.0:
        return None
    return 10 * math.log10((1.0 - power_ratio) / power_ratio)


def flatten(blocks: Sequence[Sequence[float]]) -> list[float]:
    return [sample for block in blocks for sample in block]


def decimate(work_buffer: Sequence[float]) -> list[float]:
    """The 64-float interpolator work buffer, decimated 2:1 by pairwise
    average (docs/findings/06: "0xb80000 reads 64, writes 32, x0.5") --
    the 32-sample block this module's WAV output actually is."""
    return [
        0.5 * (work_buffer[2 * i] + work_buffer[2 * i + 1])
        for i in range(len(work_buffer) // 2)
    ]


def write_wav(path: str, samples: Sequence[float], sample_rate: int = 48000) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        clipped = (max(-1.0, min(1.0, s)) for s in samples)
        wav.writeframes(
            b"".join(struct.pack("<h", int(round(s * 32767))) for s in clipped)
        )


def reference_render(
    input_samples: Sequence[float],
    coeff_table: Sequence[Sequence[float]],
    step: float,
    n_output: int,
    *,
    start_phase: float = 0.0,
) -> list[float]:
    """A 6-tap polyphase interpolator (input_samples, indexed by a
    fractional position advancing ``step`` samples per interpolated
    output, phase selecting a row of ``coeff_table``) followed by 2:1
    decimation (pairwise average -- docs/findings/06's "0xb80000 reads 64,
    writes 32, x0.5"), computed independently of tools/sharc_dev/sharc_core so it is
    a real cross-check rather than the same code re-run.

    **Forward taps, not centered (this lane's own finding, 2026-09-25,
    correcting an earlier ``base - half + 1 + t`` centered-window guess).**
    An impulse-response test against a concrete run of FUN_1c4f81's DO 64
    loop (single nonzero raw sample, unity pitch_step, six placements of
    the impulse relative to ``base`` swept one at a time) shows tap ``t``
    (0-indexed in the same order the loop reads them: the DO loop's first
    sample read pairs with ``coeff_table[phase][0]``, its last with
    ``coeff_table[phase][5]``) always lands on ``input_samples[base + t]``
    -- i.e. the filter is purely causal/forward from ``base``, never
    reaching behind it. Concretely: with the impulse at sample index K and
    ``base`` (the loop's own integer position, sw 0x1c50a4-0x1c50af's
    ``I4 = I0; modify(I4, M3)`` with M3 = floor(position)) swept from K-5
    to K, the nonzero output at each step reproduced
    ``coeff_table[0][K - base]`` (times the sample's own Q15 value) to
    within fixed-point rounding, for every one of the 6 tap positions --
    see ``read_coeff_table()``'s own docstring for the same trace this
    reused to fix the coefficient byte offset. See
    ``/private/tmp/claude-501/-Users-em-src-digi-digitakt2/89c5e6c8-2a58-496e-84a0-527f96e6f55a/scratchpad/trace_impulse2.py``.
    """
    phases = len(coeff_table)
    taps = len(coeff_table[0]) if coeff_table else 6

    def sample_at(index: int) -> float:
        if 0 <= index < len(input_samples):
            return input_samples[index]
        return 0.0

    interpolated = []
    pos = start_phase
    for _ in range(n_output * 2):
        base = int(math.floor(pos))
        frac = pos - base
        phase = int(frac * phases) % phases
        acc = 0.0
        for t in range(taps):
            acc += coeff_table[phase][t] * sample_at(base + t)
        interpolated.append(acc)
        pos += step
    return [
        0.5 * (interpolated[2 * i] + interpolated[2 * i + 1]) for i in range(n_output)
    ]
