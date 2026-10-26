"""Spot checks of tools/coldfire/cf_timing.py against the entries of the manual's section 3.3.5."""

from tools.coldfire import cf_timing as t


def row(id_, *ops, size=".l", flow="", **extra):
    return {"id": id_, "size": size, "ops": list(ops), "flow": flow, "len": 2, **extra}


def cr(timing):
    return (timing.c, timing.r, timing.w)


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
