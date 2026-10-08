"""Send FX sweep tooling: scenario generator and name parsing."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
try:
    import fxcheck
    import gen_sendfx
    import scenario_dsl as d
finally:
    sys.path.pop(0)


def test_chord_latches() -> None:
    tl = d.Timeline()
    tl.chord(1000, d.FUNC, d.FX_PAGE)
    ev = [(e["button"], e["down"]) for e in tl.as_list()]
    assert ev == [(17, True), (5, True), (5, False), (17, False)]


def test_scenarios_share_timing() -> None:
    sc = gen_sendfx.scenarios()
    names = [s["name"] for s in sc]
    assert len(set(names)) == len(names)
    for fx in ("del", "rev", "chr"):
        grp = [
            s
            for s in sc
            if s["name"].startswith(f"sf-{fx}-") and "ppwidth" not in s["name"]
        ]
        times = {tuple(e["at_instr"] for e in s["events"]) for s in grp}
        assert len(times) == 1, fx
        assert len({s["seconds"] for s in grp}) == 1
    # the chord count selects the page: 1 Delay, 2 Reverb, 3 Chorus
    rev = next(s for s in sc if s["name"] == "sf-rev-ref")
    assert sum(1 for e in rev["events"] if e.get("button") == 17 and e["down"]) == 2


def test_sf_name() -> None:
    assert fxcheck.sf_name("sf-del-fb-d127") == ("del", "fb", -127)
    assert fxcheck.sf_name("sf-rev-decay-u20") == ("rev", "decay", 20)
    assert fxcheck.sf_name("sf-chr-ref") == ("chr", "ref", None)
    assert fxcheck.sf_name("tf-br-28") == (None, None, None)
