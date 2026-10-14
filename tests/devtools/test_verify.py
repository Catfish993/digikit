"""tools/verify.py: routing and the python-tests registry, without git or a subprocess."""

from __future__ import annotations

import sys

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "tools"))

import verify  # noqa: E402


def test_rust_tests_command_has_no_package_flags() -> None:
    cmd = verify.rust_tests_command()
    assert "-p" not in cmd
    assert cmd[-2:] == ["tools/rust_ws.py", "test"]


def test_docs_select_no_steps() -> None:
    assert verify.steps_for(["docs/x.md"]) == ()
    assert verify.steps_for(["AGENTS.md"]) == ()


def test_changed_pk_test_runs_itself_and_the_gate() -> None:
    assert "python-tests" in verify.steps_for(["tests/patchkit/test_pk_cf.py"])
    tests = verify.python_tests(["tests/patchkit/test_pk_cf.py"], False)
    assert any(t in tests for t in ("tests/patchkit", "tests/patchkit/test_pk_cf.py"))
    assert set(verify.PY_GATE) <= set(tests)


def test_pk_harness_triggers_the_patchkit_suite() -> None:
    tests = verify.python_tests(["tools/pk_harness.py"], False)
    assert "tests/patchkit" in tests


def test_digi_client_triggers_its_test() -> None:
    assert "tests/control" in verify.python_tests(["tools/digi_client.py"], False)
    assert "python-tests" in verify.steps_for(["tools/digi_client.py"])
    assert "python-tests" in verify.steps_for(["native/control/src/lib.rs"])


def test_full_and_empty() -> None:
    assert verify.python_tests([], False) == list(verify.PY_GATE)
    full = verify.python_tests([], True)
    for _, _, paths in verify.PY_SUITES:
        assert set(verify.resolve_tests(paths)) <= set(full)
    assert len(full) == len(set(full))


def test_every_suite_entry_resolves() -> None:
    for name, _, paths in verify.PY_SUITES:
        for path in paths:
            assert verify.resolve_tests((path,)), (name, path)


def test_unmatched_entry_is_an_error() -> None:
    with pytest.raises(SystemExit):
        verify.resolve_tests(("tests/test_no_such_*.py",))
    with pytest.raises(SystemExit):
        verify.resolve_tests(("tests/test_no_such_file.py",))


def test_scenario_tests_are_not_added() -> None:
    changed = ["tests/scenarios/test_dt2_fx.py"]
    assert "tests/scenarios/test_dt2_fx.py" not in verify.python_tests(changed, False)


def test_changed_support_module_runs_its_users() -> None:
    tests = verify.python_tests(["tests/support/sharc_swi.py"], False)
    assert "tests/sharc/semantics/test_sharc_software_interrupts.py" in tests
    assert "tests/sharc/semantics/test_sharc_core_timer.py" in tests
    assert "tests/sharc/trace/test_sharc_trace_control.py" not in tests


def test_explain_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("a run directory was created")

    monkeypatch.setattr(verify.digi_env, "run_dir", fail)
    assert verify.main(["--explain", "--only", "pins"]) == 0
    assert "pins" in capsys.readouterr().out
