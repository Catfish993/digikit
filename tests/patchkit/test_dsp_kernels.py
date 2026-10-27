"""SDK DSP step macros (sdk/dsp/*.inc): each macro in a 32-sample block loop on the concrete core, against the float32
models of tools/patchkit/dsp_ref.py, plus the instruction count of the loop.

recip, div and rsqrt run with the core's approximate seed (approx_recips: the seed ROM is not published), so they are
compared within the bounds stated in dsp_ref.py. Skips when Selache is not built."""

import numpy as np
import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.patchkit import (  # noqa: E402
    dsp_ref,
    pk_build,
    pk_sharc,
    pk_toolchains,
)

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/patchkit/pk_toolchains.py selache)",
)

BASE = 0xB90000  # short-word (PC) address in L2 code space
BUF = 0x08020000  # normal-word DM address in L2
IN0, IN1, OUT = BUF, BUF + 0x40, BUF + 0x80
N = dsp_ref.BLOCK
f32 = np.float32


def bits(a) -> list[int]:
    return [int(w) for w in np.asarray(a, dtype=f32).view(np.uint32)]


def b(x: float) -> int:
    return int(f32(x).view(np.uint32))


def fill(edges, count=N) -> np.ndarray:
    """The edge values, then a ramp from -4 to 4 up to `count` values (float32)."""
    ramp = np.linspace(-4.0, 4.0, count - len(edges))
    return np.concatenate([np.asarray(edges, dtype=f32), ramp.astype(f32)])


# zero, +-1, +-8, small values, and some in-between values
X = fill([0.0, 1.0, -1.0, 8.0, -8.0, 0.001, -0.001, 0.5, -0.5, 1.5, -1.5, 0.25])
Y = X[::-1].copy()
# 1e-3 .. 1e3, both signs, no zero
WIDE = np.concatenate(
    [np.geomspace(1e-3, 1e3, 16), -np.geomspace(1e-3, 1e3, 16)]
).astype(f32)
POS = np.geomspace(1e-3, 1e3, N).astype(f32)


def kernel(
    inc, setup, setup_n, macro, macro_n, consts, inputs, out_reg="F8", approx=False
):
    """Build and run one loop: load the inputs (F0, F1), the macro, the store of `out_reg`; 32 samples.

    Returns (output words, executed instructions, ceiling). The ceiling documents the cost:
      prologue (I0, I1, [I2], M6)  +  setup_n  +  1 (LCNTR)
      + 32 x (loads + macro_n + 1 store + NOPs up to the 4 instructions a loop needs)  +  1 (RTS)."""
    loads = [f"F{i} = DM(I{i + 1}, M6);" for i in range(len(inputs))]
    body_n = len(loads) + macro_n + 1
    pad = ["NOP;"] * max(0, 4 - body_n)
    per_sample = body_n + len(pad)
    pointers = ["I0 = %d;" % OUT, "I1 = %d;" % IN0]
    if len(inputs) > 1:
        pointers.append("I2 = %d;" % IN1)
    text = "\n".join(
        [
            '#include "dsp/%s.inc"' % inc,
            ".SECTION/PM seg_pmco;",
            ".GLOBAL _t;",
            "_t:",
            *("    " + p for p in pointers),
            "    M6 = 1;",
            *("    " + s for s in setup),
            "    LCNTR = 32, DO _t_end UNTIL LCE;",
            *("        " + s for s in loads),
            "        %s;" % macro,
            *("        " + s for s in pad),
            "_t_end: DM(I0, M6) = %s;" % out_reg,
            "    RTS;",
            "",
        ]
    )
    text = pk_build.expand_includes(text, [ROOT / "sdk"])
    _, linked = pk_sharc.build(text, BASE)
    dm = {}
    for base, vec in zip((IN0, IN1), inputs, strict=False):
        dm.update({base + i: w for i, w in enumerate(bits(vec))})
    r = pk_sharc.run_call(
        linked,
        "_t",
        regs=consts,
        dm_words=dm,
        reads={"out": (OUT, N)},
        approx_recips=approx,
    )
    assert r["halt"]["reason"] == "breakpoint", r["halt"]
    ceiling = len(pointers) + 1 + setup_n + 1 + N * per_sample + 1
    return np.array(r["out"], dtype=np.uint32).view(f32), r["instructions"], ceiling


