"""Circular buffering for ordinary DAG load/store post-modify.

Rules under test (SHARC+ Core Programming Reference, out/refs/sharc-plus-prm,
printed pages 6-9..6-12 and 6-23..6-26; out/native/circbuf/RULES.md):
post-modify wraps around B/L when MODE1.CBUFEN is set and L != 0; pre-modify
neither updates I nor wraps; L is scaled by the access size in byte space.
"""


import pytest
from support.paths import REPO_ROOT

from tools.sharc_dev import sharc_trace as T
from tools.sharc_dev.sharc_core import memory as m
from tools.sharc_dev.sharc_core.values import Const, Unknown
from tools.sharc_dev.sharc_disasm import Instruction

CBUFEN = 1 << 24
PEYEN = 1 << 21
U = T.UREG_CODES


def run(form, fields, regs, *, length=4, assume_nw32=False):
    uregs = {U[name]: Const(value) for name, value in regs.items()}
    state = T.State(0x10, uregs, assume_nw32=assume_nw32)
    [result] = T._execute(state, Instruction(0, length, form, fields, kind="confident"))
    assert result.stopped is None, result.stopped
    return result


def reg(result, name):
    return result.uregs[U[name]]


# One entry per affected form: fields (DAG1, I0, M1 or immediate), the
# modifier kind and how to express a modifier. Stores of R1 keep memory out
# of the picture. REG forms take the modifier from M1; IMM forms from the
# six-bit signed immediate.
def f3a(u=1):
    return {
        "compute": 0,
        "cond": 31,
        "d": 1,
        "g": 0,
        "i": 0,
        "l": 0,
        "m": 1,
        "u": u,
        "ureg": 1,
    }


def f3b(u=1):
    return {
        "cond[4:0]": 31, "d": 1, "g": 0, "i[2:0]": 0, "l": 0, "m[2:0]": 1,
        "u": u, "ureg[6:0]": 1, "w": 1, "x": 1,
    }  # fmt: skip


def f3c():
    return {"d": 1, "dmi[2:0]": 0, "dmm[2:0]": 1, "dreg[3:0]": 1}


def f3d(u=1):
    return {
        "cond[4:0]": 31, "d": 1, "ex": 0, "g": 0, "i[2:0]": 0, "l": 0, "m[2:0]": 1,
        "u": u, "ureg[6:0]": 1, "w": 0, "x": 0,
    }  # fmt: skip


def f1a():
    return {
        "compute[15:0]": 0, "compute[22:16]": 0, "dmd": 1, "dmdreg[3:0]": 1,
        "dmi[2:0]": 0, "dmm[2:0]": 1, "pmd": 1, "pmdreg[3:0]": 2,
        "pmi[1:0]": 0, "pmi[2:2]": 0, "pmm[2:0]": 7,
    }  # fmt: skip


def f16a():
    return {
        "by": 0,
        "data[15:0]": 0x1234,
        "data[31:16]": 0,
        "g": 0,
        "i[2:0]": 0,
        "m[2:0]": 1,
        "sl": 0,
    }


def f6a():
    return {
        "cond[4:0]": 31, "d": 1, "dataex[3:0]": 0, "dreg[3:0]": 1, "g": 0,
        "i[2:0]": 0, "m[2:0]": 1, "shiftimm[15:0]": 0, "shiftimm[22:16]": 0,
    }  # fmt: skip


def imm(delta):
    return {"data[5:5]": (delta >> 5) & 1, "data[4:0]": delta & 0x1F}


def f4a(delta, u=1):
    return {
        "compute[15:0]": 0, "compute[22:16]": 0, "cond[4:0]": 31, "d": 1, "dreg[3:0]": 1,
        "g": 0, "i[2:0]": 0, "u": u, **imm(delta),
    }  # fmt: skip


def f4b(delta, u=1, lxw=(1, 1, 1)):
    return {
        "cond[4:0]": 31, "d": 1, "dreg[3:0]": 1, "g": 0, "i[2:0]": 0, "l": lxw[0],
        "u": u, "w": lxw[2], "x": lxw[1], **imm(delta),
    }  # fmt: skip


def f4d(delta, u=1, lxw=(1, 1, 1)):
    return f4b(delta, u, lxw)


REG_FORMS = {
    "3a": (f3a, 6),
    "3b": (f3b, 6),
    "3c": (lambda u=1: f3c(), 4),
    "3d": (f3d, 6),
    "1a": (lambda u=1: f1a(), 4),
    "16a": (lambda u=1: f16a(), 6),
    "6a_mem": (lambda u=1: f6a(), 6),
}
IMM_FORMS = {"4a": (f4a, 6), "4b": (f4b, 6), "4d": (f4d, 6)}

