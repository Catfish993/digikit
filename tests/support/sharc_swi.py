"""Synthetic SHARC+ interrupt vector memory, runners and native core builders."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import sharc_run as sr  # noqa: E402
import sharc_transpile_run as nr  # noqa: E402
import sharcldr  # noqa: E402
from sharc_core.state import _sync_pc_stack, _sync_status_stack  # noqa: E402
from sharc_core.values import Const  # noqa: E402
from sharc_disasm import get_type  # noqa: E402


def word(form: str, **fields: int) -> int:
    entry = get_type(form)
    assert entry is not None and entry["bits"] == 48
    raw = entry["opcode_value"]
    for name, value in fields.items():
        hi, lo = entry["fields"][name]
        assert 0 <= value < 1 << (hi - lo + 1)
        raw |= value << lo
    return raw


def memory(handler: list[int] | None = None):
    nop = word("21a")
    ivt = bytearray(nop.to_bytes(6, "little") * 128)
    jump = word(
        "8a_abs",
        **{
            "addr[23:16]": 0,
            "addr[15:0]": 0x80,
            "cond[4:0]": 31,
            "b": 0,
            "j": 1,
            "a": 0,
        },
    )
    for level in (11, 22, *range(28, 32)):
        offset = level * 24
        ivt[offset : offset + 6] = jump.to_bytes(6, "little")
    rti = word(
        "11a",
        **{
            "x": 1,
            "cond[4:0]": 31,
            "j": 0,
            "lr": 0,
            "e": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        },
    )
    program = bytearray(512)
    words = [(0x40, nop)] + [
        (0x80 + 3 * index, raw) for index, raw in enumerate(handler or [rti])
    ]
    for pc, raw in words:
        program[pc * 2 : pc * 2 + 6] = struct.pack(
            "<HHH", raw >> 32, (raw >> 16) & 0xFFFF, raw & 0xFFFF
        )
    return sharcldr.LoadedMemory(
        bytes(ivt + program),
        [
            dict(
                target_address=0x28240000,
                byte_count=len(ivt),
                fill=False,
                payload_offset=0,
                payload_len=len(ivt),
            ),
            dict(
                target_address=sharcldr.SW_ALIAS_BASE,
                byte_count=len(program),
                fill=False,
                payload_offset=len(ivt),
                payload_len=len(program),
            ),
        ],
    )


def runner(enabled=True, handler=None):
    ref = sr.Runner(memory(handler), 0x40)
    state = ref.state
    state.software_interrupts = enabled
    state.stack_model = state.bank_model = True
    for code, value in (
        (114, 0x1000),
        (115, 0x1000),
        (122, 0x80000000),
        (123, 0x80000000),
        (124, 0),
        (112, 0x123),
        (113, 0x456),
    ):
        state.uregs[code] = Const(value)
    state.mmrs[0x30024] = Const(4)
    _sync_pc_stack(state)
    _sync_status_stack(state)
    return ref


def native_core(ref):
    library = os.environ.get("SHARC_NATIVE_LIB", nr.DEFAULT_LIB)
    if not Path(library).exists():
        pytest.skip("no native library built")
    core = nr.NativeCore(nr.pack_image(ref.state.concrete), library)
    nr.to_native(core, ref.state)
    core.set_option(nr.OPT_RUNTIME_DECODE, 1)
    return core
