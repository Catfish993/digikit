"""The stack-use tools (tools/coldfire/task_stacks.py, frame_depth.py; finding 34), checked without firmware."""

from __future__ import annotations

from tools.coldfire import frame_depth, task_stacks


def test_push_values_of_a_task_create_call():
    # `pea (0x542,PC)` at 0x40033276: the operand is relative to the address after the opcode word
    assert task_stacks._push_value(0x40033276, "pea (0x542,PC)") == 0x400337BA
    assert task_stacks._push_value(0, "pea (0x4098f75c).l") == 0x4098F75C
    assert task_stacks._push_value(0, "pea (0x6).w") == 6
    assert task_stacks._push_value(0, "move.l #0x28000,-(SP)") == 0x28000
    assert task_stacks._push_value(0, "clr.l -(SP)") == 0
    assert task_stacks._push_value(0, "move.l D0,-(SP)") == "?"
    assert task_stacks._push_value(0, "addq.l 0x4,SP") is None


def _sec(name: str, code: str) -> dict:
    return {"name": name, "kind": "text", "hex": code, "size": len(code) // 2}


def test_frame_scan_follows_lea_and_calls():
    # lea -0x20(a7),a7 ; pea (a0) ; jsr $0.l (relocated to g) ; lea 4(a7),a7 ; lea 0x20(a7),a7 ; rts
    f_code = "4fefffe0" + "4850" + "4eb900000000" + "4fef0004" + "4fef0020" + "4e75"
    g_code = "4fefffc0" + "4fef0040" + "4e75"  # a 0x40 byte frame
    relocs = [
        {
            "section": ".text.f",
            "offset": 8,
            "symbol": ".sec..text.g",
            "type": "cf.32",
            "addend": 0,
        }
    ]
    f = frame_depth.scan(_sec(".text.f", f_code), relocs)
    g = frame_depth.scan(_sec(".text.g", g_code), relocs)
    assert (f.ext, g.ext) == (0x24, 0x40)
    assert f.calls == [(0x24, ".sec..text.g")]
    # f needs its 0x24 bytes, the return address of the call (4) and g's frame
    assert (
        frame_depth.worst({".sec..text.f": f, ".sec..text.g": g}, ".sec..text.f")[0]
        == 0x24 + 4 + 0x40
    )
