"""`pk_build.comparable`: the --check comparison ignores the local toolchain binary hashes."""

from __future__ import annotations

import copy

from tools.patchkit import pk_build


def make() -> dict:
    return {
        "source_sha256": "aa",
        "sections": {".text": {"hex": "0011"}},
        "symbols": {"main": {"value": 4}},
        "toolchain": {
            "selache_rev": "rev1",
            "instructions_redecoded": "3",
            "selas_sha256": "s1",
            "seldump_sha256": "d1",
        },
    }


def test_binary_hashes_are_ignored() -> None:
    a = make()
    b = make()
    b["toolchain"]["selas_sha256"] = "s2"
    b["toolchain"]["seldump_sha256"] = "d2"
    assert pk_build.comparable(a) == pk_build.comparable(b)


def test_input_is_not_modified() -> None:
    a = make()
    before = copy.deepcopy(a)
    pk_build.comparable(a)
    assert a == before


def test_real_differences_are_kept() -> None:
    base = pk_build.comparable(make())
    rev = make()
    rev["toolchain"]["selache_rev"] = "rev2"
    sec = make()
    sec["sections"][".text"]["hex"] = "0012"
    sym = make()
    sym["symbols"]["main"]["value"] = 8
    src = make()
    src["source_sha256"] = "bb"
    for other in (rev, sec, sym, src):
        assert pk_build.comparable(other) != base
