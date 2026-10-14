"""Register-form shifter opcodes (cu=2) of tools/sharc_core/compute_shift.py:
fext, fext (se), fdep, fdep (se), OR-fdep, OR-fdep (se), rot, OR-ashift, exp,
exp (ex), funpack, bitdep and bitext (nu) with the count in a register.

Expected values are worked by hand from the SHARC+ PRM (out/refs/sharc-plus-prm,
Table 18-9 pp.431-433, Bit Field Manipulation p.3-17, instruction pages
23-3..23-19) and not produced by calling the code under test. bit6 = Ry[5:0],
len6 = Ry[11:6].
"""

import os
import sys
from importlib import import_module

from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(str(REPO_ROOT), "tools"))
T = import_module("sharc_trace")
encoding = import_module("sharc_core.encoding")

ASTATX = encoding.UREG_CODES["ASTATX"]


def ry(bit6, len6):
    return (len6 << 6) | bit6


def run(opcode, left, right, rn_old=0, special=None, astatx=None):
    """_compute for RN(R0) = op(RX(R1), RY(R2)); returns the result tuple and
    a function giving a flag's known value after the instruction."""
    field = (2 << 20) | (opcode << 12) | (0 << 8) | (1 << 4) | 2
    f = {"compute[22:16]": field >> 16, "compute[15:0]": field & 0xFFFF}
    values = {0: T.Const(rn_old), 1: left, 2: right}
    if astatx is not None:
        values[ASTATX] = T.Const(astatx)
    result = T._compute(f, False, values, special)
    state = result[3](T.Unknown("start"))

    def flag(bit):
        return T._astatx_known_bit(state, bit)

    return result, flag


def Cn(x):
    return T.Const(x)


# --- fext ------------------------------------------------------------------


def test_fext_issue_case():
    # R13 = fext(R13, R4) with bit6 = 4, len6 = 8 on 0x1234abcd: bits 11..4
    # are 0xbc (0x1234abcd >> 4 = 0x01234abc; & 0xff = 0xbc).
    result, flag = run(0x40, Cn(0x1234ABCD), Cn(ry(4, 8)))
    assert result[1] == Cn(0xBC)
    assert result[2] == "field-extract"
    assert flag(T.SS_BIT) is False and flag(T.SZ_BIT) is False
    assert flag(T.SV_BIT) is False  # 4 + 8 <= 32


def test_fext_len_zero_is_zero_and_sets_sz():
    result, flag = run(0x40, Cn(0xFFFFFFFF), Cn(ry(5, 0)))
    assert result[1] == Cn(0)
    assert flag(T.SZ_BIT) is True and flag(T.SV_BIT) is False


def test_fext_bit6_at_or_above_32_reads_zero_and_sets_sv():
    result, flag = run(0x40, Cn(0xFFFFFFFF), Cn(ry(32, 4)))
    assert result[1] == Cn(0)
    assert flag(T.SZ_BIT) is True and flag(T.SV_BIT) is True
    result, flag = run(0x40, Cn(0xFFFFFFFF), Cn(ry(63, 63)))
    assert result[1] == Cn(0) and flag(T.SV_BIT) is True


def test_fext_span_over_32_sets_sv_and_zero_fills_the_top():
    # bit6 = 28, len6 = 8: bits 35..28; only 31..28 exist (0x1), rest read 0.
    result, flag = run(0x40, Cn(0x1234ABCD), Cn(ry(28, 8)))
    assert result[1] == Cn(0x1)
    assert flag(T.SV_BIT) is True


def test_fext_len_32_whole_word_and_len_over_32_saturates_to_the_word():
    assert run(0x40, Cn(0x80000001), Cn(ry(0, 32)))[0][1] == Cn(0x80000001)
    assert run(0x40, Cn(0x80000001), Cn(ry(0, 40)))[0][1] == Cn(0x80000001)


