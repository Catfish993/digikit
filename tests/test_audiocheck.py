"""tools/audiocheck.py on synthetic signals with known answers."""

from __future__ import annotations

import math
import pathlib
import struct
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
try:
    import audiocheck as ac
finally:
    sys.path.pop(0)

R = 48000


def sine(f: float, secs: float = 1.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(R * secs)) / R
    return (amp * np.sin(2 * np.pi * f * t))[:, None].repeat(2, axis=1)


def test_lr_diff_identical_and_offset() -> None:
    x = sine(1000)
    assert ac.lr_diff(x) == (0.0, -math.inf)
    y = x.copy()
    y[:, 1] = np.roll(y[:, 1], 1)
    mx, rel = ac.lr_diff(y)
    assert mx > 0 and rel > -60


def test_dc_and_nonfinite() -> None:
    x = sine(1000) + 0.1
    assert ac.basic(x, R)["dc"] == pytest.approx([0.1, 0.1], abs=1e-6)
    x[10, 0] = np.nan
    x[20, 1] = np.inf
    b = ac.basic(x, R)
    assert b["nonfinite"] == 2 and math.isfinite(b["rms"])  # type: ignore[arg-type]


def test_growth() -> None:
    n = 2 * R
    rise = (np.linspace(0, 1, n) ** 2 * np.sin(np.arange(n) * 0.1))[:, None]
    assert ac.basic(rise, R)["growth"] is True
    assert ac.basic(sine(1000, 2.0), R)["growth"] is False


def test_envelope_and_onsets() -> None:
    x = np.zeros((R, 2))
    for at in (0.2, 0.5):
        i = int(at * R)
        x[i : i + 200] = 0.5
    on = ac.onsets(x, R)
    assert len(on) == 2 and on[0] == pytest.approx(0.2, abs=0.004)
    assert on[1] == pytest.approx(0.5, abs=0.004)
    t, e = ac.envelope(x, R)
    assert e.max() == pytest.approx(-6.8, abs=0.1) and t[np.argmax(e)] < 0.21


def test_echoes() -> None:
    x = np.zeros((2 * R, 2))
    for k in range(6):
        x[int((0.1 + 0.25 * k) * R)] = 0.8 * 10 ** (-6 * k / 20)
    r = ac.echoes(x, R)
    assert len(r["times"]) == 6  # type: ignore[arg-type]
    assert r["spacing_s"] == pytest.approx(0.25, abs=0.001)
    assert r["ratios_db"] == pytest.approx([-6.0] * 5, abs=0.5)


def test_decay_time() -> None:
    rng = np.random.default_rng(1)
    t = np.arange(3 * R) / R
    x = (rng.standard_normal(len(t)) * 10 ** (-3 * t / 1.2))[:, None]
    assert ac.decay_time(x, R) == pytest.approx(1.2, rel=0.1)


@pytest.mark.parametrize("bits", [4, 8, 12])
def test_levels(bits: int) -> None:
    ramp = np.linspace(-1, 1, 200000)
    q = 2 ** (bits - 1)
    x = (np.round(ramp * (q - 1)) / q)[:, None]
    r = ac.levels(x)
    assert r["bits"] == pytest.approx(bits, abs=0.5)
    assert r["step"] == pytest.approx(1 / q, rel=0.01)


def test_spectrum_peaks() -> None:
    x = sine(1000, 0.5, 1.0)
    f, s = ac.spectrum(x, R)
    top = ac.peaks(s, f, 3)
    assert top[0][0] == pytest.approx(1000, abs=4)
    assert top[0][1] == pytest.approx(-6, abs=1.5) or top[0][1] > -8


def test_alias_products() -> None:
    # 1 kHz held at 12 kHz, 48 kHz out: 11k, 13k, 23k (25k folds to 23k, ...).
    assert ac.alias_products(1000, 12000, R) == [11000.0, 13000.0, 23000.0]


def wav_bytes(tag: int, bits: int, data: bytes, size: int, ch: int = 2) -> bytes:
    fmt = struct.pack("<HHIIHH", tag, ch, R, R * ch * bits // 8, ch * bits // 8, bits)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", size)
        + data
    )


def test_load_float_wav_zero_size(tmp_path: pathlib.Path) -> None:
    x = sine(440, 0.01).astype("<f4")
    p = tmp_path / "a.wav"
    p.write_bytes(wav_bytes(3, 32, x.tobytes(), 0))
    rate, y = ac.load(p)
    assert rate == R and y.shape == x.shape
    assert np.array_equal(y, x.astype(np.float64))
    p.write_bytes(wav_bytes(3, 32, x.tobytes(), 10**9))
    assert ac.load(p)[1].shape == x.shape


def test_load_pcm16_and_raw(tmp_path: pathlib.Path) -> None:
    x = np.array([[0, -32768], [16384, 32767]], "<i2")
    p = tmp_path / "b.wav"
    p.write_bytes(wav_bytes(1, 16, x.tobytes(), x.nbytes))
    _, y = ac.load(p)
    assert y[0, 1] == -1.0 and y[1, 0] == 0.5
    f = np.array([[0.25, -0.5]], "<f4")
    (tmp_path / "c.raw").write_bytes(f.tobytes())
    rate, z = ac.load(tmp_path / "c.raw")
    assert rate == R and z.tolist() == [[0.25, -0.5]]


