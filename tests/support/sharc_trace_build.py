"""Synthetic loader blocks, loaded memory and instructions for SHARC tracer tests."""

import struct
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
L = import_module("sharcldr")


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
    return import_module("sharc_disasm").Instruction(0, length, name, fields, kind=kind)
