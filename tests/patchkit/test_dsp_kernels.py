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


# --- block routines (sdk/dsp/call.inc, biquad.inc): the C convention, called from assembly and from selcc C ---

BQ = BUF + 0x200
BQ_IN = (
    BQ,
    BQ + 0x80,
)  # two input blocks; the two words after each are a marker region the routine must not read
BQ_OUT = (BQ + 0x40, BQ + 0xC0)
BQ_ST = BQ + 0x100
BQ_STACK = BQ + 0x300  # I6 = I7 at the call; the frames grow down from here
BQ_CALL, BQ_ROUTINE = (
    3,
    200,
)  # DK_CALL; _dk_biquad: 25 before the loop, 5 x 32 in and after it, 15 after (counted in biquad.inc)
BQ_SAVED = {f"R{n}": 0x7000 + n for n in (3, 5, 6, 7, 9, 10, 11, 13, 14, 15)}
BQ_SAVED.update(
    {"I0": 0x08021000, "I1": 0x08021010, "I2": 0x08021020, "I3": 0x08021030}
)
BQ_SAVED.update({"I5": 0x08021040, "M1": 11, "M2": 12, "M3": 13, "B3": 0x08021050})
BQ_CONSTS = {"M5": 0, "M6": 1, "M7": -1 & 0xFFFFFFFF, "M14": 1}
BQ_READ_REGS = tuple(
    k
    for k in (*BQ_SAVED, *BQ_CONSTS, "I6", "I7")
    if k not in ("R5", "R6", "R7", "R9", "R10", "R11", "R13", "I0")
)

RNG = np.random.default_rng(7)
BQ_X = RNG.uniform(-1.0, 1.0, 2 * N).astype(f32)


def cookbook_lowpass(fs, f0, q):
    w = 2 * np.pi * f0 / fs
    al = np.sin(w) / (2 * q)
    a0 = 1 + al
    c = np.cos(w)
    return [f32(v / a0) for v in ((1 - c) / 2, 1 - c, (1 - c) / 2, -2 * c, 1 - al)]


def cookbook_bandpass(fs, f0, q):
    """Constant 0 dB peak gain band-pass."""
    w = 2 * np.pi * f0 / fs
    al = np.sin(w) / (2 * q)
    a0 = 1 + al
    return [f32(v / a0) for v in (al, 0.0, -al, -2 * np.cos(w), 1 - al)]


BQ_SETS = {
    "lowpass": (cookbook_lowpass(48000, 1000, 0.7071), (0.0, 0.0)),
    "bandpass": (cookbook_bandpass(48000, 2000, 8.0), (0.1, -0.05)),
}


def bq_model(coef, state):
    y1, s1, s2 = dsp_ref.biquad(BQ_X[:N], *coef, *state)
    y2, s1, s2 = dsp_ref.biquad(BQ_X[N:], *coef, s1, s2)
    return y1, y2, [s1, s2]


def bq_poke(coef, state):
    dm = {BQ_ST + i: b(v) for i, v in enumerate([*coef, *state])}
    for k in range(2):
        for i, w in enumerate(bits(BQ_X[k * N : (k + 1) * N])):
            dm[BQ_IN[k] + i] = w
        dm[BQ_IN[k] + N] = dm[BQ_IN[k] + N + 1] = (
            0x7FC00001  # NaN marker after the block
        )
    return dm


