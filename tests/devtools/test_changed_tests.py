"""tools/devtools/changed_tests.py: the test selection, without git or a subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.devtools import changed_tests, verify_route  # noqa: E402


def _no_directory(sel: changed_tests.Selection) -> None:
    for path in sel.tests:
        assert (ROOT / path).is_file(), path


def test_docs_select_nothing() -> None:
    assert changed_tests.select(["docs/TESTING.md"]).tests == {}


def test_patchkit_tool_selects_its_importer() -> None:
    sel = changed_tests.select(["tools/patchkit/pk_harness.py"])
    reason = sel.tests["tests/patchkit/smoke/test_pk_harness_units.py"]
    assert reason.startswith("imports")
    _no_directory(sel)


def test_framework_module_selects_conformance_not_a_directory() -> None:
    sel = changed_tests.select(["modules/core/dsp-voice/module.toml"])
    assert verify_route.CONFORMANCE in sel.tests
    _no_directory(sel)
    assert any(line.startswith("tests/patchkit") for line in sel.gate_adds)


def test_changed_test_selects_itself() -> None:
    sel = changed_tests.select(["tests/devtools/test_verify.py"])
    assert sel.tests["tests/devtools/test_verify.py"] == "changed"


def test_support_module_selects_importers() -> None:
    sel = changed_tests.select(["tests/support/machine_rig.py"])
    assert sel.tests
    assert all("via" in r or "imports" in r or "runs" in r for r in sel.tests.values())


def test_selection_never_holds_a_directory() -> None:
    paths = sorted(
        p.relative_to(ROOT).as_posix()
        for sub in ("tools/devtools", "tools/patchkit")
        for p in (ROOT / sub).glob("*.py")
    )
    for path in paths:
        _no_directory(changed_tests.select([path]))


def test_deep_is_a_superset() -> None:
    path = "tools/emulator/digi_env.py"
    shallow = changed_tests.select([path])
    deep = changed_tests.select([path], deep=True)
    assert not deep.deep_only
    assert set(shallow.tests) <= set(deep.tests)


def test_explain_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert changed_tests.main(["--explain"]) == 0
    assert "the gate adds:" in capsys.readouterr().out
    assert Path(changed_tests.__file__).is_file()
