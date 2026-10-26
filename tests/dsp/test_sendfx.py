"""Send FX tooling: shared Python timeline and analysis name parsing."""

from __future__ import annotations

from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.emulator import fxcheck  # noqa: E402
from tools.emulator import scenario_dsl as d  # noqa: E402


def test_chord_latches() -> None:
    tl = d.Timeline()
    tl.chord(1000, d.FUNC, d.FX_PAGE)
    ev = [(e["button"], e["down"]) for e in tl.as_list()]
    assert ev == [(17, True), (5, True), (5, False), (17, False)]


def test_sf_name() -> None:
    assert fxcheck.sf_name("sf-del-fb-d127") == ("del", "fb", -127)
    assert fxcheck.sf_name("sf-rev-decay-u20") == ("rev", "decay", 20)
    assert fxcheck.sf_name("sf-chr-ref") == ("chr", "ref", None)
    assert fxcheck.sf_name("tf-br-28") == (None, None, None)
