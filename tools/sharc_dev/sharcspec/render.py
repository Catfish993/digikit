#!/usr/bin/env python3
"""Readable operand/mnemonic rendering for the SHARC+ decoder.

Compute-field mnemonics come from compute_table.json (public-doc-derived,
see its "sources" block: PRM ch.18/25, PGR ch.12). This module is clean-room
w.r.t. any vendor disassembler: it only consumes the two JSON tables
(decode_table.json via sharc_decode.py, and compute_table.json here) and
re-derives readable syntax from their documented bit layouts.
"""
import json
import os
import re

from tools.sharc_dev.sharcspec.sharc_decode import Decoder, linear

HERE = os.path.dirname(os.path.abspath(__file__))
_CT = json.load(open(os.path.join(HERE, "compute_table.json")))


def reg(n, float_=False):
    return f"F{n}" if float_ else f"R{n}"


def _nospace(s):
    return s.replace(" ", "")


def _rows(key):
    return _CT[key]["rows"]


# -- Single-function ALU (Table 18-5 + Table 18-6, merged on the 8-bit opcode) --
ALUOP = {}
for r in _rows("aluop_32_40bit"):
    ALUOP[_nospace(r["opcode"])] = r["syntax"]
for r in _rows("aluop_64bit"):
    ALUOP[_nospace(r["opcode"])] = r["syntax"]

# -- Single-function Multiplier (Table 18-7 has wildcard letters F/x/y/r; Table 18-8 is exact) --
MULOP_64 = {_nospace(r["opcode"]): r["syntax"] for r in _rows("mulop_64bit")}
MULOP_32_PATTERNS = [(_nospace(r["opcode"]), r["syntax"]) for r in _rows("mulop_32_40bit")]

# -- Single-function Shifter / ShiftImm (Table 18-9): one row list, two keys --
SHIFTOP = {}
SHIFTIMM = {}
for r in _rows("shiftop_shiftimm"):
    SHIFTOP[r["shiftop_8bit"]] = r["syntax"]
    if r["shiftimm_6bit"] is not None:
        SHIFTIMM[r["shiftimm_6bit"]] = r["syntax"]

# -- Dual add/subtract (cu=00 ALU, opcode[19:16] only, Table 18-10) --
DUALADDSUB = {_nospace(r["opcode_19_16"]): r["syntax"] for r in _rows("dual_add_subtract")}

# -- Multifunction MUL/ALU (opcode[21:16], PGR Table 12-12) --
MULTIFN_MULALU = {_nospace(r["opcode_21_16"]): r["syntax"] for r in _rows("multifn_mul_alu")}

# -- Multifunction MUL dual add/sub: only top 2 bits (10=fixed,11=float) are a real
# selector; the low 4 bits are the Rs register field, not opcode bits (see PRM ch.25). --
MULTIFN_DUALADDSUB = {"10": _rows("multifn_mul_dual_addsub")[0]["syntax"],
                       "11": _rows("multifn_mul_dual_addsub")[1]["syntax"]}

# -- ShortCompute (Type 2c, Table opcode[11:8]) --
SHORTCOMPUTE = {_nospace(r["opcode_11_8"]): r["syntax"] for r in _rows("shortcompute")}

# -- MRDATAMOVE (PRM Table 18-29 + PGR-only top bits, flagged "gap" in compute_table.json) --
MRDATAMOVE = {_nospace(r["opcode_15_12"]): r["mr_register"] for r in _rows("mrdatamove")}


def _wildcard_match(pattern, bits):
    """Match an 8-bit binary string against a Table 18-7 style pattern where any
    non-0/1 character (F, x, y, r, ...) is a don't-care."""
    if len(pattern) != len(bits):
        return False
    return all(p == b for p, b in zip(pattern, bits) if p in "01")


def _match_mulop32(opbits):
    best, best_score = None, -1
    for pat, syn in MULOP_32_PATTERNS:
        if _wildcard_match(pat, opbits):
            score = sum(1 for c in pat if c in "01")
            if score > best_score:
                best, best_score = syn, score
    return best


def _subst(text, mapping):
    """Replace whole-token placeholders (RN, RX, R3-0, ...) with concrete operands.
    Longest keys first so e.g. 'RXA' isn't clobbered by a 'RX' rule."""
    for key in sorted(mapping, key=len, reverse=True):
        text = re.sub(r"\b" + re.escape(key) + r"\b", mapping[key], text)
    return text


