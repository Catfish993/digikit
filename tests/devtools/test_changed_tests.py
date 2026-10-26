"""tools/devtools/changed_tests.py: the test selection, without git or a subprocess."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

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


def test_graph_links_support_imports() -> None:
    # tests import helpers as `support.x`; the graph links them to tests.support.x
    g = changed_tests.graph()
    assert "tests.support.pluck_rig" in g.find_modules_directly_imported_by(
        "tests.patchkit.test_pluck"
    )


def test_graph_links_a_module_run_by_name() -> None:
    # test_machine_conformance starts `-m support.conform_worker` in a subprocess
    g = changed_tests.graph()
    assert "tests.support.conform_worker" in g.find_modules_directly_imported_by(
        "tests.modules.test_machine_conformance"
    )


def test_graph_drops_imports_inside_functions_of_tools() -> None:
    # digi_env imports the patchkit front end only inside one function
    g = changed_tests.graph()
    assert "tools.patchkit.patchkit" not in g.find_modules_directly_imported_by(
        "tools.emulator.digi_env"
    )


def test_pk_sharc_selects_the_routine_tests_and_no_sharc_semantics() -> None:
    tests, _ = changed_tests.gate_tests(["tools/patchkit/pk_sharc.py"])
    assert {
        verify_route.CONFORMANCE,
        "tests/patchkit/test_pluck.py",
        "tests/patchkit/test_sine.py",
    } <= set(tests)
    assert not any(t.startswith("tests/sharc/") for t in tests)


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


@pytest.mark.parametrize("returncodes", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_main_propagates_exit_codes_and_continues(
    returncodes: tuple[int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    names = ["python-tests", "ruff-check"]
    monkeypatch.setattr(changed_tests.verify, "changed_files", lambda _: [])
    monkeypatch.setattr(changed_tests.verify, "rust_scope", lambda _: [])
    monkeypatch.setattr(
        changed_tests, "plan", lambda *args: [(n, [n], "") for n in names]
    )
    monkeypatch.setattr(changed_tests.digi_env, "run_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(changed_tests.verify_budget, "resolve_budget", lambda: 1)
    monkeypatch.setattr(changed_tests.verify_budget, "bounded_env", lambda *args: {})
    run = Mock(
        side_effect=[
            CompletedProcess([n], rc) for n, rc in zip(names, returncodes, strict=True)
        ]
    )
    monkeypatch.setattr(changed_tests.subprocess, "run", run)

    assert changed_tests.main(["--foreground"]) == int(any(returncodes))
    assert [call.args[0] for call in run.call_args_list] == [[n] for n in names]
    for call in run.call_args_list:
        assert not {"capture_output", "stdout", "stderr", "text"} & call.kwargs.keys()
    output = capsys.readouterr().out
    assert f"python-tests logs: {tmp_path}" in output
    for name, rc in zip(names, returncodes, strict=True):
        assert f"{'FAIL' if rc else 'PASS'} {name} (" in output
    if any(returncodes):
        failed = ", ".join(n for n, rc in zip(names, returncodes, strict=True) if rc)
        assert f"test:changed: FAILED: {failed}." in output


def test_explain_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(changed_tests.verify, "changed_files", lambda _: [])
    monkeypatch.setattr(changed_tests.verify, "rust_scope", lambda _: [])
    monkeypatch.setattr(
        changed_tests, "plan", lambda *args: [("python-tests", ["unused"], "")]
    )
    run = Mock(side_effect=AssertionError("explain must not run commands"))
    monkeypatch.setattr(changed_tests.subprocess, "run", run)
    assert changed_tests.main(["--explain"]) == 0
    run.assert_not_called()
    output = capsys.readouterr().out
    assert "Deferred to `mise run verify`" in output
    assert "RUN  python-tests: unused" in output
    assert "explained, nothing run" in output


def test_fast_plan_defers_module_and_native_work() -> None:
    files = [
        "modules/core/dsp-voice/module.toml",
        "packages/emulator/control/src/lib.rs",
        "tools/device/dkrun.py",
    ]
    commands = {n: c for n, c, _ in changed_tests.plan(files, Path("OUT"))}
    assert commands["ruff-check"][-1] == "tools/device/dkrun.py"
    assert commands["pathcheck"]
    assert "rust-tests" not in commands and "pk-build-check" not in commands
    tests = commands["python-tests"] or []
    assert not any(
        p.startswith("tests/") and p not in changed_tests.FAST_PYTHON_TESTS
        for p in tests
    )


def test_fast_plan_runs_only_selected_audited_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        changed_tests,
        "select",
        lambda *args: changed_tests.Selection(
            tests={
                "tests/devtools/test_verify.py": "changed",
                "tests/patchkit/test_pluck.py": "imports",
            }
        ),
    )
    commands = {n: c for n, c, _ in changed_tests.plan([], Path("OUT"))}
    assert "tests/devtools/test_verify.py" in commands["python-tests"]
    assert "tests/patchkit/test_pluck.py" not in commands["python-tests"]


def test_host_selection_uses_native_related_for_sources_tests_and_assets() -> None:
    for path in (
        "packages/control-client/src/index.ts",
        "packages/control-client/tests/client.test.ts",
        "apps/desktop/ui/src/native.css",
    ):
        commands = {n: c for n, c, _ in changed_tests.host_plan([path])}
        related = commands["host-tests-related"]
        assert related[related.index("vitest") + 1] == "related"
        assert "--run" in related and path in related
    for path in (
        "pnpm-lock.yaml",
        "vitest.host.config.ts",
        "packages/dk-session/package.json",
    ):
        commands = {n: c for n, c, _ in changed_tests.host_plan([path])}
        assert commands["host-tests"][1] == "test:host"


def test_deferred_output_names_python_crates_and_changed_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(changed_tests.verify, "python_test_plan", lambda *args: [])
    monkeypatch.setattr(changed_tests.verify, "rust_scope", lambda _: ["digi-control"])
    changed_tests.print_deferred(
        ["modules/core/dsp-voice/module.toml"],
        changed_tests.Selection(tests={"tests/patchkit/test_pluck.py": "imports"}),
    )
    output = capsys.readouterr().out
    assert "Python: tests/patchkit/test_pluck.py" in output
    assert "Rust crates: digi-control" in output
    assert "Full gate coverage: modules/core/dsp-voice/module.toml" in output


def test_unselected_audited_gate_units_are_still_deferred(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(changed_tests.verify, "python_test_plan", lambda *args: [])
    monkeypatch.setattr(changed_tests.verify, "rust_scope", lambda _: [])
    changed_tests.print_deferred(
        [],
        changed_tests.Selection(
            deep_only={"tests/devtools/test_verify_budget.py": "deep"}
        ),
    )
    assert "Python: tests/devtools/test_verify_budget.py" in capsys.readouterr().out


def test_docs_do_not_claim_unselected_python_gate_coverage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = ["docs/TESTING.md"]
    changed_tests.print_deferred(files, changed_tests.select(files))
    output = capsys.readouterr().out
    assert "Python:" not in output
    assert "python-tests:" not in output