def test_biquad_from_assembly_two_blocks_in_a_row():
    for in_place in (False, True):
        for coef, state in BQ_SETS.values():
            outs = BQ_IN if in_place else BQ_OUT
            text = "\n".join(
                [
                    "#define DK_BLOCK 32",
                    '#include "dsp/call.inc"',
                    '#include "dsp/biquad.inc"',
                    ".SECTION/PM seg_pmco;",
                    ".GLOBAL _bq_h;",
                    "_bq_h:",
                    *(
                        line
                        for k in range(2)
                        for line in (
                            "    R4 = %d;" % BQ_IN[k],
                            "    R8 = %d;" % outs[k],
                            "    R12 = %d;" % BQ_ST,
                            "    DK_CALL(_dk_biquad);",
                        )
                    ),
                    "    RTS;",
                    "",
                ]
            )
            _, linked = pk_sharc.build(
                pk_build.expand_includes(text, [ROOT / "sdk"]), BASE
            )
            regs = {**BQ_SAVED, **BQ_CONSTS, "I6": BQ_STACK, "I7": BQ_STACK}
            r = pk_sharc.run_call(
                linked,
                "_bq_h",
                regs=regs,
                dm_words=bq_poke(coef, state),
                reads={"y0": (outs[0], N), "y1": (outs[1], N), "st": (BQ_ST + 5, 2)},
                read_regs=BQ_READ_REGS,
            )
            assert r["halt"]["reason"] == "breakpoint", r["halt"]
            y1, y2, st = bq_model(coef, state)
            assert np.array_equal(np.array(r["y0"], dtype=np.uint32), bits(y1))
            assert np.array_equal(np.array(r["y1"], dtype=np.uint32), bits(y2))
            assert r["st"] == bits(st)
            # the cost: 3 loads of R4/R8/R12 and DK_CALL per block, the routine, the final RTS
            assert r["instructions"] <= 2 * (3 + BQ_CALL + BQ_ROUTINE) + 1
            for name, want in {
                **BQ_SAVED,
                **BQ_CONSTS,
                "I6": BQ_STACK,
                "I7": BQ_STACK,
            }.items():
                assert r["regs"][name] == want, name


def test_biquad_never_reads_past_the_input_block():
    coef, state = BQ_SETS["bandpass"]
    text = "\n".join(
        [
            "#define DK_BLOCK 32",
            '#include "dsp/call.inc"',
            '#include "dsp/biquad.inc"',
            ".SECTION/PM seg_pmco;",
            ".GLOBAL _bq_h;",
            "_bq_h:",
            "    R4 = %d;" % BQ_IN[0],
            "    R8 = %d;" % BQ_OUT[0],
            "    R12 = %d;" % BQ_ST,
            "    DK_CALL(_dk_biquad);",
            "    RTS;",
            "",
        ]
    )
    _, linked = pk_sharc.build(pk_build.expand_includes(text, [ROOT / "sdk"]), BASE)
    regs = {**BQ_SAVED, **BQ_CONSTS, "I6": BQ_STACK, "I7": BQ_STACK}
    # the watch is a read watchpoint of the core's driver (CallSession.record_reads); the control range proves that it fires
    for lo, count, must_hit in ((BQ_IN[0] + N, 2, False), (BQ_IN[0] + N - 2, 2, True)):
        sess = pk_sharc.CallSession([linked], 0xB9F000)
        for addr, word in bq_poke(coef, state).items():
            sess.poke(addr, word)
        sess.record_reads(lo, count)
        r = sess.call("_bq_h", regs)
        assert r["halt"]["reason"] == "breakpoint", r["halt"]
        assert bool(sess.stores) == must_hit, (lo, [e.address for e in sess.stores])


def test_biquad_from_selcc_c():
    c = """
void dk_biquad(int x, int y, int st);
void run(int x, int y, int st) { dk_biquad(x, y, st); }
"""
    kernel_text = "\n".join(
        [
            "#define DK_BLOCK 32",
            '#include "dsp/call.inc"',
            '#include "dsp/biquad.inc"',
            "",
        ]
    )
    kbase = BASE + 0x1000
    _, kernel = pk_sharc.build(
        pk_build.expand_includes(kernel_text, [ROOT / "sdk"]), kbase
    )
    caller = pk_sharc.link(
        pk_sharc.compile_c(c), BASE, {"dk_biquad.": kernel.symbols["dk_biquad."]}
    )
    ret, frame = 0xB9E000, BQ_STACK
    for coef, state in BQ_SETS.values():
        y1, _, _ = dsp_ref.biquad(BQ_X[:N], *coef, *state)
        dm = {frame: 0, frame - 1: ret - 1, **bq_poke(coef, state)}
        r = pk_sharc.run_call(
            caller,
            "run.",
            regs={
                **BQ_CONSTS,
                "R4": BQ_IN[0],
                "R8": BQ_OUT[0],
                "R12": BQ_ST,
                "I6": frame,
                "I7": frame - 2,
            },
            dm_words=dm,
            reads={"y": (BQ_OUT[0], N)},
            return_sw=ret,
            others=(kernel,),
            max_steps=20000,
        )
        assert r["halt"]["reason"] == "breakpoint", r["halt"]
        assert np.array_equal(np.array(r["y"], dtype=np.uint32), bits(y1))


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
