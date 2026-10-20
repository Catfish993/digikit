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


HEADER = "file\tstatus\trc\tseconds\tpassed\tfailed\tskipped\terrors\n"


def _write_summary(out: Path, rows: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.tsv").write_text(HEADER + "".join(r + "\n" for r in rows))


def test_failure_report_failed_file(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        [
            "tests/NAME/test_ok.py\tOK\t0\t1.0\t3\t0\t0\t0",
            "tests/NAME/test_bad.py\tFAILED\t1\t2.0\t1\t1\t0\t0",
        ],
    )
    (tmp_path / "xml").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "xml" / "tests__NAME__test_bad.xml").write_text(
        '<testsuite><testcase name="test_x"><failure message="AssertionError: x != y">'
        'trace</failure></testcase><testcase name="test_y"/></testsuite>'
    )
    (tmp_path / "logs" / "tests__NAME__test_bad.log").write_text("log\n")
    head, detail = changed_tests.failure_report(tmp_path)
    assert head.startswith("FAIL python-tests: 1 of 2 files failed")
    assert any("tests/NAME/test_bad.py" in d and "1 failed" in d for d in detail)
    assert any("AssertionError: x != y" in d for d in detail)
    assert not any("test_y" in d for d in detail)


def test_failure_report_all_ok(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        [
            "tests/NAME/test_one.py\tOK\t0\t1.0\t3\t0\t0\t0",
            "tests/NAME/test_two.py\tOK\t0\t1.0\t1\t0\t1\t0",
        ],
    )
    head, detail = changed_tests.failure_report(tmp_path)
    assert head.startswith("PASS python-tests: 2 files, 4 passed")
    assert detail == []


def test_failure_report_no_xml_shows_log_tail(tmp_path: Path) -> None:
    _write_summary(tmp_path, ["tests/NAME/test_gone.py\tNO-XML\t2\t1.0\t0\t0\t0\t0"])
    (tmp_path / "logs").mkdir()
    lines = [f"line {i}" for i in range(20)]
    (tmp_path / "logs" / "tests__NAME__test_gone.log").write_text(
        "\n".join(lines) + "\n"
    )
    head, detail = changed_tests.failure_report(tmp_path)
    assert head.startswith("FAIL python-tests: 1 of 1 files failed")
    assert detail[1:] == [f"    line {i}" for i in range(5, 20)]


def test_explain_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert changed_tests.main(["--explain"]) == 0
    assert "the gate adds:" in capsys.readouterr().out
    assert Path(changed_tests.__file__).is_file()
