"""Textual decoding of compute fields: ALU, multiplier, shifter, multifunction and short forms."""

from __future__ import annotations

import json
import os

from tools.sharc_dev import sharcinv
from tools.sharc_dev.sharc_trace import UREG_NAMES

from .regions import _here

# --- register naming ---------------------------------------------------


def reg_name(code: int, is_float: bool) -> str:
    return ("F%d" if is_float else "R%d") % code


def ureg_name(code: int) -> str:
    if 0 <= code < len(UREG_NAMES):
        return UREG_NAMES[code]
    return "UREG%d" % code


# --- compute-field decode -----------------------------------------------
#
# Bit layouts from the SHARC+ Core Programming Reference, geometrically
# extracted into tools/sharc_dev/sharcspec/compute_table.json's figures_and_bit_layouts
# (Figure 18-1/18-2/18-3, Table 18-11/13/15/16/18/19/21). Opcode->name tables
# reused from tools/sharc_dev/sharcinv.py (built from the same manual's Table
# 18-2/18-5/18-7/18-9 rows) rather than re-derived here.

ALU_OPS = sharcinv.ALU_OPS
SHIFT_OPS = sharcinv.SHIFT_OPS
SHORT_OPS = sharcinv.SHORT_OPS
FLOAT_SHORT_OPS = sharcinv.FLOAT_SHORT_OPS

UNARY_ALU_OPS = {
    "pass",
    "neg",
    "inc",
    "dec",
    "abs",
    "fpass",
    "fneg",
    "frnd",
    "mant",
    "fabs",
    "fix",
    "float",
    "trunc",
    "logb",
    "recips",
    "rsqrts",
    "not",
    "inc_c",
    "dec_c",
}

# Task-flagged decode gaps: opcodes to mark loudly even when a name table
# already carries an entry for them (see module docstring).
GAP_ALU_OPCODES = {0xE0, 0xD9, 0xDA}
GAP_SHIFT_OPCODES = {0xB0}
GAP_MULTIFN_OPCODES = {0x1A, 0x1E, 0x1F}


def _load_multifn_alu_syntax():
    """{opcode_21_16 (6-bit int): syntax string} from
    tools/sharc_dev/sharcspec/compute_table.json's multifn_mul_alu (PGR Table 12-12).

    This is a DIFFERENT numbering from tools/sharc_dev/sharcinv.py's ALU_OPS
    (PRM Table 18-5, the standalone-ALU opcode space): the fused MUL+ALU
    multifunction op's ALU sub-operation is selected by a 6-bit field at
    bits 21:16 of the compute field (cu[1:0] plus the low 4 bits of what
    Figure 18-1 calls opcode[7:4]), not by the 8-bit ALU_OPS opcode space.
    Read once at import time; never re-derived per instruction."""
    path = os.path.join(_here, "sharcspec", "compute_table.json")
    with open(path) as fh:
        table = json.load(fh)["multifn_mul_alu"]
    return {int(row["opcode_21_16"], 2): row["syntax"] for row in table["rows"]}


MULTIFN_ALU_SYNTAX = _load_multifn_alu_syntax()


def _gap_mark(gap: bool) -> str:
    return "  !!GAP!!" if gap else ""


def decode_alu(opcode: int, field23: int, is_dual: bool, is_float: bool):
    if is_dual:
        # cu=0, opcode[19:16]=0111/1111 identifies dual add/subtract; the
        # rest of `opcode` (its low nibble, bits 15:12) is Rs/Fs, not an
        # ALU_OPS opcode (see tools/sharc_dev/sharcinv.py's classify_compute), so no
        # name lookup applies -- structurally fixed, never a gap.
        # Table 18-13: Rs/Fs 15:12, Ra/Fa 11:8, Rx/Fx 7:4, Ry/Fy 3:0.
        rs, ra, rx, ry = ((field23 >> sh) & 0xF for sh in (12, 8, 4, 0))
        Rs, Ra, Rx, Ry = (reg_name(r, is_float) for r in (rs, ra, rx, ry))
        text = "%s = %s + %s; %s = %s - %s" % (Ra, Rx, Ry, Rs, Rx, Ry)
        return text, False
    name = ALU_OPS.get(opcode)
    gap = opcode in GAP_ALU_OPCODES or name is None
    label = name or "alu?0x%02x" % opcode
    # Table 18-11: RN/FN 11:8, Rx/Fx 7:4, Ry/Fy 3:0.
    rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
    Rn, Rx, Ry = (reg_name(r, is_float) for r in (rn, rx, ry))
    if name in UNARY_ALU_OPS:
        text = "%s = %s(%s)" % (Rn, label, Rx)
    else:
        text = "%s = %s(%s, %s)" % (Rn, label, Rx, Ry)
    return text + _gap_mark(gap), gap