def _render_singlefn(field23):
    cu = (field23 >> 20) & 0x3
    opcode = (field23 >> 12) & 0xFF
    rn = (field23 >> 8) & 0xF
    rx = (field23 >> 4) & 0xF
    ry = field23 & 0xF
    opbits = f"{opcode:08b}"
    regmap = {"RN": reg(rn), "RX": reg(rx), "RY": reg(ry),
              "FN": reg(rn, True), "FX": reg(rx, True), "FY": reg(ry, True)}

    if cu == 0b00:  # ALU
        top4 = opbits[:4]
        if top4 in DUALADDSUB:  # Dual add/subtract: low opcode nibble is really Rs
            rs = opcode & 0xF
            m = dict(regmap, RS=reg(rs), FS=reg(rs, True), RA=reg(rn), FA=reg(rn, True))
            return _subst(DUALADDSUB[top4], m)
        syntax = ALUOP.get(opbits)
    elif cu == 0b01:  # Multiplier
        syntax = MULOP_64.get(opbits) or _match_mulop32(opbits)
    elif cu == 0b10:  # Shifter
        syntax = SHIFTOP.get(opbits)
        if syntax is not None:
            return _subst(re.sub(r"\b\w+\|[\w:]+\b", reg(ry), syntax), regmap)
    else:
        syntax = None

    if syntax is None:
        return f"compute(0x{field23:06x})"
    return _subst(syntax, regmap)


def _quad_map(field23, prefix):
    """Register-quad substitution for multifunction MUL/ALU-family ops
    (Table 18-15/18-16/18-18/18-19): Rm/Ra full 4-bit, the 4 inputs are 2-bit
    indices into fixed quads R0-3/R4-7/R8-11/R12-15."""
    rm = (field23 >> 12) & 0xF
    ra = (field23 >> 8) & 0xF
    rxm = (field23 >> 6) & 0x3
    rym = (field23 >> 4) & 0x3
    rxa = (field23 >> 2) & 0x3
    rya = field23 & 0x3
    is_f = prefix == "F"
    return {
        f"{prefix}M": reg(rm, is_f), f"{prefix}A": reg(ra, is_f),
        "R3-0": reg(rxm, is_f), "R7-4": reg(4 + rym, is_f),
        "RXA": reg(8 + rxa, is_f), "RYA": reg(12 + rya, is_f),
        "F3-0": reg(rxm, True), "F7-4": reg(4 + rym, True),
        "FXA": reg(8 + rxa, True), "FYA": reg(12 + rya, True),
    }


def _render_multifn(field23):
    opcode6 = (field23 >> 16) & 0x3F
    obits = f"{opcode6:06b}"

    # MRDATAMOVE: opcode_21_16 in {000000,000001}; low bit is D, then opcode[15:12], rn[11:8].
    if opcode6 >> 1 == 0:
        d = opcode6 & 1
        mrop = (field23 >> 12) & 0xF
        rn = (field23 >> 8) & 0xF
        mrreg = MRDATAMOVE.get(f"{mrop:04b}")
        if mrreg is not None:
            return f"{mrreg} = {reg(rn)}" if d else f"{reg(rn)} = {mrreg}"

    top2 = obits[:2]
    if top2 in MULTIFN_DUALADDSUB:
        rs = (field23 >> 16) & 0xF
        is_f = top2 == "11"
        m = _quad_map(field23, "F" if is_f else "R")
        m["RS"] = reg(rs, is_f)
        m["FS"] = reg(rs, True)
        return _subst(MULTIFN_DUALADDSUB[top2], m)

    syntax = MULTIFN_MULALU.get(obits)
    if syntax is None:
        return f"compute(0x{field23:06x})"
    is_f = syntax.split()[0] == "FM"
    m = _quad_map(field23, "F" if is_f else "R")
    return _subst(syntax, m)


def _render_short(field12):
    opcode = (field12 >> 8) & 0xF
    rn = (field12 >> 4) & 0xF
    rx = field12 & 0xF
    syntax = SHORTCOMPUTE.get(f"{opcode:04b}")
    if syntax is None:
        return f"compute(0x{field12:03x})"
    return _subst(syntax, {"RN": reg(rn), "RX": reg(rx), "FN": reg(rn, True), "FX": reg(rx, True)})


