"""DT2 FX scenario files: they are what tools/emulator/gen_dt2fx.py writes, every
scenario is well formed, and fxcheck switches between the devices cleanly."""

from __future__ import annotations

import json

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.emulator import fxcheck
from tools.emulator import gen_dt2fx

FILES = {
    "dt2-trackfx.json": gen_dt2fx.trackfx,
    "dt2-sendfx.json": gen_dt2fx.sendfx,
}


@pytest.mark.parametrize("fn", FILES)
def test_files_match_the_generator(fn: str) -> None:
    got = json.loads((ROOT / "scenarios" / fn).read_text())["scenarios"]
    assert got == json.loads(json.dumps(FILES[fn]()))


@pytest.mark.parametrize("fn", FILES)
def test_scenarios_are_well_formed(fn: str) -> None:
    scenarios = FILES[fn]()
    names = [s["name"] for s in scenarios]
    assert len(names) == len(set(names))
    for s in scenarios:
        at = [e["at_instr"] for e in s["events"]]
        assert at == sorted(at), s["name"]
        # the NO prelude closes the boot modal before any turn
        assert s["events"][0] == {"at_instr": 0, "button": gen_dt2fx.NO, "down": True}
        trig = [e for e in s["events"] if e.get("button") == 25]
        assert len(trig) == 2 and trig[0]["down"] and not trig[1]["down"], s["name"]
        # the run ends after the note is released
        assert s["seconds"] * 132e6 > trig[1]["at_instr"], s["name"]


def test_every_name_has_a_reference() -> None:
    names = {s["name"] for fn in FILES.values() for s in fn()}
    for n in names:
        if n.startswith("sf-") and n.split("-")[1] in ("del", "rev", "chr"):
            assert f"sf-{n.split('-')[1]}-ref" in names
        elif n.startswith("tf-"):
            hold = fxcheck.hold_seconds(
                next(
                    s["events"] for fn in FILES.values() for s in fn() if s["name"] == n
                )
            )
            assert fxcheck.reference_for(n, hold) in names, n


def test_configure_round_trip() -> None:
    dn2 = (fxcheck.F0, fxcheck.SND_PEEK, fxcheck.BR_IDX, fxcheck.SCENARIO_FILES)
    try:
        fxcheck.configure("dt2")
        assert fxcheck.F0 == 1000.0 and fxcheck.BR_IDX == 64
        assert fxcheck.SND_PEEK.startswith("snd=*0x8000470c")
    finally:
        fxcheck.configure("dn2")
    now = (fxcheck.F0, fxcheck.SND_PEEK, fxcheck.BR_IDX, fxcheck.SCENARIO_FILES)
    assert now == dn2
    with pytest.raises(ValueError):
        fxcheck.configure("dx7")
