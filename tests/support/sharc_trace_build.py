"""Synthetic loader blocks, loaded memory and instructions for SHARC tracer tests."""

import struct
from importlib import import_module
from pathlib import Path

L = import_module("tools.sharc_dev.sharcldr")
T = import_module("tools.sharc_dev.sharc_trace")


def loader_block(code, address, count, arg=0, payload=b""):
    """Build a checksum-valid synthetic loader block."""
    header = bytearray(struct.pack("<IIII", code | 0xAD000000, address, count, arg))
    header[2] = 0
    checksum = 0
    for byte in header:
        checksum ^= byte
    header[2] = checksum
    return bytes(header) + payload


def loader_memory(*blocks):
    return L.LoadedMemory.from_stream(b"".join(blocks))


def insn(name, fields, length=4, kind="confident"):
    return import_module("tools.sharc_dev.sharc_disasm").Instruction(0, length, name, fields, kind=kind)


def full_compute(cu, opcode, rn, rx, ry):
    """A full-compute field dict for _compute(f, short=False, ...)."""
    field = (cu << 20) | (opcode << 12) | (rn << 8) | (rx << 4) | ry
    return {"compute[22:16]": field >> 16, "compute[15:0]": field & 0xFFFF}


def short_compute(opcode, rn, rx):
    """A short-compute field dict for _compute(f, short=True, ...)."""
    return {"compute[11:0]": (opcode << 8) | (rn << 4) | rx}


def dual_add_subtract_fields(float_form, rs, ra, rx, ry):
    """PRM Table 18-10 (p.433) / Table 18-13 (p.434): opcode[19:16]=0111
    (fixed) or 1111 (float); Rs[15:12], Ra[11:8], Rx[7:4], Ry[3:0]."""
    top_nibble = 0xF if float_form else 0x7
    field = (top_nibble << 16) | (rs << 12) | (ra << 8) | (rx << 4) | ry
    return {"compute[22:16]": field >> 16, "compute[15:0]": field & 0xFFFF}


def mulalu_fields(category, rm, ra, rxm, rym, rxa, rya):
    """PRM Figure 18-1 mf bit (p.423) + Table 18-16/18-17 (p.434): mf=1,
    opcode[21:16]=CATEGORY, then Rm[15:12]/Ra[11:8]/Rxm[7:6]/Rym[5:4]/
    Rxa[3:2]/Rya[1:0] (Rxm/Rym/Rxa/Rya are each a 2-bit code within their
    fixed quad: R/F 0-3, 4-7, 8-11, 12-15 respectively)."""
    field = (
        (1 << 22)
        | (category << 16)
        | (rm << 12)
        | (ra << 8)
        | (rxm << 6)
        | (rym << 4)
        | (rxa << 2)
        | rya
    )
    return {"compute[22:16]": field >> 16, "compute[15:0]": field & 0xFFFF}


def f32(value):
    """The IEEE-754 single-precision bit pattern for a Python float."""
    return struct.unpack("<I", struct.pack("<f", value))[0]


def shiftimm_fields(opcode, data8, rn, rx, dataex=0):
    """A ShiftImm field dict for _shift_immediate."""
    field = (opcode << 16) | (data8 << 8) | (rn << 4) | rx
    return {
        "shiftimm[22:16]": field >> 16,
        "shiftimm[15:0]": field & 0xFFFF,
        "dataex[3:0]": dataex,
    }


class TraceRunMixin:
    """TraceTest's run_one helper, shared by the trace test classes."""

    def run_one(self, state, record):
        return T._execute(state, record)[0]