def _render_shiftimm(field23):
    shiftop6 = (field23 >> 16) & 0x3F
    data8 = (field23 >> 8) & 0xFF
    rn = (field23 >> 4) & 0xF
    rx = field23 & 0xF
    syntax = SHIFTIMM.get(f"{shiftop6:06b}")
    if syntax is None:
        return f"compute(0x{field23:06x})"
    text = re.sub(r"\b\w+\|[\w:]+\b", f"0x{data8:02x}", syntax)
    return _subst(text, {"RN": reg(rn), "RX": reg(rx), "FN": reg(rn, True), "FX": reg(rx, True)})


def render_compute(field23, variant="compute"):
    """Decode a compute field into a readable mnemonic string.

    variant: "compute" (23-bit Compute field, Figure 18-1), "short" (12-bit
    ShortCompute sub-field of Type2c), "shiftimm" (23-bit ShiftImm, Figure 18-3).
    """
    if variant == "short":
        return _render_short(field23)
    if variant == "shiftimm":
        return _render_shiftimm(field23)
    if (field23 >> 22) & 1:
        return _render_multifn(field23)
    return _render_singlefn(field23)


_SPLIT_RE = re.compile(r"^(\w+)\[(\d+):(\d+)\]$")
_BASE_RE = re.compile(r"^(\w+)")


def _render_field(base, val, form, raw_label=None):
    if base == "compute":
        variant = "short" if form["width"] == 16 else "compute"
        return render_compute(val, variant)
    if base == "shiftimm":
        return render_compute(val, "shiftimm")
    if base == "addr":
        return f"addr=0x{val:x}"
    if base == "reladdr":
        return f"reladdr(pc-rel)=0x{val:x}"
    if base == "ureg":
        return f"ureg{val}"
    if base == "dreg":
        return f"{reg(val)}"
    if base == "cond":
        return f"cond{val}"
    if base == "data":
        return f"0x{val:x}"
    return f"{raw_label or base}={val}"


def render_operands(frame, form):
    """Render every non-compute (and compute) operand field of a decoded
    instruction into a compact, comma-joined string."""
    values = Decoder.fields(frame, form)

    # Find contiguous hi/lo split pairs (e.g. "compute[22:16]" + "compute[15:0]",
    # "addr[23:16]" + "addr[15:0]") so they can be reassembled into one value.
    groups = {}
    for fl in form["fields"]:
        m = _SPLIT_RE.match(fl["label"])
        if m:
            base, hi, lo = m.group(1), int(m.group(2)), int(m.group(3))
            groups.setdefault(base, []).append((hi, lo, fl["label"]))
    pair_hi = {}
    pair_skip = set()
    for base, parts in groups.items():
        if len(parts) == 2:
            parts.sort(key=lambda p: -p[0])
            (hi1, lo1, l1), (hi2, lo2, l2) = parts
            if lo1 == hi2 + 1:
                pair_hi[l1] = (base, l2, hi2 - lo2 + 1)
                pair_skip.add(l2)

    out = []
    for fl in form["fields"]:
        lbl = fl["label"]
        if lbl in pair_skip:
            continue
        if lbl in pair_hi:
            base, lo_label, lo_width = pair_hi[lbl]
            val = (values[lbl] << lo_width) | values[lo_label]
            out.append(_render_field(base, val, form))
        else:
            base = _BASE_RE.match(lbl).group(1)
            out.append(_render_field(base, values[lbl], form, lbl))
    return ", ".join(out)


def disasm_line(region_load, pos, nwords, form, frame):
    """Format one decoded instruction as a disassembly line."""
    words = [(frame >> (32 - 16 * i)) & 0xFFFF for i in range(nwords)]
    hexwords = " ".join(f"{w:04x}" for w in words)
    sw_addr = (region_load + pos - 0x28000000) // 2
    if form is None:
        return f"{sw_addr:08x}  {hexwords}  ??"
    return f"{sw_addr:08x}  {hexwords}  {form['name']}  {render_operands(frame, form)}"


def disassemble(mem, region_load, start=0, count=None):
    """Linear-decode a memory region and return a list of disassembly lines."""
    dec = Decoder()
    if count is None:
        count = (len(mem) - start) // 2  # worst case: all 16-bit instructions
    entries = linear(mem, start, count, dec)
    return [disasm_line(region_load, pos, n, form, frame) for pos, n, form, cands, frame in entries]
