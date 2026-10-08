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