def test_analyse_json(tmp_path: pathlib.Path) -> None:
    import json

    feats = ac.analyse(sine(1000, 0.2), R)
    json.dumps(feats, allow_nan=False)
    assert feats["lr_diff"]["rel_db"] is None  # type: ignore[index]


def test_tone_harmonics_levels() -> None:
    x = sine(261.6, 0.3, 0.5)
    x = x + sine(3 * 261.6, 0.3, 0.05)  # 3rd harmonic 20 dB down
    h = ac.tone_harmonics(x, R, 261.6, kmax=6)
    p = h["power"]
    assert abs(10 * math.log10(p[2] / p[0]) + 20) < 0.5  # type: ignore[index]
    assert 10 * math.log10(p[1] / p[0]) < -60  # type: ignore[index]


def test_image_search_finds_held_rate() -> None:
    fs = 12000.0
    n = int(0.3 * R)
    t = np.arange(n) / R
    held = np.sin(2 * np.pi * 261.6 * (np.floor(t * fs) / fs))  # zero-order hold
    h = ac.tone_harmonics(held[:, None], R, 261.6, kmax=1)
    cands = ac.image_search(h["residual"][:, None], R, 261.6)  # type: ignore[index]
    assert abs(cands[0][0] - fs) < 30


def test_autocorr_spacing() -> None:
    hop = 0.005
    e = np.full(800, -90.0)
    for k in range(4):
        e[20 + k * 75 : 40 + k * 75] = -20.0 - 6 * k  # 0.375 s apart
    sp, corr = ac.autocorr_spacing(e, hop, 0.1, 1.5)
    assert abs(sp - 0.375) < 0.011
    assert corr > 0.3


def test_modulation_rate() -> None:
    t = np.arange(int(2.0 * R)) / R
    f0, fm = 261.6, 1.5
    left = np.sin(2 * np.pi * f0 * t + 2 * np.sin(2 * np.pi * fm * t))
    right = np.sin(2 * np.pi * f0 * t)
    m = ac.modulation_rate(np.stack([left, right], axis=1), R, f0)
    assert abs(m["phase_hz"] - fm) < 0.2
    mono = ac.modulation_rate(np.stack([right, right], axis=1), R, f0)
    assert math.isnan(mono["phase_hz"]) or mono["phase_dev"] < 1e-6


def test_autocorr_spacing_rises() -> None:
    # Overlapping echoes: each starts a step up, blocks are longer than the spacing.
    hop = 0.005
    amp = np.zeros(800)
    for k in range(5):
        amp[20 + k * 75 : 20 + k * 75 + 90] += 0.5**k
    sp, _ = ac.autocorr_spacing(20 * np.log10(amp + 1e-9), hop, 0.1, 1.5, rises=True)
    assert abs(sp - 0.375) < 0.011


def test_segment_stats() -> None:
    t = np.arange(R) / R
    a = np.sin(2 * np.pi * 500 * t)
    b = np.sin(2 * np.pi * 4000 * t)
    x = np.stack([a, 0.5 * b], axis=1)
    s = ac.segment_stats(x, R, [0.0, 0.5], 0.4)
    assert s[0]["lr_db"] == pytest.approx(10 * np.log10(0.5 / 0.125), abs=0.2)
    assert 500 < s[0]["centroid_hz"] < 4000
    assert s[0]["band_300"] > s[0]["band_3000"] + 5
    mono = ac.segment_stats(a[:, None], R, [0.0], 0.4)[0]
    assert math.isnan(mono["lr_db"]) and mono["centroid_hz"] == pytest.approx(
        500, abs=30
    )
    assert math.isnan(ac.segment_stats(x, R, [5.0], 0.4)[0]["rms_db"])


def test_first_above_and_rt60_slope() -> None:
    t = np.arange(400) * 0.005
    e = np.where(t < 0.5, -80.0, -10.0 - 30.0 * (t - 0.5))
    assert ac.first_above(t, e, 20) == pytest.approx(0.5, abs=0.006)
    assert ac.rt60_slope(t, e, 0.6, 1.8) == pytest.approx(2.0, rel=0.02)
    assert math.isnan(ac.rt60_slope(t, np.zeros_like(t), 0.1, 1.0))


def test_inst_freq_dev() -> None:
    t = np.arange(2 * R) / R
    f0, fm, tau = 261.6, 1.5, 0.004  # delay swing amplitude 4 ms
    x = np.sin(2 * np.pi * f0 * (t - tau * np.sin(2 * np.pi * fm * t)))
    r = ac.inst_freq_dev(x, R, f0)
    assert r["mod_hz"] == pytest.approx(fm, abs=0.15)
    assert r["tau_pp_ms"] == pytest.approx(8.0, rel=0.15)
    assert ac.inst_freq_dev(np.sin(2 * np.pi * f0 * t), R, f0)["dev_pp_hz"] < 0.5
