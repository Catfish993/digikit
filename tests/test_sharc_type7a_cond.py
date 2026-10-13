"""Conditional Type 7a (IF cond [compute,] MODIFY): SHARC+ PRM p.14-47.

"If a condition is specified, it affects the entire instruction" (compute and
index modify).  SISD tests PEx's condition.  In SIMD mode the compute runs in
each PE whose own condition is true and the index modify happens when either
PE's condition is true (logical OR).  Stock DT2 1.17 has `IF EQ modify(I4,
M4)` at 0x1cbc93 (finding 07).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sharc_trace as st
from sharc_core.encoding import AZ_BIT
from sharc_disasm import Instruction

U = st.UREG_CODES
SIMD = 1 << 21
ADD = 0x81
EQ, NE, TRUE = 0x00, 0x10, 0x1F


def run(cond, *, simd=False, az_x=False, az_y=False, with_compute=False):
    state = st.State(
        0x10,
        {
            U["MODE1"]: st.Const(SIMD if simd else 0),
            U["ASTATX"]: st.Const(int(az_x) << AZ_BIT),
            U["ASTATY"]: st.Const(int(az_y) << AZ_BIT),
            U["I4"]: st.Const(0x1000),
            U["M4"]: st.Const(3),
            U["L4"]: st.Const(0),
            U["R0"]: st.Const(0x3F800000),  # 1.0
            U["R1"]: st.Const(0x3F800000),
            U["R2"]: st.Const(0),
            U["S0"]: st.Const(0x3F800000),
            U["S1"]: st.Const(0x3F800000),
            U["S2"]: st.Const(0),
        },
    )
    compute = ((ADD << 12) | (2 << 8) | (0 << 4) | 1) if with_compute else 0
    fields = {
        "cond[4:0]": cond,
        "g": 0,
        "idis[2:0]": 0,
        "is[1:0]": 0,
        "is[2:2]": 1,
        "m[2:0]": 4,
        "w": 1,
        "l": 0,
        "compute[22:16]": compute >> 16,
        "compute[15:0]": compute & 0xFFFF,
    }
    [out] = st._execute(state, Instruction(0, 6, "7a", fields, kind="confident"))
    assert out.stopped is None, out.stopped
    return out


def i4(out):
    return out.uregs[U["I4"]].value


def test_sisd_eq_true_modifies():
    assert i4(run(EQ, az_x=True)) == 0x1003


def test_sisd_eq_false_skips():
    assert i4(run(EQ, az_x=False)) == 0x1000


def test_sisd_ne_true_modifies():
    assert i4(run(NE, az_x=False)) == 0x1003


def test_simd_modify_is_or_of_both_pes():
    assert i4(run(EQ, simd=True, az_x=False, az_y=True)) == 0x1003
    assert i4(run(EQ, simd=True, az_x=True, az_y=False)) == 0x1003
    assert i4(run(EQ, simd=True, az_x=False, az_y=False)) == 0x1000


def test_simd_compute_runs_only_in_pes_whose_condition_holds():
    out = run(EQ, simd=True, az_x=True, az_y=False, with_compute=True)
    assert i4(out) == 0x1003
    assert out.uregs[U["R2"]].value == 0x40000000  # 1.0 + 1.0
    assert out.uregs[U["S2"]].value == 0
    out = run(EQ, simd=True, az_x=False, az_y=True, with_compute=True)
    assert out.uregs[U["R2"]].value == 0
    assert out.uregs[U["S2"]].value == 0x40000000


def test_conditional_compute_is_skipped_with_the_modify():
    out = run(EQ, az_x=False, with_compute=True)
    assert i4(out) == 0x1000
    assert out.uregs[U["R2"]].value == 0
    out = run(EQ, az_x=True, with_compute=True)
    assert i4(out) == 0x1003
    assert out.uregs[U["R2"]].value == 0x40000000


def test_unconditional_simd_compute_runs_in_both_pes():
    out = run(TRUE, simd=True, with_compute=True)
    assert out.uregs[U["R2"]].value == out.uregs[U["S2"]].value == 0x40000000
