"""Builders for hand-made SHARC+ words, loader blocks and call/return sites."""

import struct

from tools.sharc_dev import (
    sharc_disasm,  # noqa: E402
    sharcinv,  # noqa: E402
)
from tools.sharc_dev import sharc_visa_tables as T  # noqa: E402


def encode(name, extra=0):
    """-> little-endian bytes of a form-`name` instruction: its fixed bits, plus `extra` elsewhere."""
    t = T.get_type(name)
    insn = t["opcode_value"] | (extra & ~t["opcode_mask"] & ((1 << t["bits"]) - 1))
    words = [
        (insn >> (t["bits"] - 16 * (i + 1))) & 0xFFFF for i in range(t["bits"] // 16)
    ]
    return struct.pack("<%dH" % len(words), *words)


def block(code, addr, count, arg=0, payload=b""):
    """Build a core-0 header with a valid HDRCHK, then its payload."""
    hdr = bytearray(struct.pack("<IIII", code | 0xAD000000, addr, count, arg))
    hdr[2] = 0
    x = 0
    for b in hdr:
        x ^= b
    hdr[2] = x
    return bytes(hdr) + payload


def put(name, **stems):
    """-> bits for encode(): each field whose label is STEM or STEM[..] set to its value."""
    extra = 0
    for label, (_hi, lo) in T.get_type(name)["fields"].items():
        stem = label.split("[")[0]
        if stem in stems:
            extra |= stems[stem] << lo
    return extra


def words(*ws):
    return struct.pack("<%dH" % len(ws), *ws)


def cjump(target):
    return encode("25a_direct", put("25a_direct") | (target & 0xFFFFFF))


def push3c():
    return words(0x9FF2)


def push3a():
    return encode("3a", put("3a", u=1, i=7, m=7, cond=31, g=0, d=1, l=0, ureg=2))


def store(value):
    return encode("16a", put("16a", i=7, m=7) | (value & 0xFFFFFFFF))


def load(ureg, value):
    return encode("17b", put("17b", ureg=ureg) | (value & 0xFFFF))


def call8a_rel(rel, b=1, a=0, cond=31, j=1, ci=0):
    """Type 8a PC-relative jump/call (SHARC+ Core Programming Reference, Type
    8a, p.357): b=1 is CALL, b=0 is JUMP; j is delayed (1) vs non-delayed (0),
    not a call/jump selector; cond=31 is unconditional."""
    return encode(
        "8a_rel", put("8a_rel", b=b, a=a, cond=cond, j=j, ci=ci) | (rel & 0xFFFFFF)
    )


def call8a_abs(addr, b=1, a=0, cond=31, j=1, ci=0):
    return encode(
        "8a_abs", put("8a_abs", b=b, a=a, cond=cond, j=j, ci=ci) | (addr & 0xFFFFFF)
    )


def field_insn(name, **values):
    """A `name` instruction with each of its fields set from `values` (by
    merged base name, e.g. reladdr=0x1234 covers both reladdr[22:16] and
    reladdr[15:0]) -- generalizes compute23's per-chunk placement below to
    any form whose fields are split across non-overlapping bit ranges, such
    as Type12a's data/reladdr."""
    t = T.get_type(name)
    insn = t["opcode_value"]
    for label, (hi, lo) in t["fields"].items():
        base = label.split("[")[0]
        if base not in values:
            continue
        if "[" in label:
            chi, clo = label[label.index("[") + 1 : -1].split(":")
            chi, clo = int(chi), int(clo)
            chunk = (values[base] >> clo) & ((1 << (chi - clo + 1)) - 1)
        else:
            chunk = values[base] & ((1 << (hi - lo + 1)) - 1)
        insn |= chunk << lo
    nwords = t["bits"] // 16
    ws = [(insn >> (t["bits"] - 16 * (i + 1))) & 0xFFFF for i in range(nwords)]
    return struct.pack("<%dH" % nwords, *ws)


def loop_insn(sw, count, reladdr, form="12a_imm"):
    """(sw, Instruction) for a Type12a hardware-loop setup at `sw`, trip
    count `count` (12a_imm) and end offset `reladdr` short-words ahead of
    the loop body's start (PRM Table 12a: reladdr is PC-relative from the
    setup instruction itself, see sharcinv.compute_vector)."""
    data = (
        field_insn(form, data=count, reladdr=reladdr & 0x7FFFFF, mode=0)
        if form == "12a_imm"
        else field_insn(form, ureg=count, reladdr=reladdr & 0x7FFFFF, mode=0)
    )
    insn = next(sharc_disasm.disassemble(data))
    return sw, insn


def rframe():
    return encode("25c_rframe")


def ret():
    """9b_abs return jump (raw 0x083F343F), split into its two 16-bit words."""
    return words(sharcinv.RETURN_JUMP >> 16, sharcinv.RETURN_JUMP & 0xFFFF)