def _mod1_label(opcode: int) -> str:
    """PRM Table 18-7 / 'MOD1 Encode Table' (p.428-430;
    tools/sharc_dev/sharcspec/compute_table.json's mulop_32_40bit/mod1_table): for a
    fixed-point RN=RX*RY or MAC, opcode bit 4 is the X operand's sign
    (1=signed, 0=unsigned), bit 5 is Y's, bit 3 selects an Integer (0) or
    Fraction (1) result, and bit 0 selects Rounded (only meaningful when
    bit 3 is set) -- e.g. 0x48 (y=0,x=0,frac=1,rnd=0) is "UUF". The
    non-rounded Fraction forms return the high word of the double-length
    product; Integer forms do not."""
    x_sign = "S" if (opcode >> 4) & 1 else "U"
    y_sign = "S" if (opcode >> 5) & 1 else "U"
    fraction = bool((opcode >> 3) & 1)
    fmt = "F" if fraction else "I"
    rounded = "R" if fraction and (opcode & 1) else ""
    return x_sign + y_sign + fmt + rounded


def decode_mult(
    opcode: int,
    field23: int,
    is_float: bool,
    housekeeping: bool,
    is_mac: bool,
    is_plain_mul: bool,
):
    # Table 18-11 register positions apply here too (single-function compute).
    # cu/opcode classification (housekeeping/plain-mul/MAC, including the
    # opcode==0x30 "FN = FX*FY" special case) comes from
    # tools/sharc_dev/sharcinv.py's classify_compute() -- PRM Table 18-7 -- not
    # recomputed here, so this always agrees with the feature-vector counts.
    #
    # classify_compute()'s is_float is wrong for every MULT opcode except
    # 0x30: bit 3 of the mulop opcode (the "01yx f00r"/"10yx.../11yx..."
    # families) is MOD1's Integer(0)/Fraction(1) result-format bit for a
    # fixed-point multiply or MAC, not a float/fixed switch -- opcode 0x30
    # ("FN = FX*FY") is the only real floating-point row in this space
    # (mulop_32_40bit in tools/sharc_dev/sharcspec/compute_table.json). Recompute it
    # here rather than trust the passed-in flag.
    is_float = opcode == 0x30
    rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
    Rn, Rx, Ry = (reg_name(r, is_float) for r in (rn, rx, ry))
    if housekeeping:
        return "MR housekeeping (opcode 0x%02x)" % opcode, False
    if is_plain_mul:
        if opcode == 0x30:
            return "%s = %s * %s" % (Rn, Rx, Ry), False
        mod1 = _mod1_label(opcode)
        hw = ", high word" if "F" in mod1 else ""
        return "%s = %s * %s  (MOD1 %s%s)" % (Rn, Rx, Ry, mod1, hw), False
    if is_mac:
        top2 = (opcode >> 6) & 3
        op = "MR + %s * %s  (MAC add)" if top2 == 2 else "MR - %s * %s  (MAC sub)"
        mod1 = _mod1_label(opcode)
        hw = ", high word" if "F" in mod1 else ""
        return ("%s = " + op + "  (MOD1 %s%s)") % (Rn, Rx, Ry, mod1, hw), False
    return "mult?0x%02x(%s, %s) -> %s  !!GAP!!" % (opcode, Rx, Ry, Rn), True


def decode_shift(opcode: int, field23: int):
    name = SHIFT_OPS.get(opcode)
    gap = opcode in GAP_SHIFT_OPCODES or name is None
    label = name or "shift?0x%02x" % opcode
    rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
    text = "R%d = %s(R%d, R%d)" % (rn, label, rx, ry)
    return text + _gap_mark(gap), gap


_MULTIFN_SUBS_ORDER = (
    "R3-0",
    "F3-0",
    "R7-4",
    "F7-4",
    "RXA",
    "FXA",
    "RYA",
    "FYA",
    "RM",
    "FM",
    "RA",
    "FA",
)


def _render_multifn_alu_from_table(field23: int):
    """MUL+ALU multifunction ALU sub-op, decoded from PGR Table 12-12's own
    syntax rows (see MULTIFN_ALU_SYNTAX) rather than tools/sharc_dev/sharcinv.py's
    standalone ALU_OPS -- a different numbering (see that table's loader).
    -> rendered text, or None if the 6-bit selector isn't one of its rows
    (reserved/undefined -- a real gap, not a lookup miss)."""
    selector = (field23 >> 16) & 0x3F
    syntax = MULTIFN_ALU_SYNTAX.get(selector)
    if syntax is None:
        return None
    rya = (field23 & 3) + 12
    rxa = ((field23 >> 2) & 3) + 8
    rym = ((field23 >> 4) & 3) + 4
    rxm = ((field23 >> 6) & 3) + 0
    ra = (field23 >> 8) & 0xF
    rm = (field23 >> 12) & 0xF
    values = {
        "R3-0": rxm,
        "F3-0": rxm,
        "R7-4": rym,
        "F7-4": rym,
        "RXA": rxa,
        "FXA": rxa,
        "RYA": rya,
        "FYA": rya,
        "RM": rm,
        "FM": rm,
        "RA": ra,
        "FA": ra,
    }
    text = syntax
    for token in _MULTIFN_SUBS_ORDER:
        text = text.replace(token, "%s%d" % (token[0], values[token]))
    return text