def test_gain():
    g = f32(0.7)
    out, n, ceiling = kernel(
        "gain", [], 0, "DK_GAIN_STEP(F8, F0, F4)", 1, {"R4": b(g)}, [X]
    )  # g = 0.7
    assert np.array_equal(out.view(np.uint32), dsp_ref.gain(X, g).view(np.uint32))
    assert n <= ceiling


def test_mix():
    m = f32(0.3)
    out, n, ceiling = kernel(
        "gain", [], 0, "DK_MIX_STEP(F8, F0, F1, F4, F9)", 3, {"R4": b(m)}, [X, Y]
    )  # m = 0.3
    assert np.array_equal(out.view(np.uint32), dsp_ref.mix(X, Y, m).view(np.uint32))
    assert n <= ceiling


@pytest.mark.parametrize("cur, target", [(0.0, 1.0), (2.5, -3.0), (-1.0, -1.0)])
def test_ramp(cur, target):
    consts = {"R8": b(cur), "R6": b(target), "R7": b(1 / 32)}  # 1/32 = 0.03125
    out, n, ceiling = kernel(
        "ramp",
        ["DK_RAMP_SETUP(F4, F8, F6, F7);"],
        2,
        "DK_RAMP_STEP(F8, F4)",
        1,
        consts,
        [X],
    )
    assert np.array_equal(
        out.view(np.uint32), dsp_ref.ramp(cur, target).view(np.uint32)
    )
    assert n <= ceiling


def test_onepole():
    a, y0 = f32(0.1227), f32(0.5)
    out, n, ceiling = kernel(
        "onepole",
        [],
        0,
        "DK_ONEPOLE_STEP(F8, F0, F4, F9)",
        3,
        {"R4": b(a), "R8": b(y0)},  # a = 0.1227, y0 = 0.5
        [X],
    )
    assert np.array_equal(
        out.view(np.uint32), dsp_ref.onepole(X, a, y0).view(np.uint32)
    )
    assert n <= ceiling


def test_sat():
    consts = {"R4": b(1.0), "R5": b(0.5), "R6": b(1.5)}  # one, half, k15
    out, n, ceiling = kernel(
        "sat", [], 0, "DK_SAT_STEP(F8, F0, F4, F5, F6, F9)", 5, consts, [X]
    )
    assert np.array_equal(out.view(np.uint32), dsp_ref.sat(X).view(np.uint32))
    assert np.max(np.abs(out)) <= 1.0
    assert n <= ceiling


def test_recip():
    out, n, ceiling = kernel(
        "recip",
        [],
        0,
        "DK_RECIP(F8, F0, F4, F9)",
        7,
        {"R4": b(2.0)},  # two
        [WIDE],
        approx=True,
    )
    assert dsp_ref.max_rel_error(out, dsp_ref.recip(WIDE)) <= dsp_ref.REL_RECIP
    assert n <= ceiling


def test_div():
    out, n, ceiling = kernel(
        "recip",
        [],
        0,
        "DK_DIV(F8, F0, F1, F4, F9, F10)",
        8,
        {"R4": b(2.0)},  # two
        [X[::-1].copy() + f32(0.125), WIDE],
        approx=True,
    )
    num = X[::-1].copy() + f32(0.125)
    got, want = out.astype(np.float64), dsp_ref.div(num, WIDE)
    assert np.max(np.abs(got - want) / np.abs(want)) <= dsp_ref.REL_DIV
    assert n <= ceiling


def test_rsqrt():
    out, n, ceiling = kernel(
        "recip",
        [],
        0,
        "DK_RSQRT(F8, F0, F4, F5, F9, F10)",
        16,
        {"R4": b(0.5), "R5": b(1.5)},  # half, k15
        [POS],
        approx=True,
    )
    assert dsp_ref.max_rel_error(out, dsp_ref.rsqrt(POS)) <= dsp_ref.REL_RSQRT
    assert n <= ceiling