# Buffer [0x1000, 0x1010) in word space (assume_nw32 off: scale 1 for
# normal word). MODE1 = CBUFEN. I0 starts near an end.
BASE = {"B0": 0x1000, "L0": 0x10, "MODE1": CBUFEN}


def regs(i, m=None, **extra):
    r = {"I0": i, **BASE, **extra}
    if m is not None:
        r["M1"] = m & 0xFFFFFFFF
    return r


@pytest.mark.parametrize("form", REG_FORMS)
@pytest.mark.parametrize(
    "i,mod,expected",
    [
        (0x100C, 8, 0x1004),  # crosses the top
        (0x1004, -8, 0x100C),  # crosses the bottom
        (0x100F, 1, 0x1000),  # lands exactly on B + L: wraps to B
        (0x1000, -1, 0x100F),  # one below B
        (0x1004, 2, 0x1006),  # inside: linear
        (0x1003, 0x10, 0x1003),  # |M| == L: full lap
        (0x1003, -0x10, 0x1003),
    ],
)
def test_register_modifier_post_modify_wraps(form, i, mod, expected):
    fields_fn, length = REG_FORMS[form]
    result = run(form, fields_fn(), regs(i, mod), length=length)
    assert reg(result, "I0") == Const(expected)


@pytest.mark.parametrize("form", IMM_FORMS)
@pytest.mark.parametrize(
    "i,delta,expected",
    [
        (0x100C, 8, 0x1004),
        (0x1004, -8, 0x100C),
        (0x100F, 1, 0x1000),
        (0x1000, -1, 0x100F),
        (0x1004, 2, 0x1006),
    ],
)
def test_immediate_modifier_post_modify_wraps(form, i, delta, expected):
    fields_fn, length = IMM_FORMS[form]
    result = run(form, fields_fn(delta), regs(i), length=length)
    assert reg(result, "I0") == Const(expected)


@pytest.mark.parametrize("form", REG_FORMS)
def test_length_zero_is_linear(form):
    fields_fn, length = REG_FORMS[form]
    r = regs(0x100C, 8)
    r["L0"] = 0
    result = run(form, fields_fn(), r, length=length)
    assert reg(result, "I0") == Const(0x1014)


@pytest.mark.parametrize("form", REG_FORMS)
def test_cbufen_clear_is_linear(form):
    fields_fn, length = REG_FORMS[form]
    r = regs(0x100C, 8)
    r["MODE1"] = 0
    result = run(form, fields_fn(), r, length=length)
    assert reg(result, "I0") == Const(0x1014)


@pytest.mark.parametrize("form", IMM_FORMS)
def test_immediate_forms_respect_length_zero_and_cbufen(form):
    fields_fn, length = IMM_FORMS[form]
    for tweak in ({"L0": 0}, {"MODE1": 0}):
        r = {**regs(0x100C), **tweak}
        result = run(form, fields_fn(8), r, length=length)
        assert reg(result, "I0") == Const(0x1014)


@pytest.mark.parametrize("form", ["3a", "3b", "3d"])
def test_pre_modify_neither_wraps_nor_updates_i(form):
    fields_fn, length = REG_FORMS[form]
    result = run(form, fields_fn(u=0), regs(0x100C, 8), length=length)
    assert reg(result, "I0") == Const(0x100C)
    event = [e for e in result.trace if e["action"] == "store"][-1]
    assert event["address"] == 0x1014  # pre-modify address is unwrapped


@pytest.mark.parametrize("form", ["4a", "4b", "4d"])
def test_immediate_pre_modify_neither_wraps_nor_updates_i(form):
    fields_fn, length = IMM_FORMS[form]
    result = run(form, fields_fn(8, u=0), regs(0x100C), length=length)
    assert reg(result, "I0") == Const(0x100C)
    event = [e for e in result.trace if e["action"] == "store"][-1]
    assert event["address"] == 0x1014


def test_modifier_larger_than_length_is_one_literal_correction():
    # The manual forbids |M| > L (p.6-26); the table's formula is a single
    # +-L correction, which is what the hardware table prescribes.
    result = run("3a", f3a(), regs(0x1004, 0x11), length=6)
    assert reg(result, "I0") == Const(0x1005)  # 0x1015 - 0x10


def test_second_dag_pm_access_uses_its_own_b_and_l():
    # 1a's PM half: I8/M15 with B8/L8; DAG1's I0 is outside any buffer here.
    r = {"I0": 0x2000, "M1": 1, "I8": 0x400F, "M15": 1, "B8": 0x4000, "L8": 0x10,
         "MODE1": CBUFEN}  # fmt: skip
    f = f1a()
    f["pmm[2:0]"] = 7
    result = run("1a", f, r)
    assert reg(result, "I8") == Const(0x4000)
    assert reg(result, "I0") == Const(0x2001)  # L0 unset: linear


