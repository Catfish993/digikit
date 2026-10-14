"""Unit tests of the harness logic that need no daemon (tools/pk_harness.py)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
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
    assert {(c.device, c.firmware) for c in cells} == {
        ("dt2", "1.17"),
        ("dt2", "1.16"),
        ("dn2", "1.12"),
    }
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


def test_goldens_save_merges_with_other_processes(tmp_path, monkeypatch):
    """Two processes recording different cells must not overwrite each other (`--update` with `--jobs`)."""
    monkeypatch.setattr(h, "OUT", tmp_path)
    path = tmp_path / "goldens.json"
    a, b = h.Goldens(path), h.Goldens(path)
    a.set("dt2-1.17/stock/boot/ready", "aa")
    b.set("dn2-1.12/stock/boot/ready", "bb")
    a.save()
    b.save()
    assert json.loads(path.read_text()) == {
        "dt2-1.17/stock/boot/ready": "aa",
        "dn2-1.12/stock/boot/ready": "bb",
    }


def test_shared_boot_cache_links(tmp_path):
    """Every daemon sees the same `patched` and `cards` directories and the same card path."""
    cache = tmp_path / "cache"
    run1, run2 = tmp_path / "run1", tmp_path / "run2"
    for run in (run1, run2):
        h.link_shared(run, cache)
    assert (run1 / "patched").resolve() == (run2 / "patched").resolve()
    assert (run1 / "cards").resolve() == (run2 / "cards").resolve()
    (
        run1 / "cards" / "abc123"
    ).mkdir()  # the daemon builds the card through its run dir
    (run1 / "cards" / "abc123" / "dt2.img").write_bytes(b"card")
    c1 = h.canonical_card(cache, str(run1 / "cards" / "abc123" / "dt2.img"))
    c2 = h.canonical_card(cache, str(run2 / "cards" / "abc123" / "dt2.img"))
    assert c1 == c2 and Path(c1).read_bytes() == b"card"
    # a private cache dir from an earlier run is kept aside
    run3 = tmp_path / "run3"
    (run3 / "patched").mkdir(parents=True)
    h.link_shared(run3, cache)
    assert (run3 / "patched").is_symlink() and list(run3.glob("patched.private-*"))


def test_default_jobs_is_bounded():
    assert 1 <= h.default_jobs(100) <= h.MAX_JOBS
    assert h.default_jobs(1) == 1


def test_proofkit_child_command_strips_job_options():
    import pk_proofkit as k

    argv = ["--quick", "--only", "a,b", "--jobs=4", "--out", "x", "--result", "r"]
    assert k._strip_options(argv, ("--only", "--jobs", "--result")) == [
        "--quick",
        "--out",
        "x",
    ]


def test_proofkit_runs_parts_and_reports(tmp_path, capsys):
    """Parts in one process: a crash is a failed check, not the end of the run; the verdict and exit code follow."""
    import argparse

    import pk_proofkit as k

    def good(p):
        p.check("fine", True, 1)

    def bad(p):
        p.check("broken", False, "detail")

    def crash(p):
        raise RuntimeError("boom")

    parts = [
        k.Part("g", "good", good),
        k.Part("b", "bad", bad, quick=False),
        k.Part("c", "crash", crash, quick=False),
    ]
    ap = argparse.ArgumentParser()
    k.add_args(ap, parts)
    a = ap.parse_args(["--out", str(tmp_path / "o"), "--jobs", "1"])
    assert k.run_tool("x.py", k.Proof, parts, a, argv=[]) == 1
    text = capsys.readouterr().out
    assert "part g" in text and "PASS" in text
    assert (
        "part b" in text
        and "part c crashed" in text
        and text.rstrip().splitlines()[-1].startswith("FAIL: parts b, c")
    )
    q = ap.parse_args(["--out", str(tmp_path / "q"), "--jobs", "1", "--quick"])
    assert k.run_tool("x.py", k.Proof, parts, q, argv=[]) == 0
