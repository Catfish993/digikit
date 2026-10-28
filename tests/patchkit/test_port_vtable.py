"""The `cf_vtable_slot` rule: the row pick and the rule fields, on synthetic rows (no database)."""

import pytest

from tools.patchkit import port_resolve as pr


class FakeCf(pr.CfFacts):
    def __init__(self, targets):
        self.name = "fake-1.0"
        self.targets = targets
        self.asked = None

    def vtable_slot(self, cls, slot, offset_to_top=0):
        self.asked = (cls, slot, offset_to_top)
        return self.targets


def test_one_row_gives_its_target():
    assert pr.pick_vtable_target([0x40111234], "w", "img") == 0x40111234


def test_no_row_is_an_error():
    with pytest.raises(pr.ResolveError, match="matches 0 vtables rows"):
        pr.pick_vtable_target([], "w", "img")


def test_two_rows_are_an_error():
    with pytest.raises(pr.ResolveError, match="matches 2 vtables rows"):
        pr.pick_vtable_target([0x100, 0x200], "w", "img")


def test_offset_to_top_defaults_to_zero_and_offset_is_added():
    cf = FakeCf([0x40111234])
    rule = {"name": "r", "class": "ConfirmWindow", "slot": 4, "offset": 8}
    assert pr.cf_vtable_addr(cf, rule) == 0x4011123C
    assert cf.asked == ("ConfirmWindow", 4, 0)
    cf.targets = [0x100]
    assert pr.cf_vtable_addr(cf, {**rule, "offset_to_top": -8, "offset": 0}) == 0x100
    assert cf.asked == ("ConfirmWindow", 4, -8)
