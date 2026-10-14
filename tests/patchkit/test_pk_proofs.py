"""tools/patchkit/pk_proofs.py: the registry and the command line, without running a proof."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.patchkit import pk_proofs  # noqa: E402


def test_every_tool_script_exists() -> None:
    for label, script, _ in pk_proofs.TOOLS:
        assert (ROOT / "tools" / "patchkit" / script).is_file(), label


def test_gain_and_drive_name_a_device() -> None:
    for label, script, extra in pk_proofs.TOOLS:
        if script in ("pk_proof_gain.py", "pk_proof_drive.py"):
            assert extra[:1] in (["dt2"], ["dn2"]), label


def test_command_is_quick_with_out() -> None:
    for _, script, extra in pk_proofs.TOOLS:
        cmd = pk_proofs.command(script, extra, Path("somewhere"), 0)
        assert "--quick" in cmd
        assert cmd[cmd.index("--out") + 1] == "somewhere"
        assert "--jobs" not in cmd
    assert pk_proofs.command("x.py", [], Path("o"), 3)[-2:] == ["--jobs", "3"]


def test_unknown_only_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("a subprocess was started")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    rc = pk_proofs.main(["--only", "nosuchtool", "--out", str(tmp_path)])
    assert rc == 2
    assert "unknown --only names: nosuchtool" in capsys.readouterr().err