def test_fext_unknown_operands():
    result, flag = run(0x40, T.Unknown("x"), Cn(ry(4, 8)))
    assert isinstance(result[1], T.Unknown)
    assert flag(T.SZ_BIT) is None and flag(T.SV_BIT) is False
    assert flag(T.SS_BIT) is False
    result, flag = run(0x40, Cn(1), T.Unknown("y"))
    assert isinstance(result[1], T.Unknown)
    assert flag(T.SZ_BIT) is None and flag(T.SV_BIT) is None
    assert flag(T.SS_BIT) is False


# --- fext (se) -------------------------------------------------------------


def test_fext_se_negative_field_sign_extends():
    # bits 11..4 of 0x1234abcd = 0xbc, MSB (bit 7 of the field) set.
    result, flag = run(0x48, Cn(0x1234ABCD), Cn(ry(4, 8)))
    assert result[1] == Cn(0xFFFFFFBC)
    assert result[2] == "field-extract-se"
    assert flag(T.SZ_BIT) is False and flag(T.SV_BIT) is False


def test_fext_se_positive_field_and_len_zero_and_off_scale():
    assert run(0x48, Cn(0x1234ABCD), Cn(ry(8, 8)))[0][1] == Cn(0xAB | 0xFFFFFF80)
    assert run(0x48, Cn(0x1234ABCD), Cn(ry(0, 4)))[0][1] == Cn(0xFFFFFFFD)
    assert run(0x48, Cn(0x7FFFFFFF), Cn(ry(0, 0)))[0][1] == Cn(0)
    # Field MSB lies above bit 31: no sign extension (bit6 = 28, len6 = 8).
    result, flag = run(0x48, Cn(0xF0000000), Cn(ry(28, 8)))
    assert result[1] == Cn(0xF) and flag(T.SV_BIT) is True


# --- fdep ------------------------------------------------------------------


def test_fdep_places_field_and_clears_the_rest():
    # Low 8 bits of 0x1234abcd (0xcd) deposited at bit 4.
    result, flag = run(0x44, Cn(0x1234ABCD), Cn(ry(4, 8)), rn_old=0xFFFFFFFF)
    assert result[1] == Cn(0xCD0)
    assert result[2] == "field-deposit"
    assert flag(T.SZ_BIT) is False and flag(T.SV_BIT) is False


def test_fdep_len_zero_clears_and_overflow_sets_sv():
    result, flag = run(0x44, Cn(0xFFFFFFFF), Cn(ry(3, 0)), rn_old=0xFFFFFFFF)
    assert result[1] == Cn(0) and flag(T.SZ_BIT) is True
    # 8 bits at bit 28: only 4 fit.
    result, flag = run(0x44, Cn(0xFF), Cn(ry(28, 8)))
    assert result[1] == Cn(0xF0000000) and flag(T.SV_BIT) is True
    result, flag = run(0x44, Cn(0xFF), Cn(ry(40, 8)))
    assert result[1] == Cn(0) and flag(T.SV_BIT) is True


def test_fdep_se_sign_extends_from_field_msb():
    # Field 0xcd (MSB set) at bit 4: sign bits fill 31..12.
    result, flag = run(0x4C, Cn(0xCD), Cn(ry(4, 8)))
    assert result[1] == Cn(0xFFFFFCD0)
    assert result[2] == "field-deposit-se"
    # Positive field 0x4d: no fill.
    assert run(0x4C, Cn(0x4D), Cn(ry(4, 8)))[0][1] == Cn(0x4D0)
    # Field MSB off-scale left: no fill (the field's visible bits only).
    assert run(0x4C, Cn(0xFF), Cn(ry(28, 8)))[0][1] == Cn(0xF0000000)


def test_or_fdep_ors_into_old_rn():
    result, flag = run(0x64, Cn(0xCD), Cn(ry(4, 8)), rn_old=0x0000F00F)
    assert result[1] == Cn(0x0000FCDF)
    assert result[2] == "field-deposit-or"
    result, _ = run(0x64, Cn(0xCD), Cn(ry(4, 0)), rn_old=0x1234)
    assert result[1] == Cn(0x1234)


