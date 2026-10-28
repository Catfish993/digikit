"""The `ordinal` of SHARC rules: the N-th function with a reloc_hash, on synthetic entries (no database)."""

import pytest

from tools.patchkit import port_resolve as pr


class FakeDsp(pr.SharcFuncs):
    def __init__(self, entries):
        self.image = "fake"
        self.entries = entries

    def funcs_by_hash(self, reloc_hash):
        return self.entries


def test_no_ordinal_needs_a_unique_hash():
    assert FakeDsp([0x100]).func_by_hash("h" * 64) == 0x100
    with pytest.raises(pr.ResolveError, match="matches 2 functions"):
        FakeDsp([0x100, 0x200]).func_by_hash("h" * 64)


def test_ordinal_picks_in_address_order():
    dsp = FakeDsp([0x100, 0x200, 0x300])
    assert dsp.func_by_hash("h" * 64, 0) == 0x100
    assert dsp.func_by_hash("h" * 64, 2) == 0x300


def test_ordinal_beyond_the_hits_is_an_error():
    with pytest.raises(pr.ResolveError, match="ordinal 3"):
        FakeDsp([0x100, 0x200]).func_by_hash("h" * 64, 3)