def test_negative_register_modifier_is_signed():
    result = run("3c", f3c(), regs(0x1000, -1), length=4)
    assert reg(result, "I0") == Const(0x100F)


# --- address scaling (byte space, assume_nw32 on) -------------------------


def byte_regs(i, mod, **extra):
    return {"I0": i, "M1": mod & 0xFFFFFFFF, "B0": 0x30001000, "L0": 0x10,
            "MODE1": CBUFEN, **extra}  # fmt: skip


def test_normal_word_byte_space_scales_modifier_and_length():
    # L = 0x10 normal words = 0x40 bytes; M = 4 words = 0x10 bytes.
    result = run("3a", f3a(), byte_regs(0x3000103C, 4), length=6, assume_nw32=True)
    assert reg(result, "I0") == Const(0x3000100C)  # 0x3000104C - 0x40
    result = run("3a", f3a(), byte_regs(0x30001004, -4), length=6, assume_nw32=True)
    assert reg(result, "I0") == Const(0x30001034)


def test_long_word_scales_like_normal_word():
    fields = f3a()
    fields["l"] = 1
    result = run("3a", fields, byte_regs(0x3000103C, 4), length=6, assume_nw32=True)
    assert reg(result, "I0") == Const(0x3000100C)


def test_short_word_byte_space_scales_by_two():
    # 3b short word: (l, x, w) = (1, 0, 0). L = 0x10 shorts = 0x20 bytes.
    fields = f3b()
    fields.update({"l": 1, "x": 0, "w": 0})
    result = run("3b", fields, byte_regs(0x3000101E, 2), assume_nw32=True, length=6)
    assert reg(result, "I0") == Const(0x30001002)  # +4 -> 0x30001022 - 0x20


def test_byte_access_is_unscaled():
    fields = f3b()
    fields.update({"l": 0, "x": 0, "w": 0})
    result = run("3b", fields, byte_regs(0x3000100F, 1), assume_nw32=True, length=6)
    assert reg(result, "I0") == Const(0x30001000)


def test_immediate_forms_scale_length():
    result = run("4a", f4a(4), byte_regs(0x3000103C, 0), assume_nw32=True, length=6)
    assert reg(result, "I0") == Const(0x3000100C)
    # 4b short word immediate 2 -> +4 bytes; L = 0x20 bytes.
    result = run(
        "4b",
        f4b(2, lxw=(1, 0, 0)),
        byte_regs(0x3000101E, 0),
        assume_nw32=True,
        length=6,
    )
    assert reg(result, "I0") == Const(0x30001002)


# --- SIMD / unknowns -------------------------------------------------------


def test_simd_post_modify_wraps_once():
    r = regs(0x100E, 2)
    r["MODE1"] = CBUFEN | PEYEN
    result = run("3a", f3a(), r, length=6)
    assert reg(result, "I0") == Const(0x1000)
    stores = [e for e in result.trace if e["action"] in ("store", "store-pey")]
    assert stores  # the implicit second access was issued without error


def test_unknown_length_or_mode1_stays_linear():
    r = regs(0x100C, 8)
    del r["L0"]
    assert reg(run("3a", f3a(), r, length=6), "I0") == Const(0x1014)
    r = regs(0x100C, 8)
    del r["MODE1"]
    assert reg(run("3a", f3a(), r, length=6), "I0") == Const(0x1014)


def test_non_constant_index_with_active_wrap_is_unknown():
    state = T.State(
        0x10,
        {
            U["I0"]: T.symbol("A0"),
            U["M1"]: Const(1),
            U["B0"]: Const(0x1000),
            U["L0"]: Const(0x10),
            U["MODE1"]: Const(CBUFEN),
        },
    )
    [result] = T._execute(state, Instruction(0, 6, "3a", f3a(), kind="confident"))
    assert isinstance(result.uregs[U["I0"]], Unknown)


def test_bounded_symbol_index_gets_a_fresh_circ_symbol():
    state = T.State(
        0x10,
        {
            U["I0"]: T.symbol("I0e"),
            U["M1"]: Const(1),
            U["B0"]: Const(0x1000),
            U["L0"]: Const(0x10),
            U["MODE1"]: Const(CBUFEN),
        },
    )
    [result] = T._execute(state, Instruction(0, 6, "3a", f3a(), kind="confident"))
    [(name, coeff)] = result.uregs[U["I0"]].terms
    assert name.startswith("circ_") and coeff == 1


def test_helper_matches_wrap_const_within_one_lap():
    for index in range(0x1000, 0x1010):
        for delta in range(-0x10, 0x11):
            assert m._circular_step(
                index, 0x1000, 0x10, delta
            ) == m._circular_wrap_const(index, 0x1000, 0x10, delta)