def test_or_fdep_se_ors_sign_extended_field():
    result, flag = run(0x6C, Cn(0xCD), Cn(ry(4, 8)), rn_old=0x0000000F)
    assert result[1] == Cn(0xFFFFFCDF)
    assert result[2] == "field-deposit-or-se"
    assert flag(T.SZ_BIT) is False


# --- rot -------------------------------------------------------------------


def test_rot_left_right_and_flags():
    result, flag = run(0x08, Cn(0x80000001), Cn(1))
    assert result[1] == Cn(0x00000003) and result[2] == "rotate"
    assert flag(T.SS_BIT) is False and flag(T.SV_BIT) is False
    assert flag(T.SZ_BIT) is False
    # -1 (0xFFFFFFFF) rotates right by one.
    assert run(0x08, Cn(0x00000003), Cn(0xFFFFFFFF))[0][1] == Cn(0x80000001)
    assert run(0x08, Cn(0x12345678), Cn(0))[0][1] == Cn(0x12345678)
    assert run(0x08, Cn(0x12345678), Cn(32))[0][1] == Cn(0x12345678)
    assert run(0x08, Cn(0x12345678), Cn(4))[0][1] == Cn(0x23456781)
    result, flag = run(0x08, Cn(0), Cn(7))
    assert result[1] == Cn(0) and flag(T.SZ_BIT) is True


# --- OR-ashift -------------------------------------------------------------


def test_or_ashift_register():
    result, flag = run(0x24, Cn(0x80000000), Cn(0xFFFFFFFC), rn_old=0x1)
    # ashift right 4 of 0x80000000 = 0xf8000000, OR old R0.
    assert result[1] == Cn(0xF8000001) and result[2] == "arithmetic-shift-or"
    assert flag(T.SZ_BIT) is False and flag(T.SV_BIT) is False
    assert flag(T.SS_BIT) is None  # OR-ashift has no SS line (see handler)
    result, flag = run(0x24, Cn(0x1), Cn(4), rn_old=0x2)
    assert result[1] == Cn(0x12) and flag(T.SV_BIT) is True
    assert run(0x24, Cn(0x1), Cn(40), rn_old=0x2)[0][1] == Cn(0x2)
    assert run(0x24, Cn(0x80000000), Cn(0xFFFFFF00 | 0xC0), rn_old=0)[0][1] == Cn(
        0xFFFFFFFF
    )


# --- exp -------------------------------------------------------------------


def test_exp_counts_leading_sign_bits_minus_one_negated():
    cases = [
        (0x40000000, 0),
        (0x20000000, -1),
        (0x00000001, -30),
        (0x00000000, -31),
        (0xFFFFFFFF, -31),
        (0x80000000, 0),
        (0xC0000000, -1),
        (0x7FFFFFFF, 0),
    ]
    for source, exponent in cases:
        result, flag = run(0x80, Cn(source), Cn(0))
        assert result[1] == Cn(exponent & 0xFFFFFFFF), hex(source)
        assert result[2] == "exp"
        assert flag(T.SZ_BIT) is (exponent == 0)
        assert flag(T.SV_BIT) is False
        assert flag(T.SS_BIT) is bool(source & 0x80000000)


def test_exp_ex_uses_av():
    av = 1 << encoding.AV_BIT
    # AV clear: as exp; SS = bit31 xor AV.
    result, flag = run(0x84, Cn(0x20000000), Cn(0), astatx=0)
    assert result[1] == Cn(0xFFFFFFFF) and flag(T.SS_BIT) is False
    # AV set: +1, SS = bit31 xor 1.
    result, flag = run(0x84, Cn(0x20000000), Cn(0), astatx=av)
    assert result[1] == Cn(1)
    assert flag(T.SS_BIT) is True and flag(T.SZ_BIT) is False
    result, flag = run(0x84, Cn(0x80000000), Cn(0), astatx=av)
    assert result[1] == Cn(1) and flag(T.SS_BIT) is False
    # AV unknown: result and SS unknown.
    result, flag = run(0x84, Cn(0x20000000), Cn(0))
    assert isinstance(result[1], T.Unknown)
    assert flag(T.SS_BIT) is None and flag(T.SV_BIT) is False


