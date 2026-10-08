#!/usr/bin/env python3
"""Features of a rendered run's audio, for scenario oracles (no firmware needed).

Pure functions on float64 arrays shaped (n, channels) plus a thin CLI. Each
detector is tested on synthetic signals in tests/test_audiocheck.py.

    basic, lr_diff       level, DC, non-finite, growth, L-R difference
    envelope, onsets     RMS envelope (dB) and transient onset times
    echoes, decay_time   click-train echo times/ratios; RT60 (Schroeder)
    levels               distinct values on a ramp, effective bit depth
    spectrum, peaks      Hann spectrum (dB, full-scale sine = 0) and its peaks
    alias_products       expected images of a sine held at a lower rate

    uv run python tools/audiocheck.py RUN_DIR_OR_WAV [--no-plots]

A directory means RUN_DIR/audio.wav. Writes features.json (and waveform.png,
envelope.png, spectrogram.png) next to the WAV and prints a short summary.
The coupled runner writes float32 stereo WAV at 48 kHz; a file that is not
.wav is read as raw f32le stereo at 48 kHz.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import sys
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray

F: TypeAlias = NDArray[np.float64]  # noqa: UP040 (mypy checks 3.11)
RATE = 48000


def load(path: str | pathlib.Path) -> tuple[int, F]:
    """Read audio as (sample rate in Hz, float64 array of shape (n, channels))."""
    p = pathlib.Path(path)
    data = p.read_bytes()
    if p.suffix.lower() != ".wav":
        n = len(data) // 8 * 2
        return RATE, np.frombuffer(data, "<f4", n).astype(np.float64).reshape(-1, 2)
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{p}: not a RIFF/WAVE file")
    fmt: tuple[int, int, int, int] | None = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt ":
            tag, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag == 0xFFFE:  # extensible: the real tag starts the sub-format
                tag = struct.unpack_from("<H", data, body + 24)[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError(f"{p}: data chunk before fmt chunk")
            if size == 0 or size > len(data) - body:  # header patched lazily
                size = len(data) - body
            return fmt[2], _decode(data[body : body + size], *fmt)
        pos = body + size + (size & 1)
    raise ValueError(f"{p}: no data chunk")


def _decode(raw: bytes, tag: int, ch: int, rate: int, bits: int) -> F:
    kinds = {(3, 32): "<f4", (3, 64): "<f8", (1, 16): "<i2", (1, 32): "<i4"}
    if (tag, bits) not in kinds:
        raise ValueError(f"unsupported WAV format tag {tag}, {bits} bits")
    dt = np.dtype(kinds[(tag, bits)])
    n = len(raw) // (dt.itemsize * ch) * ch
    x = np.frombuffer(raw, dt, n).astype(np.float64)
    if tag == 1:
        x /= float(1 << (bits - 1))
    return x.reshape(-1, ch)


def _db(v: float) -> float:
    """Amplitude ratio to dB; -inf for zero."""
    return 20 * math.log10(v) if v > 0 else -math.inf


def _mono(x: F) -> F:
    return x.mean(axis=1) if x.ndim == 2 else x


def basic(x: F, rate: int) -> dict[str, object]:
    """peak and rms (linear, full scale 1.0, over finite samples), dc (per-channel
    mean), nonfinite (count), growth (rms of the last 10% > 2x rms of the 40-60%
    middle and above -40 dBFS), duration_s."""
    ok = np.isfinite(x)
    y = np.where(ok, x, 0.0)
    n = len(y)

    def rms(a: F) -> float:
        return float(np.sqrt(np.mean(a**2))) if len(a) else 0.0

    tail, mid = rms(y[n - n // 10 :]), rms(y[n * 2 // 5 : n * 3 // 5])
    return {
        "duration_s": n / rate,
        "peak": float(np.abs(y).max()) if n else 0.0,
        "rms": rms(y),
        "dc": [float(v) for v in y.mean(axis=0)] if n else [],
        "nonfinite": int((~ok).sum()),
        "growth": bool(tail > 2 * mid and _db(tail) > -40),
    }


def lr_diff(x: F) -> tuple[float, float]:
    """(max |L-R| linear, rms(L-R) relative to rms(L) in dB); (0, -inf) if
    identical or the signal is not stereo."""
    if x.shape[1] < 2:
        return 0.0, -math.inf
    d = x[:, 0] - x[:, 1]
    num = float(np.sqrt(np.mean(d**2)))
    den = float(np.sqrt(np.mean(x[:, 0] ** 2)))
    rel = _db(num / den) if den > 0 else (-math.inf if num == 0 else math.inf)
    return float(np.abs(d).max()), rel


def envelope(x: F, rate: int, win_ms: float = 5.0) -> tuple[F, F]:
    """(window centre times in s, RMS level in dB re full scale) over the channel
    mean-square, in consecutive windows of win_ms."""
    w = max(1, round(rate * win_ms / 1000))
    ms = np.mean(np.atleast_2d(x.T) ** 2, axis=0)
    k = len(ms) // w
    e = ms[: k * w].reshape(k, w).mean(axis=1)
    return (np.arange(k) + 0.5) * w / rate, 10 * np.log10(e + 1e-24)


def onsets(
    x: F,
    rate: int,
    win_ms: float = 2.0,
    rise_db: float = 9.0,
    floor_db: float = 40.0,
    min_gap_s: float = 0.05,
) -> list[float]:
    """Times (s) where the envelope rises by more than rise_db within one window
    and ends within floor_db of the loudest window; successive onsets are at
    least min_gap_s apart."""
    t, e = envelope(x, rate, win_ms)
    if len(e) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(e)):
        if e[i] - e[i - 1] > rise_db and e[i] > e.max() - floor_db:
            ts = float(t[i] - win_ms / 2000)
            if not out or ts - out[-1] >= min_gap_s:
                out.append(ts)
    return out


def echoes(
    x: F, rate: int, floor_db: float = 50.0, min_gap_s: float = 0.02
) -> dict[str, object]:
    """Echoes of a click: times (s of each cluster's peak), spacing_s (median of
    diffs, None if under 2 echoes) and ratios_db (level of each echo relative to
    the previous one). Peaks are clusters of |x| above floor_db below the
    maximum, separated by more than min_gap_s, so echoes closer than that merge
    and echoes quieter than the floor are lost."""
    a = np.abs(_mono(x))
    if not len(a) or a.max() == 0:
        return {"times": [], "spacing_s": None, "ratios_db": []}
    idx = np.flatnonzero(a > a.max() * 10 ** (-floor_db / 20))
    cuts = np.flatnonzero(np.diff(idx) > min_gap_s * rate) + 1
    times, peaks = [], []
    for grp in np.split(idx, cuts):
        j = grp[np.argmax(a[grp])]
        times.append(float(j / rate))
        peaks.append(float(a[j]))
    d = np.diff(times)
    return {
        "times": times,
        "spacing_s": float(np.median(d)) if len(d) else None,
        "ratios_db": [_db(b / a_) for a_, b in zip(peaks, peaks[1:], strict=False)],
    }


def decay_time(x: F, rate: int, lo_db: float = -5.0, hi_db: float = -25.0) -> float:
    """RT60 in s: slope of the Schroeder backward-integrated energy between lo_db
    and hi_db, extrapolated to -60 dB. NaN if the decay never reaches hi_db."""
    e = _mono(x) ** 2
    s = np.cumsum(e[::-1])[::-1]
    if s[0] <= 0:
        return math.nan
    d = 10 * np.log10(s / s[0] + 1e-30)
    i0 = np.flatnonzero(d <= lo_db)
    i1 = np.flatnonzero(d <= hi_db)
    if not len(i0) or not len(i1) or i1[0] <= i0[0]:
        return math.nan
    seg = np.arange(i0[0], i1[0] + 1)
    slope = np.polyfit(seg / rate, d[seg], 1)[0]  # dB per second, negative
    return float(-60 / slope) if slope < 0 else math.nan


def levels(x: F) -> dict[str, float]:
    """For a slow ramp: n_levels (distinct values after rounding to float32),
    bits (log2 n_levels, effective bit depth) and step (median gap between
    adjacent distinct values, linear full scale 1.0)."""
    v = np.unique(_mono(x).astype(np.float32).astype(np.float64))
    n = len(v)
    return {
        "n_levels": n,
        "bits": math.log2(n) if n else 0.0,
        "step": float(np.median(np.diff(v))) if n > 1 else 0.0,
    }


def spectrum(x: F, rate: int) -> tuple[F, F]:
    """(frequencies in Hz, magnitude in dB) of the channel mean under a Hann
    window; a full-scale sine on a bin centre reads 0 dB."""
    m = _mono(x)
    w = np.hanning(len(m))
    mag = 2 * np.abs(np.fft.rfft(m * w)) / w.sum()
    return np.fft.rfftfreq(len(m), 1 / rate), 20 * np.log10(mag + 1e-12)


def peaks(spec: F, freqs: F, n: int = 5) -> list[tuple[float, float]]:
    """The n strongest local maxima of a dB spectrum as (Hz, dB), strongest first."""
    if len(spec) < 3:
        return []
    k = np.flatnonzero((spec[1:-1] > spec[:-2]) & (spec[1:-1] >= spec[2:])) + 1
    k = k[np.argsort(spec[k])[::-1][:n]]
    return [(float(freqs[i]), float(spec[i])) for i in k]


def alias_products(f0: float, srr_rate: float, rate: float = RATE) -> list[float]:
    """Frequencies (Hz, sorted, in 0..rate/2) of the images k*srr_rate +- f0
    (k >= 1) of a sine at f0 held at srr_rate, folded into the output band. The
    fundamental itself is left out."""
    out: set[float] = set()
    for k in range(1, int(2 * rate / srr_rate) + 2):
        for f in (k * srr_rate - f0, k * srr_rate + f0):
            f %= rate
            f = rate - f if f > rate / 2 else f
            if abs(f - f0) > 0.5:
                out.add(round(f, 3))
    return sorted(out)


def analyse(x: F, rate: int) -> dict[str, object]:
    """Always-on features, JSON-serialisable: basic, lr_diff (max_abs,
    rel_db or None when identical) and the envelope peak (dB, time in s)."""
    mx, rel = lr_diff(x)
    t, e = envelope(x, rate)
    k = int(np.argmax(e)) if len(e) else 0
    return {
        "rate": rate,
        "channels": int(x.shape[1]),
        "basic": basic(x, rate),
        "lr_diff": {"max_abs": mx, "rel_db": rel if math.isfinite(rel) else None},
        "envelope_peak": {
            "db": float(e[k]) if len(e) else None,
            "time_s": float(t[k]) if len(e) else None,
        },
    }


def plots(x: F, rate: int, out_dir: str | pathlib.Path) -> list[pathlib.Path]:
    """Write waveform.png, envelope.png and spectrogram.png into out_dir."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = pathlib.Path(out_dir)
    t = np.arange(len(x)) / rate
    paths = []

    def save(fig: object, name: str) -> None:
        p = out / name
        fig.savefig(p, dpi=100, bbox_inches="tight")  # type: ignore[attr-defined]
        plt.close(fig)
        paths.append(p)

    fig, ax = plt.subplots(figsize=(10, 3))
    for c in range(x.shape[1]):
        ax.plot(t, x[:, c], lw=0.5, label=f"ch{c}")
    ax.set(xlabel="s", ylabel="amplitude", title="waveform")
    ax.legend(loc="upper right")
    save(fig, "waveform.png")

    et, ev = envelope(x, rate)
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(et, ev, lw=0.8)
    ax.set(xlabel="s", ylabel="dB FS", title="RMS envelope (5 ms)")
    ax.grid(alpha=0.3)
    save(fig, "envelope.png")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.specgram(_mono(x), NFFT=2048, Fs=rate, noverlap=1536, cmap="magma")
    ax.set(xlabel="s", ylabel="Hz", title="spectrogram")
    save(fig, "spectrogram.png")
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="run directory (uses audio.wav) or a WAV file")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)
    p = pathlib.Path(a.target)
    wav = p / "audio.wav" if p.is_dir() else p
    rate, x = load(wav)
    feats = analyse(x, rate)
    (wav.parent / "features.json").write_text(json.dumps(feats, indent=2) + "\n")
    if not a.no_plots:
        plots(x, rate, wav.parent)
    b: dict = feats["basic"]  # type: ignore[assignment]
    ep: dict = feats["envelope_peak"]  # type: ignore[assignment]
    print(
        f"{wav}: {b['duration_s']:.2f} s, peak {b['peak']:.4f}, rms {b['rms']:.4f}, "
        f"nonfinite {b['nonfinite']}, growth {b['growth']}, "
        f"L-R {feats['lr_diff']['rel_db']} dB, env peak {ep['db']:.1f} dB "  # type: ignore[index]
        f"at {ep['time_s']:.3f} s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
