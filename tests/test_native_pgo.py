"""The PGO profile key covers the emulation crates, not the desktop UI crate."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import native_pgo  # noqa: E402

sys.path.pop(0)

PACKAGES = ["native/sharc", "native/machine"]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A tiny repo with a trained profile manifest for it."""
    for rel, text in {
        "native/sharc/lib.rs": "a",
        "native/machine/lib.rs": "b",
        "packages/desktop/src-tauri/main.rs": "ui",
        "Cargo.toml": "[workspace]",
        "Cargo.lock": "lock",
        "gen/core.rs": "core",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    state = tmp_path / "state"
    state.mkdir()
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(native_pgo, "ROOT", tmp_path)
    monkeypatch.setattr(native_pgo, "state_dir", lambda triple=None: state)
    monkeypatch.setattr(native_pgo, "toolchain_dir", lambda: toolchain)
    monkeypatch.setattr(native_pgo, "rustc_version", lambda: "rustc 1.98.1")
    profdata = state / "p.profdata"
    profdata.write_bytes(b"profile")
    hasher = native_pgo.FileHasher(state / "hash-cache.json")
    components, _ = native_pgo.compute_components(PACKAGES, tmp_path / "gen", hasher)
    manifest = {
        "components": components,
        "packages": PACKAGES,
        "desktop_sha256": native_pgo.desktop_digest(hasher),
        "profdata": {"path": str(profdata)},
    }
    (state / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def state(tree):
    return native_pgo.check_state(tree / "gen", "")


def test_unchanged_tree_is_ok(tree):
    assert state(tree) == ("ok", str(tree / "state" / "p.profdata"), False)


def test_desktop_crate_change_reuses_the_profile(tree):
    (tree / "packages/desktop/src-tauri/main.rs").write_text("ui, diagnostics fix")
    status, _, desktop = state(tree)
    assert (status, desktop) == ("ok", True)
    assert native_pgo.check(tree / "gen", "")[0] == "ok"


def test_desktop_files_in_a_listed_package_list_are_ignored(tree):
    manifest_path = tree / "state" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["packages"] = [*PACKAGES, "packages/desktop/src-tauri"]
    manifest_path.write_text(json.dumps(manifest))
    (tree / "packages/desktop/src-tauri/main.rs").write_text("changed")
    assert state(tree)[0] == "ok"


def test_emulation_crate_change_is_stale(tree):
    (tree / "native/sharc/lib.rs").write_text("a changed")
    status, detail, _ = state(tree)
    assert (status, detail) == ("stale", "changed: sources")


def test_generated_core_and_fast_regions_are_stale(tree):
    (tree / "gen/core.rs").write_text("new core")
    assert state(tree)[1] == "changed: generated"
    assert native_pgo.check_state(tree / "gen", "0x1")[1] == (
        "changed: generated, fast_regions"
    )


def test_blocktrans_roots_are_stale(tree):
    assert native_pgo.check_state(tree / "gen", "", "0xb8276c")[1] == (
        "changed: blocktrans"
    )


def test_blocktrans_roots_follow_env_then_launcher(monkeypatch):
    assert native_pgo.blocktrans_roots({"SHARC_BLOCKTRANS": "0x1"}) == "0x1"
    assert native_pgo.blocktrans_roots({"DIGI_EMU_FAST": "0"}) == ""
    monkeypatch.setattr(
        native_pgo.runpy,
        "run_path",
        lambda *a, **k: {"blocktrans_roots_for": lambda syx: "0x2"},
    )
    assert native_pgo.blocktrans_roots({}) == "0x2"


def test_missing_profile_file_and_manifest(tree):
    (tree / "state" / "p.profdata").unlink()
    assert state(tree)[0] == "missing"
    (tree / "state" / "manifest.json").unlink()
    assert state(tree) == ("missing", "no manifest", False)


def test_cmd_check_reports_the_reuse(tree, monkeypatch, capsys):
    monkeypatch.setattr(native_pgo, "fast_regions", lambda: "")
    monkeypatch.setattr(native_pgo, "blocktrans_roots", lambda: "")
    monkeypatch.setattr(native_pgo, "generated_dir", lambda environ=None: tree / "gen")
    (tree / "packages/desktop/src-tauri/main.rs").write_text("ui 2")
    assert native_pgo.cmd_check(None) == 0
    assert (
        capsys.readouterr().out.strip() == "ok (desktop crate changed; profile reused)"
    )


def test_local_package_dirs_excludes_packages(monkeypatch):
    metadata = {
        "packages": [
            {"source": None, "manifest_path": str(ROOT / name / "Cargo.toml")}
            for name in (
                "native/sharc",
                "native/coldfire/cfabi",
                "packages/desktop/src-tauri",
            )
        ]
        + [{"source": "registry+x", "manifest_path": "/elsewhere/Cargo.toml"}]
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(metadata), ""),
    )
    assert native_pgo.local_package_dirs() == ["native/coldfire/cfabi", "native/sharc"]


def test_fallback_notice_is_prefixed_with_warning():
    for status, detail in (
        ("stale", "changed: sources"),
        ("missing", "no manifest"),
        ("skipped", "RUSTFLAGS is set"),
        ("failed", "training failed (exit 1)"),
    ):
        assert native_pgo.fallback_notice(status, detail).startswith("WARNING: ")


# --- crate-hash guard -------------------------------------------------------------

PROFILE_TEXT = """Counters:
  _RNvNtCsAAA111_12sharc_native6engine3run:
  _RNvCsBBB222_9sharc_gen4core:
  _RNvCsCCC333_8sharc_rt3mem:
  elektron_native_boot.1234-cgu.03;_RNvCsDDD444_20elektron_native_boot4boot:
  _RNvNtCsOTHER_4core3ptr9drop_glue:
  _RNvCsEEE555_12sharc_native3dup:
"""


def test_crates_in_maps_crates_to_disambiguators():
    assert native_pgo.crates_in(PROFILE_TEXT) == {
        "sharc_native": {"AAA111", "EEE555"},
        "sharc_gen": {"BBB222"},
        "sharc_rt": {"CCC333"},
        "elektron_native_boot": {"DDD444"},
    }


def test_profile_crates_parses_and_caches(tmp_path, monkeypatch):
    profdata = tmp_path / "p.profdata"
    profdata.write_text("x")
    runs = []

    def fake_run(command, **_kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=PROFILE_TEXT)

    monkeypatch.setattr(subprocess, "run", fake_run)
    first = native_pgo.profile_crates(profdata)
    assert first["sharc_gen"] == {"BBB222"}
    assert (tmp_path / "p.profdata.crates.json").is_file()
    assert native_pgo.profile_crates(profdata) == first
    assert len(runs) == 1


def test_binary_crates_reads_nm_output(monkeypatch):
    nm = "0000 T _RNvCsZZZ999_12sharc_native3run\n0001 t _RNvCsZZZ999_8sharc_rt3x\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kw: subprocess.CompletedProcess(command, 0, stdout=nm),
    )
    assert native_pgo.binary_crates("bin") == {
        "sharc_native": {"ZZZ999"},
        "sharc_rt": {"ZZZ999"},
    }


def test_crate_mismatches_reports_only_disjoint_crates():
    profile = native_pgo.crates_in(PROFILE_TEXT)
    same = {k: set(v) for k, v in profile.items()}
    assert native_pgo.crate_mismatches(profile, same) == []
    other = {**same, "sharc_gen": {"NEW"}}
    assert native_pgo.crate_mismatches(profile, other) == [
        "MISMATCH crate sharc_gen: profile ['BBB222'] binary ['NEW']"
    ]