# --- funpack ---------------------------------------------------------------


def test_funpack_normal_and_denormal_and_sign():
    # Short exp field e -> single exponent: e=8: top=1 -> 1 0000 000 = 128.
    assert run(0x94, Cn(0x4000), Cn(0))[0][1] == Cn(128 << 23)
    # e=7: top=0 -> 0 1111 111 = 127 (1.0), frac 0x400 -> 1.5.
    assert run(0x94, Cn((7 << 11) | 0x400), Cn(0))[0][1] == Cn(
        (127 << 23) | (0x400 << 12)
    )
    # e=1: 0 1111 001 = 121, sign set.
    assert run(0x94, Cn(0x8000 | (1 << 11)), Cn(0))[0][1] == Cn((1 << 31) | (121 << 23))
    # e=15: top=1 -> 1 0000 111 = 135.
    assert run(0x94, Cn(15 << 11), Cn(0))[0][1] == Cn(135 << 23)
    # e=0, fraction 0x400 (N=0): exponent 120, mantissa 0.
    assert run(0x94, Cn(0x400), Cn(0))[0][1] == Cn(120 << 23)
    # e=0, fraction 0x001 (N=10): exponent 110, mantissa (1<<11)&0x7ff = 0.
    assert run(0x94, Cn(0x001), Cn(0))[0][1] == Cn(110 << 23)
    # e=0, fraction 0x600 (N=0): exponent 120, mantissa (0x600 << 1) & 0x7ff = 0x400.
    assert run(0x94, Cn(0x600), Cn(0))[0][1] == Cn((120 << 23) | (0x400 << 12))
    # Upper half of RX is ignored.
    assert run(0x94, Cn(0xABCD0000 | 0x4000), Cn(0))[0][1] == Cn(128 << 23)
    result, flag = run(0x94, Cn(0x4000), Cn(0))
    assert result[2] == "funpack"
    assert flag(T.SS_BIT) is False and flag(T.SZ_BIT) is False
    assert flag(T.SV_BIT) is False


# --- bit FIFO register forms ----------------------------------------------


def test_bitdep_then_bitext_register_forms():
    special = {"BFFWRP": Cn(0), "BFF_HI": Cn(0), "BFF_LO": Cn(0)}
    # BITDEP R1 by R2: deposit 8 bits of 0xAB at the top of the FIFO.
    result, flag = run(0x74, Cn(0xAB), Cn(8), special=special)
    assert result[0] == ("BFFWRP", "BFF_HI", "BFF_LO")
    wrp, hi, lo = result[1]
    assert wrp == Cn(8) and hi == Cn(0xAB000000) and lo == Cn(0)
    assert flag(T.SF_BIT) is False and flag(T.SV_BIT) is False
    special = {"BFFWRP": wrp, "BFF_HI": hi, "BFF_LO": lo}
    # RN = bitext R1 (count = low 6 bits of R1 = 4): top nibble 0xA.
    result, flag = run(0x50, Cn(4), Cn(0), special=special)
    names, values = result[0], result[1]
    assert names == (0, "BFFWRP", "BFF_HI", "BFF_LO")
    assert values[0] == Cn(0xA) and values[1] == Cn(4)
    assert values[2] == Cn(0xB0000000)
    # (nu): value only, FIFO untouched.
    result, _ = run(0x58, Cn(4), Cn(0), special=special)
    assert result[0] == 0 and result[1] == Cn(0xA) and result[2] == "bit-extract-nu"
    # Count above 32 sets SV.
    result, flag = run(0x58, Cn(33), Cn(0), special=special)
    assert flag(T.SV_BIT) is True


def test_bit_fifo_register_forms_with_unknown_count_are_unknown():
    result, flag = run(0x74, Cn(1), T.Unknown("n"))
    assert all(isinstance(v, T.Unknown) for v in result[1])
    result, flag = run(0x50, T.Unknown("n"), Cn(0))
    assert all(isinstance(v, T.Unknown) for v in result[1])
    assert flag(T.SS_BIT) is False
