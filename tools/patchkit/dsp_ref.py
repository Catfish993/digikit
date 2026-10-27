"""Float32 reference models of the SDK DSP step macros (sdk/dsp/*.inc) over one block of 32 samples.

Each model does the macro's operations in its order, with one float32 rounding per SHARC+ operation and no fused
multiply-add, so the kernel on the concrete core equals the model bit for bit: gain, mix, ramp, onepole, sat, biquad.

recip, div and rsqrt start from the core's seed (RECIPS 8 bits, RSQRTS 4 bits; the seed table is not published and is not
modelled). They return the exact value in float64; the kernel is compared within a relative bound:

  REL_RECIP = 2^-21   2 Newton steps from an 8-bit seed leave about 2^-32; the rest is rounding. Each step rounds x y,
                      2 - x y and y (2 - x y) once, about 3 x 2^-24 = 1.8e-7 in the last step. 2^-21 = 4.8e-7 is 4 ulp.
  REL_DIV   = 2^-20   the reciprocal bound plus the rounding of the final multiply (2^-24), rounded up to 2^-20.
  REL_RSQRT = 2^-21   3 Newton steps from a 4-bit seed leave about 4e-9 (e' = 1.5 e^2: 6e-3, 5e-5, 4e-9); the rest is
                      the rounding of x y, (x y) y, 1.5 - .., y (..), about 4 x 2^-24 = 2.4e-7.
"""

from __future__ import annotations

import numpy as np

f32 = np.float32

BLOCK = 32
REL_RECIP = 2.0**-21
REL_DIV = 2.0**-20
REL_RSQRT = 2.0**-21


def _v(x) -> np.ndarray:
    return np.asarray(x, dtype=f32)


def gain(x, g) -> np.ndarray:
    """DK_GAIN_STEP: y = x g."""
    return _v(x) * f32(g)


def mix(a, b, m) -> np.ndarray:
    """DK_MIX_STEP: t = b - a; t = t m; y = a + t."""
    a = _v(a)
    t = _v(b) - a
    t = t * f32(m)
    return a + t


def ramp(cur, target, n: int = BLOCK, inv_n: float = 1.0 / BLOCK) -> np.ndarray:
    """DK_RAMP_SETUP then n x DK_RAMP_STEP: the values after each step (the first is cur + step)."""
    step = f32(target) - f32(cur)
    step = step * f32(inv_n)
    c = f32(cur)
    out = np.empty(n, dtype=f32)
    for i in range(n):
        c = c + step
        out[i] = c
    return out


def onepole(x, a, y0) -> np.ndarray:
    """DK_ONEPOLE_STEP per sample: t = x - y; t = a t; y = y + t; returns y after each sample."""
    x = _v(x)
    a = f32(a)
    y = f32(y0)
    out = np.empty(len(x), dtype=f32)
    for i, xi in enumerate(x):
        t = xi - y
        t = a * t
        y = y + t
        out[i] = y
    return out


def biquad(x, b0, b1, b2, a1, a2, s1, s2):
    """_dk_biquad (sdk/dsp/biquad.inc), per sample: p0 = b0 x; p1 = b1 x; p2 = b2 x; y = p0 + s1; u = s2 + p1; q = a1 y;
    s1 = u - q; q = a2 y; s2 = p2 - q. Returns (y, s1, s2) with the state after the last sample."""
    x = _v(x)
    b0, b1, b2, a1, a2 = (f32(c) for c in (b0, b1, b2, a1, a2))
    s1, s2 = f32(s1), f32(s2)
    out = np.empty(len(x), dtype=f32)
    for i, xi in enumerate(x):
        p0 = b0 * xi
        p1 = b1 * xi
        p2 = b2 * xi
        y = p0 + s1
        u = s2 + p1
        q = a1 * y
        s1 = u - q
        q = a2 * y
        s2 = p2 - q
        out[i] = y
    return out, s1, s2


def sat(x) -> np.ndarray:
    """DK_SAT_STEP: c = clip(x, +-1); t = c c; t = t 0.5; t = 1.5 - t; y = c t."""
    c = np.clip(_v(x), f32(-1.0), f32(1.0))
    t = c * c
    t = t * f32(0.5)
    t = f32(1.5) - t
    return c * t


def recip(x) -> np.ndarray:
    """DK_RECIP: the exact 1 / x in float64 (compare within REL_RECIP)."""
    return 1.0 / _v(x).astype(np.float64)


def div(n, d) -> np.ndarray:
    """DK_DIV: the exact n / d in float64 (compare within REL_DIV)."""
    return _v(n).astype(np.float64) / _v(d).astype(np.float64)


def rsqrt(x) -> np.ndarray:
    """DK_RSQRT: the exact 1 / sqrt(x) in float64 (compare within REL_RSQRT), x > 0."""
    return 1.0 / np.sqrt(_v(x).astype(np.float64))


def max_rel_error(got, want) -> float:
    """Largest |got - want| / |want| over the block."""
    got = np.asarray(got, dtype=np.float64)
    want = np.asarray(want, dtype=np.float64)
    return float(np.max(np.abs(got - want) / np.abs(want)))
