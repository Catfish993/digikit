"""DT2 FX analysis configuration and direct/legacy report compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.emulator import dt2fx_compare, fxcheck


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


@pytest.mark.parametrize("layout", ["report.json", "dt2-trackfx-1/report.json"])
def test_compare_reads_direct_and_legacy_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    monkeypatch.setattr(fxcheck, "name_maps", lambda _: ({}, {}))
    monkeypatch.setattr(fxcheck, "decode_arrays", lambda peeks, _: peeks)
    report = tmp_path / layout
    report.parent.mkdir(parents=True, exist_ok=True)
    values = {"snd[64] Bit Reduction": 22 << 8}
    report.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "name": "tf-br-22",
                        "peek": values,
                        "pcm": {"sha256": "sample-hash"},
                        "guest_instructions": 123,
                    }
                ]
            }
        )
    )
    assert dt2fx_compare.load(tmp_path) == {"tf-br-22": (values, "sample-hash", 123)}