def decode_multifn(top3: int, opcode: int, field23: int):
    # Table 18-18/19 (MUL+dual-add/sub, bits 19:0: Rs/Fs 19:16 then the
    # MUL+ALU 16-bit layout below) -- both its rows (fixed/float) are a
    # single fixed form each (no further opcode sub-table), so this needs
    # no lookup. The four INPUT operands are 2-bit fields restricted to a
    # fixed quad (register_operand_encoding.restricted_2bit_fields): Rxm in
    # R0-3, Rym in R4-7, Rxa in R8-11, Rya in R12-15.
    is_float = bool(top3 & 1)
    is_dual = top3 in (6, 7)
    if is_dual:
        rya = (field23 & 3) + 12
        rxa = ((field23 >> 2) & 3) + 8
        rym = ((field23 >> 4) & 3) + 4
        rxm = ((field23 >> 6) & 3) + 0
        ra = (field23 >> 8) & 0xF
        rm = (field23 >> 12) & 0xF
        rs = (field23 >> 16) & 0xF
        parts = [
            "%s = %s * %s"
            % (
                reg_name(rm, is_float),
                reg_name(rxm, is_float),
                reg_name(rym, is_float),
            ),
            "%s = %s + %s"
            % (
                reg_name(ra, is_float),
                reg_name(rxa, is_float),
                reg_name(rya, is_float),
            ),
            "%s = %s - %s"
            % (
                reg_name(rs, is_float),
                reg_name(rxa, is_float),
                reg_name(rya, is_float),
            ),
        ]
        return "; ".join(parts), False
    text = _render_multifn_alu_from_table(field23)
    if text is not None:
        gap = ((field23 >> 16) & 0x3F) in GAP_MULTIFN_OPCODES
        return text + _gap_mark(gap), gap
    return "multifn_alu?0x%02x(field23=0x%06x)" % (opcode, field23) + _gap_mark(
        True
    ), True


def render_compute(field23: int):
    """-> (mnemonic or None, gap_flag) for a 23-bit compute field, using
    tools/sharc_dev/sharcinv.py's classify_compute() to identify cu/opcode and the
    Figure/Table bit layouts above for the register operands."""
    if not field23:
        return None, False
    cu, d = sharcinv.classify_compute(field23)
    opcode = d.get("opcode", 0)
    if cu == "ALU":
        return decode_alu(
            opcode, field23, d.get("is_dual_addsub", False), d.get("is_float", False)
        )
    if cu == "MULT":
        return decode_mult(
            opcode,
            field23,
            d.get("is_float", False),
            d.get("housekeeping", False),
            d.get("is_mac", False),
            d.get("is_plain_mul", False),
        )
    if cu == "SHIFT":
        return decode_shift(opcode, field23)
    if cu == "MULTIFN":
        top3 = (field23 >> 20) & 7
        return decode_multifn(top3, opcode, field23)
    return "cu?%r opcode=0x%02x" % (cu, opcode), False


_BINARY_SHORT_OPS = {0x0, 0x1, 0x3, 0x7, 0x8, 0x9, 0xB, 0xC, 0xD, 0xE, 0xF}
# PRM Table 17-2/18-21 (p.17-3, ShortCompute Opcode table): opcode 0101 is
# "RN = RN + 1 -> RN = RX + 1" and 0110 is "RN = RN - 1 -> RN = RX - 1" --
# the leading "RN = RN +/- 1" is the *destination* column's generic
# 2-operand form, folded to its concrete instruction in the same row's
# right-hand "Instruction" column, which reads RX (not RN) and adds/
# subtracts the literal 1. inc/dec never read RN at all; they are unary-RX
# opcodes like pass/not/float, not "operate on RN itself" -- matching
# tools/sharc_dev/sharc_trace.py's short=True "increment"/"decrement" rows, which
# already compute from `right` (the RX value), not `left` (RN).
_UNARY_RX_SHORT_OPS = {0x2, 0x4, 0x5, 0x6, 0xA}  # pass, not, inc, dec, float


def render_shortcompute(field12: int):
    """Type 2c: Figure 18-2, opcode 11:8, RN 7:4, RX 3:0 (RN is the result
    for every opcode; PRM Table 17-2 gives inc/dec (opcode 5/6) as
    "RN = RX + 1" / "RN = RX - 1" -- RX is the only source read, RN is
    write-only, same as the pass/not/float unary-RX opcodes)."""
    opcode = (field12 >> 8) & 0xF
    rn = (field12 >> 4) & 0xF
    rx = field12 & 0xF
    is_float = opcode in FLOAT_SHORT_OPS
    name = SHORT_OPS.get(opcode, "short?0x%x" % opcode)
    Rn, Rx = reg_name(rn, is_float), reg_name(rx, is_float)
    if opcode in _UNARY_RX_SHORT_OPS:
        return "%s = %s(%s)" % (Rn, name, Rx)
    return "%s = %s(%s, %s)" % (Rn, name, Rn, Rx)
