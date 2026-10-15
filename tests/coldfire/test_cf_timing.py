"""Spot checks of tools/coldfire/cf_timing.py against the entries of the manual's section 3.3.5."""

from tools.coldfire import cf_timing as t


def row(id_, *ops, size=".l", flow="", **extra):
    return {"id": id_, "size": size, "ops": list(ops), "flow": flow, "len": 2, **extra}


def cr(timing):
    return (timing.c, timing.r, timing.w)


def test_move_table():
    # Table 3-14: source row x destination column.
    assert cr(t.timing(row("move", "dn:0", "dn:1"))) == (1, 0, 0)
    assert cr(t.timing(row("move", "ind:0", "dn:1"))) == (1, 1, 0)
    assert cr(t.timing(row("move", "post:0", "pre:1"))) == (2, 1, 1)
    assert cr(t.timing(row("move", "idx:0:1:4", "ind:1"))) == (3, 1, 1)
    assert cr(t.timing(row("move", "dn:0", "idx:1:2:1"))) == (2, 0, 1)
    assert cr(t.timing(row("move", "imm", "disp:1"))) == (1, 0, 1)
    assert cr(t.timing(row("move", "absl:4", "ind:1"))) == (2, 1, 1)
    assert cr(t.timing(row("movea", "pcdisp", "an:1"))) == (
        1,
        1,
        0,
    )  # PC modes cost like An modes


def test_one_and_two_operand_tables():
    assert cr(t.timing(row("clr", "idx:0:1:1"))) == (2, 0, 1)  # Table 3-15
    assert cr(t.timing(row("tst", "ind:3"))) == (1, 1, 0)
    assert cr(t.timing(row("add_to_d", "idx:0:1:2", "dn:1"))) == (2, 1, 0)  # Table 3-16
    assert cr(t.timing(row("add_to_ea", "dn:0", "ind:1"))) == (1, 1, 1)
    assert cr(t.timing(row("addq", {"m": "q", "v": 1}, "ind:1"))) == (1, 1, 1)
    assert cr(t.timing(row("cmp", "imm", "dn:1"))) == (1, 0, 0)
    assert t.timing(row("divu_w", "dn:0", "dn:1")).c == 20
    assert t.timing(row("rems_l", "ind:0", "dn:1", "dn:2")).c == 35
    assert cr(t.timing(row("btst_r", "dn:0", "dn:1"))) == (2, 0, 0)
    assert cr(t.timing(row("bset_i", {"m": "q", "v": 3}, "ind:1"))) == (2, 1, 1)


def test_misc_emac_and_flow():
    assert cr(t.timing(row("lea", "idx:0:1:1", "an:2"))) == (2, 0, 0)  # Table 3-16
    assert cr(t.timing(row("pea", "disp:1"))) == (1, 0, 1)  # Table 3-17
    assert cr(t.timing(row("movem_load", "ind:7", {"m": "list", "n": 5}))) == (5, 5, 0)
    assert cr(t.timing(row("movem_store", {"m": "list", "n": 4}, "ind:0"))) == (4, 0, 4)
    assert t.timing(row("nop")).c == 6
    assert cr(t.timing(row("trap"))) == (18, 1, 2)
    assert t.timing(row("muls_w", "idx:0:1:1", "dn:2")).c == 5  # Table 3-18
    assert t.timing(row("mulu_l", "dn:0", "dn:1")).c == 4
    assert t.timing(row("mac_load", "ind:0")).c == 1
    assert t.timing(row("move_to_macsr", "dn:0", "macsr")).c == 8
    rts = t.timing(row("rts"))
    assert (rts.c, rts.worst()) == (2, 9)  # Table 3-19, note 3
    assert t.timing(row("jmp", "ind:0")).c == 5
    assert t.timing(row("jsr", "idx:0:1:1")).c == 6
    assert t.timing(row("jsr", "absl:4")).worst() == 3  # note 2: 1 to 3
    assert t.timing(row("fmove_out", "fp")) is None  # no FPU on this device


def test_bcc_cycles():
    # Table 3-20: folded when taken and predicted (0), 1 when not taken;
    # a mispredicted Bcc costs 8 in total (1 + 7).
    assert t.bcc_cycles(10, 5, 0) == (5, 15)
    assert t.bcc_cycles(10, 5, 2) == (5, 29)


def test_stall_rules():
    load_a0 = row("movea", "ind:1", "an:0")
    use_a0 = row("move", "ind:0", "dn:1")
    assert t.stall_cycles(load_a0, use_a0) == 2  # written An as base
    use_idx = row("move", "idx:2:0:4", "dn:1")  # D0 as index, scale 4
    set_d0 = row("moveq", {"m": "q", "v": 1}, "dn:0")
    assert t.stall_cycles(set_d0, use_idx) == 3
    assert t.stall_cycles(set_d0, row("move", "idx:2:0:1", "dn:1")) == 2
    assert (
        t.stall_cycles(row("move", "post:0", "dn:5"), row("move", "ind:3", "dn:1")) == 0
    )
    # post-increment results do not stall
    assert t.stall_cycles(row("move", "dn:1", "post:0"), use_a0) == 0
    assert (
        t.stall_cycles(row("mac_load", "ind:0"), row("move_from_acc", "acc", "dn:0"))
        == 3
    )
