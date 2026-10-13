"""Unit tests of the harness logic that need no daemon (tools/pk_harness.py)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import pk_harness as h  # noqa: E402


def test_button_and_encoder_names():
    assert h.button("trig 1") == 25
    assert h.button("TRIG_16") == 40
    assert h.button("func") == 17
    assert h.button("SYN") == h.button("SRC") == 2
    assert h.button(12) == 12
    assert h.encoder("enc c") == 3
    assert h.encoder("level") == 9
    with pytest.raises(h.HarnessError):
        h.button("TRIG 17")


def test_cell_names_and_keys():
    c = h.parse_cell("dt2-1.17:test/cf-demo,fx/master-gain")
    assert (c.device, c.firmware) == ("dt2", "1.17")
    assert c.modset == "fx/master-gain+test/cf-demo"
    assert c.golden_prefix == "dt2-1.17/fx/master-gain+test/cf-demo"
    assert "/" not in c.slug
    stock = h.parse_cell("dn2-1.12")
    assert stock.modset == "stock" and stock.golden_prefix == "dn2-1.12/stock"


def test_the_default_matrix_and_tour_parse():
    cells = h.load_matrix(h.DEFAULT_MATRIX)
    assert {(c.device, c.firmware) for c in cells} == {("dt2", "1.17"), ("dn2", "1.12")}
    assert any(c.modules for c in cells) and any(not c.modules for c in cells)
    segments = h.smoke_segments()
    names = [n for n, _ in segments]
    assert names == [
        "boot", "track", "pattern", "audition", "play_stop", "machine_menu", "save_reload", "stress",
    ]  # fmt: skip
    for _, steps in segments:
        for step in steps:
            actions = set(step) & {
                "tap", "hold", "turn", "wait_ms", "expect_frame", "expect_pcm_db", "expect_no_fault", "stress",
            }  # fmt: skip
            assert len(actions) == 1, step
            for key in [step["tap"]] if "tap" in step else []:
                for name in key if isinstance(key, list) else [key]:
                    h.button(name)


def test_module_tests_are_found_by_device():
    found = dict(
        h.module_tests(["fx/master-gain", "test/cf-demo"], "dt2", quick_only=True)
    )
    assert "fx.master-gain.audible" in found and "test.cf-demo.boots" in found
    assert all(isinstance(steps, list) and steps for steps in found.values())
    assert dict(h.module_tests(["fx/master-gain"], "dn2", quick_only=False))


def test_goldens_round_trip_and_only_hashes_are_committed(tmp_path):
    g = h.Goldens(tmp_path / "g.json")
    assert g.get("a") is None
    g.set("a", "ab" * 32)
    g.save()
    again = h.Goldens(tmp_path / "g.json")
    assert again.get("a") == "ab" * 32
    committed = json.loads(h.GOLDENS.read_text())
    assert committed, "goldens are committed"
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in committed.values())
    assert all(k.count("/") >= 3 for k in committed)


def test_stale_or_foreign_ready_inputs_are_refused(tmp_path, monkeypatch):
    cell = h.Cell("dn2", "1.12")
    syx = h.ROOT / h.FIRMWARE[("dn2", "1.12")]["syx"]
    if not syx.is_file():
        pytest.skip("no firmware")
    digest = hashlib.sha256(syx.read_bytes()).hexdigest()
    ready = tmp_path / "ready"
    ready.mkdir()
    monkeypatch.setenv("DIGI_PK_READY_DN2_112", str(ready))
    with pytest.raises(h.HarnessError, match="SOURCE.json"):
        h.firmware_inputs(cell)
    (ready / "SOURCE.json").write_text(json.dumps({"syx_sha256": digest}))
    with pytest.raises(h.HarnessError, match="INTC-force"):
        h.firmware_inputs(cell)
    (ready / "SOURCE.json").write_text(
        json.dumps({"syx_sha256": "00", "intc_force": False})
    )
    with pytest.raises(h.HarnessError, match="another .syx"):
        h.firmware_inputs(cell)
    (ready / "SOURCE.json").write_text(
        json.dumps({"syx_sha256": digest, "intc_force": False})
    )
    with pytest.raises(h.HarnessError, match="is missing"):
        h.firmware_inputs(cell)
