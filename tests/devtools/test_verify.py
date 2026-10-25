"""tools/devtools/verify.py: routing and the python-tests registry, without git or a subprocess."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.devtools import verify, verify_pre, verify_route  # noqa: E402
from tools.patchkit import modroots  # noqa: E402


def test_rust_tests_command_has_no_package_flags() -> None:
    cmd = verify.rust_tests_command()
    assert "-p" not in cmd
    assert cmd[-3:] == ["-m", "tools.devtools.rust_ws", "test"]


def test_docs_select_no_steps() -> None:
    assert verify.steps_for(["docs/x.md"]) == ()
    assert verify.steps_for(["AGENTS.md"]) == ()


UI_FILES = [
    "apps/desktop/ui/src/Console.tsx",
    "apps/desktop/ui/src/console.css",
    "apps/desktop/ui/preview/sim.ts",
    "apps/web/src/components/PatchGallery.tsx",
    "apps/web/src/styles/site.css",
    "apps/web/tests/gallery.test.mjs",
    "docs/TESTING.md",
]


def test_ui_only_change_skips_smoke() -> None:
    for path in UI_FILES:
        steps = verify.steps_for([path])
        assert "smoke" not in steps, path
    steps = verify.steps_for(UI_FILES)
    assert "smoke" not in steps
    assert "web" in steps  # the UI steps themselves still run


def test_ui_change_with_one_other_file_keeps_smoke() -> None:
    assert "smoke" in verify.steps_for([*UI_FILES, "tools/device/dkscreen.py"])


@pytest.mark.parametrize(
    "path",
    [
        "apps/desktop/src-tauri/src/main.rs",
        "apps/web/src/pages/patch.astro",
        "apps/web/src/modules/dktransfer.ts",
        "apps/web/src/patch/patchkit-core.ts",
        "modules/core/runtime/module.toml",
        "tests/patchkit/smoke/matrix.toml",
    ],
)
def test_non_ui_paths_keep_smoke(path: str) -> None:
    assert "smoke" in verify.steps_for([*UI_FILES, path])
    assert not verify.ui_only([path])


def test_docs_only_stays_empty() -> None:
    assert verify.steps_for(["docs/x.md", "README.md"]) == ()
    assert not verify.ui_only([])


def test_explain_says_why_smoke_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    def run(files: list[str]) -> str:
        steps = verify.steps_for(files)
        verify.explain(steps, files, [], per_file=True)
        return capsys.readouterr().out

    assert "smoke: skipped, UI-only change" in run(UI_FILES)
    assert "smoke: skipped" not in run([*UI_FILES, "tools/device/dkscreen.py"])
    assert "smoke: skipped" not in run(["docs/x.md"])


def test_changed_pk_test_runs_itself_and_the_gate() -> None:
    assert "python-tests" in verify.steps_for(["tests/patchkit/test_pk_cf.py"])
    tests = verify.python_tests(["tests/patchkit/test_pk_cf.py"], False)
    assert any(t in tests for t in ("tests/patchkit", "tests/patchkit/test_pk_cf.py"))
    assert set(verify.PY_GATE) <= set(tests)


def test_claude_settings_run_the_agent_guard_test() -> None:
    assert verify.steps_for([".claude/settings.json"]) == ("python-tests", "smoke")
    plan = [p for p, _ in verify.python_test_plan([".claude/settings.json"], False)]
    assert "tests/devtools/test_agent_guard.py" in plan


def test_a_python_module_selects_its_importers_not_the_directory() -> None:
    tests = verify.python_tests(["tools/patchkit/pk_harness.py"], False)
    assert "tests/patchkit/smoke/test_pk_harness_units.py" in tests
    assert "tests/patchkit" not in tests


def test_a_rust_change_keeps_the_directory_suite() -> None:
    assert "tests/patchkit" in verify.python_tests(
        ["packages/patchkit/src/lib.rs"], False
    )


def test_digi_client_triggers_its_test() -> None:
    assert "tests/control/test_digi_client.py" in verify.python_tests(
        ["tools/control/digi_client.py"], False
    )
    assert "python-tests" in verify.steps_for(["tools/control/digi_client.py"])
    assert "python-tests" in verify.steps_for(["packages/emulator/control/src/lib.rs"])


def test_moved_paths_route() -> None:
    assert "web" in verify.steps_for(["apps/web/src/x.ts"])
    assert "desktop" in verify.steps_for(["apps/desktop/src-tauri/src/main.rs"])
    assert "dt2-tone" in verify.steps_for(["packages/emulator/sharc/src/lib.rs"])
    assert "tier-quick" in verify.steps_for(
        ["packages/emulator/sharc-fast-cl/src/lib.rs"]
    )
    assert "web" in verify.steps_for(["packages/patchkit/src/lib.rs"])
    assert "card" in verify.steps_for(["packages/emulator/plusdrive/src/lib.rs"])
    assert "dt2-tone" in verify.steps_for(["packages/emulator/machine/src/lib.rs"])


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


def _group(
    label: str, stem: str, count: int, factor: float
) -> tuple[str, str, list[str], dict[str, float], float]:
    names = [f"{stem}-{i:03d}" for i in range(count)]
    seconds = {n: 4.0 + (i % 3) for i, n in enumerate(names)}
    return label, f"tests/scenarios/{stem}.json", names, seconds, factor


def test_plan_chunks() -> None:
    groups = [
        _group("default", "a", 47, 1.0),
        _group("default", "b", 87, 1.0),
        _group("no-aot", "a", 47, 2.0),
        _group("no-aot", "b", 87, 2.0),
    ]
    budget = 12
    chunks = verify.plan_chunks(groups, budget)
    for label, path, names, _, _ in groups:
        got = [n for c in chunks if (c.label, c.path) == (label, path) for n in c.names]
        assert sorted(got) == sorted(names)
        indexes = [c.index for c in chunks if (c.label, c.path) == (label, path)]
        assert sorted(indexes) == list(range(len(indexes)))
    costs = [c.cost for c in chunks]
    assert costs == sorted(costs, reverse=True)
    total = sum(costs)
    target = total / (budget * 4)
    biggest = max(f * s for _, _, _, sec, f in groups for s in sec.values())
    assert 24 <= len(chunks) <= 80
    assert max(costs) <= 2 * target + biggest


def test_plan_chunks_single_scenario() -> None:
    chunks = verify.plan_chunks([_group("default", "a", 1, 1.0)], 12)
    assert len(chunks) == 1
    assert chunks[0].names == ("a-000",)


def test_tier_jobs_budget_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGI_TIER_JOBS", "7")
    assert verify.tier_jobs_budget() == 7
    monkeypatch.setenv("DIGI_TIER_JOBS", "0")
    with pytest.raises(SystemExit):
        verify.tier_jobs_budget()


def test_python_tests_passed(tmp_path: Path) -> None:
    head = "file\tstatus\trc\tseconds\tpassed\tfailed\tskipped\terrors\n"
    (tmp_path / "summary.tsv").write_text(head + "a.py\tOK\t0\t1.0\t0\t0\t3\t0\n")
    assert verify.python_tests_passed(tmp_path)
    (tmp_path / "summary.tsv").write_text(head + "a.py\tOK\t0\t1.0\t2\t0\t3\t0\n")
    assert verify.python_tests_passed(tmp_path) is None


def test_python_tests_command() -> None:
    cmd = verify.python_tests_command(Path("x"), ["tests/a.py"])
    assert "tools.devtools.test_outcomes" in cmd
    assert "run" in cmd and "--check" in cmd
    assert "--timeout" in cmd
    assert cmd[-1] == "tests/a.py"


def test_python_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIGI_PY_TIMEOUT", raising=False)
    assert verify.python_timeout() == 3600
    monkeypatch.setenv("DIGI_PY_TIMEOUT", "120")
    assert verify.python_timeout() == 120
    cmd = verify.python_tests_command(Path("x"), ["tests/a.py"])
    assert cmd[cmd.index("--timeout") + 1] == "120"
    monkeypatch.setenv("DIGI_PY_TIMEOUT", "0")
    with pytest.raises(SystemExit):
        verify.python_timeout()


def test_load_last_result_picks_newest(tmp_path: Path) -> None:
    assert verify.load_last_result(tmp_path / "none", None) is None
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (tmp_path / stamp).mkdir()
    (tmp_path / "20260101-000000" / "result.json").write_text('{"n": 1}')
    (tmp_path / "20260102-000000" / "result.json").write_text('{"n": 2}')
    assert verify.load_last_result(tmp_path, None) == {"n": 2}
    assert verify.load_last_result(tmp_path, tmp_path / "20260102-000000") == {"n": 1}


def test_load_last_result_skips_other_root(tmp_path: Path) -> None:
    older = tmp_path / "20260101-000000"
    newer = tmp_path / "20260102-000000"
    older.mkdir()
    newer.mkdir()
    (older / "result.json").write_text(
        json.dumps({"root": str(verify.ROOT), "failed": ["pins"]})
    )
    (newer / "result.json").write_text(
        json.dumps({"root": "/elsewhere", "failed": ["web"]})
    )
    assert verify.load_last_result(tmp_path, None) == {
        "root": str(verify.ROOT),
        "failed": ["pins"],
    }
    (newer / "result.json").write_text(json.dumps({"failed": ["web"]}))
    assert verify.load_last_result(tmp_path, None) == {"failed": ["web"]}


def test_rerun_plan() -> None:
    result = {"failed": ["pins", "check"], "py_tests": ["tests/a.py"]}
    expected = tuple(s for s in verify.STEPS if s in {"pins", "check"})
    assert verify.rerun_plan(result) == (expected, ["tests/a.py"])
    with pytest.raises(SystemExit):
        verify.rerun_plan({"failed": ["bogus"], "py_tests": []})


def test_rerun_failed_excludes_only_and_full() -> None:
    with pytest.raises(SystemExit) as info:
        verify.main(["--rerun-failed", "--only", "pins"])
    assert info.value.code == 2
    with pytest.raises(SystemExit) as info:
        verify.main(["--rerun-failed", "--full"])
    assert info.value.code == 2


def test_pick_rechecks() -> None:
    names = [f"s{i}" for i in range(20)]
    rng = verify.coupled.random.Random(1)
    assert verify.coupled.pick_rechecks(names, 0.0, rng) == []
    assert verify.coupled.pick_rechecks(names, 1.0, rng) == names
    first = verify.coupled.pick_rechecks(names, 0.5, verify.coupled.random.Random(7))
    again = verify.coupled.pick_rechecks(names, 0.5, verify.coupled.random.Random(7))
    assert first == again
    assert 0 < len(first) < len(names)
    assert set(first) <= set(names)


def test_recheck_probability_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIGI_RENDER_CACHE_CHECK", raising=False)
    assert verify.coupled.recheck_probability() == 0.0
    monkeypatch.setenv("DIGI_RENDER_CACHE_CHECK", "0.25")
    assert verify.coupled.recheck_probability() == 0.25
    for bad in ("2", "-0.1", "x", "nan"):
        monkeypatch.setenv("DIGI_RENDER_CACHE_CHECK", bad)
        with pytest.raises(SystemExit):
            verify.coupled.recheck_probability()


def test_cache_env() -> None:
    env = verify.cache_env({"A": "1"})
    assert env == {"A": "1", "DIGI_RENDER_CACHE_CHECK": "0.03"}
    keep = verify.cache_env({"DIGI_RENDER_CACHE_CHECK": "0.5"})
    assert keep["DIGI_RENDER_CACHE_CHECK"] == "0.5"
    off = verify.cache_env({}, no_cache=True)
    assert off["DIGI_RENDER_CACHE"] == "0"
    assert "DIGI_RENDER_CACHE" not in env


def test_explain_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("a run directory was created")

    monkeypatch.setattr(verify.digi_env, "run_dir", fail)
    assert verify.main(["--explain", "--only", "pins"]) == 0
    assert "pins" in capsys.readouterr().out


# ---- finer routing (tools/devtools/verify_route.py) ----------------------------------------------

PCM_STEPS = {
    "build",
    "desktop",
    "pins",
    "check",
    "check-no-aot",
    "tier-quick",
    "tier-sweep",
    "dt2-tone",
    "dt2-check",
    "dt2-tier-quick",
    "dt2-tier-sweep",
    "card",
}


def mpath(mid: str) -> str:
    """The repo-relative directory of a module."""
    return modroots.module_dir(mid).relative_to(ROOT).as_posix()


def selected(*files: str) -> set[str]:
    """The python-tests paths beyond the always-on gate files."""
    return set(verify.python_tests(list(files), False)) - set(verify.PY_GATE)


def test_mise_new_task_routes_like_its_tool() -> None:
    old = '[tasks.a]\nrun = "echo"\n'
    task = '[tasks."dk:run"]\nrun = "uv run python -m tools.device.dkrun"\ndescription = "x"\n'
    got = verify_route.mise_paths(old, old + task)
    assert got == (
        ["tools/device/dkrun.py"],
        {"tools/device/dkrun.py": "mise.toml task dk:run"},
    )
    assert verify.steps_for(got[0]) == ("python-tests", "smoke")
    # only the description or the alias changed: the same tool
    again = task.replace('description = "x"', 'description = "y"')
    assert verify_route.mise_paths(old + task, old + again) == got
    # an unchanged task selects nothing; a removed one only the path check
    assert verify_route.mise_paths(old + task, old + task) == ([], {})
    assert verify_route.mise_paths(old + task, old) == ([], {})


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('[tools]\nrust = "1"\n', '[tools]\nrust = "2"\n'),
        ('[env]\nA = "1"\n', '[env]\nA = "2"\n'),
        ('[tasks.verify]\nrun = "x"\n', '[tasks.verify]\nrun = "y"\n'),
        ('[tasks."verify:pre"]\nrun = "x"\n', '[tasks."verify:pre"]\nrun = "y"\n'),
        ('[tasks."gen:core"]\nrun = "x"\n', '[tasks."gen:core"]\nrun = "y"\n'),
        ('[tasks.build]\nrun = "x"\n', '[tasks.build]\nrun = "y"\n'),
        ("", '[tasks.t]\nrun = "cargo build"\n'),  # a run that names no file
        (
            "",
            '[tasks.t]\nrun = "uv run python -m tools.device.dkrun"\nenv = {A = "1"}\n',
        ),
        ("", "[tasks.t"),  # unreadable
    ],
)
def test_mise_broad_changes_stay_broad(old: str, new: str) -> None:
    assert verify_route.mise_paths(old, new) is None


def test_mise_missing_side_is_broad() -> None:
    assert verify_route.mise_paths(None, "") is None
    assert verify_route.mise_paths("", None) is None


def test_mise_tasks_of_the_repo_resolve() -> None:
    """Every task of the real mise.toml names a repo file or is listed here as broad: a new task
    that routes nowhere would make every edit of it run all steps, so it is listed on purpose."""
    import tomllib

    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    broad = set()
    for name, task in tasks.items():
        group = name.split(":", 1)[0]
        if group in verify_route.BROAD_TASK_GROUPS or "run" not in task:
            continue
        if verify_route.paths_of_run(verify_route.run_lines(task)) is None:
            broad.add(name)
    assert broad == {"dev:plusdrive"}  # cargo run: no file to route by


def test_expand_mise_replaces_a_task_change(monkeypatch: pytest.MonkeyPatch) -> None:
    old = '[tasks.a]\nrun = "echo"\n'
    new = old + '[tasks."dk:run"]\nrun = "uv run python -m tools.device.dkrun"\n'
    monkeypatch.setattr(verify, "merge_base", lambda base, head: "BASE")
    monkeypatch.setattr(
        verify, "git_show", lambda rev, path: old if rev == "BASE" else new
    )
    files, origin = verify.expand_mise(["mise.toml", "docs/x.md"], "b", None)
    assert files == ["docs/x.md", "tools/device/dkrun.py"]
    assert origin == {"tools/device/dkrun.py": "mise.toml task dk:run"}
    # a broad change keeps mise.toml, which selects every step
    monkeypatch.setattr(
        verify,
        "git_show",
        lambda rev, path: old if rev == "BASE" else old + "[env]\nA = 1\n",
    )
    files, _ = verify.expand_mise(["mise.toml"], "b", None)
    assert files == ["mise.toml"]
    assert verify.steps_for(files) == verify.EVERYTHING
    # the other files are untouched
    assert verify.expand_mise(["tools/x.py"], "b", None) == (["tools/x.py"], {})


def test_device_tools_select_their_tests_only() -> None:
    assert verify.steps_for(["tools/device/dkscreen.py"]) == ("python-tests", "smoke")
    assert selected("tools/device/dkscreen.py") == {
        "tests/digikit/test_dkscreen.py",
        "tests/digikit/test_scenarios.py",
        "tests/digikit/test_upload_dn2.py",
        "tests/digikit/test_upload_dt2.py",
    }
    assert selected("tools/device/dkrun.py") == {
        "tests/digikit/test_dkrun.py",
        "tests/digikit/test_dktransfer.py",  # dktransfer imports dkrun
        "tests/digikit/test_live_dn2.py",  # and the live scenarios run dktransfer
        "tests/digikit/test_live_dt2.py",
        "tests/digikit/test_scenarios.py",
        "tests/digikit/test_menu_dn2.py",
        "tests/digikit/test_menu_dt2.py",
    }
    assert selected("tools/device/mmon.py") == set()
    assert "tests/patchkit" not in selected("tools/device/dkupload.py")
    assert "tests/digikit" not in selected("tools/device/dkupload.py")


def test_device_tools_nothing_dropped() -> None:
    """A change in a tools/device module runs every unit test that imports it, directly or through
    another tools/device module, and the scenario kinds whose runners call it."""
    for path in sorted((ROOT / "tools/device").glob("*.py")):
        stem = path.stem
        assert stem in verify_route.DEVICE_KINDS, f"classify tools/device/{stem}.py"
        got = selected(f"tools/device/{stem}.py")
        for test in sorted((ROOT / "tests").rglob("test_*.py")):
            if "node_modules" in test.parts:
                continue
            text = test.read_text()
            imports = rf"^\s*(from tools\.device import [^\n]*\b{stem}\b|from tools\.device\.{stem} import|import tools\.device\.{stem}\b)"
            if re.search(imports, text, re.M):
                assert test.relative_to(ROOT).as_posix() in got, (stem, test.name)
        for kind in verify_route.DEVICE_KINDS[stem]:
            assert set(verify_route.kind_files()[kind]) <= got, (stem, kind)
    kinds = set(verify_route.kind_files())
    for stem, wanted in verify_route.DEVICE_KINDS.items():
        assert set(wanted) <= kinds, stem


def test_digikit_test_files_and_scenarios() -> None:
    assert selected("tests/digikit/test_dkrun.py") == {"tests/digikit/test_dkrun.py"}
    assert selected("tests/patchkit/test_pk_cf.py") == {"tests/patchkit/test_pk_cf.py"}
    assert selected("tests/digikit/scenarios/menu-dkrun-pluck.toml") == {
        "tests/digikit/test_scenarios.py",
        "tests/digikit/test_menu_dn2.py",
        "tests/digikit/test_menu_dt2.py",
    }
    assert verify.steps_for(["tests/digikit/scenarios/menu-dkrun-pluck.toml"]) == (
        "python-tests",
        "smoke",
    )
    # a helper of the suite: the test files that import it
    helper = selected("tests/digikit/scen_cases.py")
    assert "tests/digikit/test_menu_dt2.py" in helper
    assert "tests/digikit" not in helper
    # every scenario kind that a test file runs maps to a test file of tests/digikit
    for scen, (kind, _) in verify_route.scenario_refs().items():
        got = selected(scen)
        assert set(verify_route.kind_files().get(kind, ())) <= got, scen


def test_module_leaf_selects_the_tests_that_name_it() -> None:
    src = "modules/fx/master-gain/src/cf/gain.s"
    assert verify.steps_for([src]) == ("python-tests", "rust-tests", "smoke")
    # test_patchkit.py (a gate file) and the tests that use a tool that builds master-gain
    assert "tests/patchkit/test_patchkit.py" in verify.python_tests([src], False)
    assert "tests/patchkit/test_pk_mem.py" in selected(src)
    assert "tests/patchkit/test_pk_cf.py" not in selected(src)
    assert not [t for t in selected(src) if t.startswith("tests/digikit/test_e")]
    assert "tests/patchkit" not in selected(src)
    # the object and the manifest are read by the web gallery
    assert "web" in verify.steps_for(["modules/fx/master-gain/module.toml"])
    assert "web" in verify.steps_for(
        ["modules/fx/master-gain/build/master-gain.dko.json"]
    )
    assert "web" not in verify.steps_for(["modules/core/runtime/sdk/src/lib.rs"])
    assert verify.steps_for(["modules/core/runtime/README.md"]) == ()


def test_module_scenario_modules_select_their_scenarios() -> None:
    got = selected("modules/machines/pluck/module.toml")
    for name in (
        "test_equiv_dn2",
        "test_equiv_dt2",
        "test_menu_dn2",
        "test_menu_dt2",
        "test_roundtrip",
        "test_safe_cut_dn2",
        "test_safe_cut_dt2",
        "test_upload_dn2",
        "test_upload_dt2",
    ):
        assert f"tests/digikit/{name}.py" in got, name
    assert "tests/patchkit/test_patchkit_dkrt_machine.py" in got
    # digikit/pluck is the source of test/patched-pluck's DSP: the patched-in twin's tests follow
    assert "tests/digikit/test_menu_dt2.py" in selected(
        "modules/machines/pluck/src/dsp/pluck.s"
    )
    assert "tests/patchkit/test_pluck.py" in selected(
        "modules/machines/pluck/src/dsp/pluck.s"
    )


CONFORMANCE = "tests/modules/test_machine_conformance.py"


@pytest.mark.parametrize(
    "path",
    [
        "modules/machines/pluck/module.toml",
        "modules/machines/pluck/src/dsp/pluck.s",
        "tests/fixtures/modules/sine/src/dsp/sine.s",
        "tests/fixtures/modules/sine/module.toml",
        "modules/core/dsp-voice/src/dsp/dsp-voice-dn2.s",
    ],
)
def test_a_machine_change_selects_the_conformance_suite(path: str) -> None:
    assert CONFORMANCE in selected(path)
    assert "python-tests" in verify.steps_for([path])


@pytest.mark.parametrize(
    "path", ["modules/fx/master-gain/module.toml", "modules/core/params/module.toml"]
)
def test_other_modules_do_not_select_the_conformance_suite(path: str) -> None:
    assert CONFORMANCE not in selected(path)


@pytest.mark.parametrize(
    "path",
    [
        "tools/patchkit/pk_conform.py",
        "tools/patchkit/pk_sharc.py",
        "tests/support/machine_rig.py",
        "tests/support/conform_worker.py",
    ],
)
def test_the_conformance_machinery_selects_the_conformance_suite(path: str) -> None:
    assert verify_route.CONFORMANCE in selected(path)


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/modules/fx-gain/src/dsp/fx_gain.s",
        "tests/fixtures/modules/fx-gain/module.toml",
        "modules/core/fx/src/dsp/fx.s",
        "modules/core/fx/module.toml",
        "tools/patchkit/pk_conform.py",
        "tests/support/fx_rig.py",
        "tests/support/fx_conform_worker.py",
    ],
)
def test_an_effect_change_selects_the_fx_conformance_suite(path: str) -> None:
    assert verify_route.FX_CONFORMANCE in selected(path)


@pytest.mark.parametrize(
    "path", ["modules/machines/pluck/module.toml", "modules/core/params/module.toml"]
)
def test_other_modules_do_not_select_the_fx_conformance_suite(path: str) -> None:
    assert verify_route.FX_CONFORMANCE not in selected(path)


@pytest.mark.parametrize("module", sorted(verify_route.FRAMEWORK))
def test_framework_module_selects_both_suites(module: str) -> None:
    path = f"{mpath(module)}/module.toml"
    assert {"tests/patchkit", "tests/digikit"} <= selected(path)
    assert {"python-tests", "rust-tests"} <= set(verify.steps_for([path]))
    assert not set(verify.steps_for([path])) & PCM_STEPS


def test_module_changes_select_no_pcm_step() -> None:
    for module in verify_route.manifests():
        steps = verify.steps_for([f"{mpath(module)}/module.toml"])
        assert not set(steps) & PCM_STEPS, module


def test_module_change_drops_no_test_that_names_a_dependent() -> None:
    """For every module, the selection holds each test file that names it or a module that
    depends on it, and the test files of each scenario that does. The text scan here is
    independent of verify_route's (it reads comments too, so it can only find more)."""
    mods = verify_route.manifests()
    scenarios = sorted((ROOT / "tests/digikit/scenarios").glob("*.toml"))
    for mid in mods:
        got = selected(f"{mpath(mid)}/module.toml") | set(verify.PY_GATE)
        if "tests/patchkit" in got:
            continue
        hit = verify_route.dependents({mid})
        for test in sorted((ROOT / "tests").rglob("test_*.py")):
            if "node_modules" in test.parts or "devtools" in test.parts:
                continue
            if any(m in test.read_text() for m in hit):
                assert test.relative_to(ROOT).as_posix() in got, (mid, test.name)
        for scen in scenarios:
            if any(m in scen.read_text() for m in hit):
                rel = scen.relative_to(ROOT).as_posix()
                assert set(selected(rel)) <= got, (mid, scen.name)


def test_dependents_follow_requires_and_shared_sources() -> None:
    assert "test/patched-pluck" in verify_route.dependents({"core/machines"})
    assert "test/patched-pluck" in verify_route.dependents({"digikit/pluck"})
    assert verify_route.dependents({"digikit/master-gain"}) == {"digikit/master-gain"}


def test_unknown_module_path_stays_broad() -> None:
    steps = verify.steps_for(["modules/no/such/module.toml"])
    assert {"python-tests", "rust-tests", "web"} <= set(steps)
    assert {"tests/patchkit", "tests/digikit"} <= selected(
        "modules/no/such/module.toml"
    )


def test_web_only_for_what_it_reads() -> None:
    assert "web" in verify.steps_for(["apps/web/src/x.ts"])
    assert "web" in verify.steps_for(["packages/dkupload/src/lib.rs"])
    assert "rust-tests" in verify.steps_for(["packages/dkupload/src/lib.rs"])
    for path in (
        "tools/device/dkrun.py",
        "tests/digikit/scenarios/upload-quick.toml",
        "tools/patchkit/pk_digikit_scen.py",
        "tests/fixtures/modules/dyn-demo/src/x.s",
    ):
        assert "web" not in verify.steps_for([path]), path


def test_routing_changes_select_everything() -> None:
    for path in ("tools/devtools/verify.py", "tools/devtools/verify_route.py"):
        assert verify.steps_for([path]) == verify.EVERYTHING
        got = verify.python_tests([path], False)
        assert set(verify.python_tests([], True)) <= set(got)
    # the lane helper is checked by the routing tests only
    assert verify.steps_for(["tools/devtools/verify_pre.py"]) == (
        "python-tests",
        "smoke",
    )
    assert "tests/devtools/test_verify.py" in verify.python_tests(
        ["tools/devtools/verify_pre.py"], False
    )


def test_gate_files_always_run() -> None:
    for path in (
        "modules/fx/master-gain/src/cf/gain.s",
        "tools/device/mmon.py",
        "tests/digikit/scenarios/upload-quick.toml",
    ):
        assert set(verify.PY_GATE) <= set(verify.python_tests([path], False)), path


def test_runtime4_merge_runs_no_pcm_step() -> None:
    """The range 457f515b..29086ff7 (one mise.toml task, device tools, runtime objects) ran all 13
    steps; its files now select the web bundle, Rust and Python tests and nothing that renders."""
    files = [
        "modules/core/digikit-menu/build/digikit-menu-rs.dko.json",
        "modules/core/runtime/build/runtime.dko.json",
        "modules/core/runtime/src/rs/src/upload.rs",
        "tests/fixtures/modules/dyn-demo/build/dyn-demo.dko.json",
        "tests/digikit/scenarios/menu-dkrun-pluck.toml",
        "tests/digikit/test_dkrun.py",
        "tools/device/dkrun.py",  # the mise.toml dk:run task
        "tools/patchkit/pk_digikit_scen.py",
        "tools/patchkit/pk_digikit_upload.py",
    ]
    assert verify.steps_for(files) == ("web", "python-tests", "rust-tests", "smoke")


def test_rust_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(changed: list[str]) -> list[str] | None:
        seen.append(changed)
        return ["patchkit"]

    monkeypatch.setattr(verify.rust_ws, "affected_packages", fake, raising=False)
    assert verify.rust_scope(["modules/fx/master-gain/module.toml"]) == ["patchkit"]
    assert seen == [["modules/fx/master-gain/module.toml"]]
    # a broad mise.toml change and a routing change keep the whole workspace
    assert verify.rust_scope(["mise.toml"]) is None
    assert verify.rust_scope(["tools/devtools/verify.py"]) is None
    assert len(seen) == 1


def test_rust_tests_command_limits_packages_when_rust_ws_can(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify.rust_ws, "__doc__", "no such option")
    assert "--packages" not in verify.rust_tests_command(["patchkit"])
    monkeypatch.setattr(verify.rust_ws, "__doc__", "test --packages A,B")
    cmd = verify.rust_tests_command(["a", "b"])
    assert cmd[-2:] == ["--packages", "a,b"]
    assert "--packages" not in verify.rust_tests_command(None)


def test_head_only_with_explain() -> None:
    with pytest.raises(SystemExit) as info:
        verify.main(["--head", "HEAD"])
    assert info.value.code == 2


def test_verify_pre_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "rust_scope", lambda files: [])
    got = {n: c for n, c, _ in verify_pre.plan(["tools/device/dkrun.py"], Path("OUT"))}
    assert got["rust-tests"] is None and got["pk-build-check"] is None
    assert got["ruff-check"][-1] == "tools/device/dkrun.py"
    assert got["pathcheck"]
    got = {
        n: c
        for n, c, _ in verify_pre.plan(
            ["modules/fx/master-gain/module.toml"], Path("OUT")
        )
    }
    assert got["pk-build-check"] and got["ruff-check"] is None
    got = {
        n: c
        for n, c, _ in verify_pre.plan(["tools/devtools/benchlock.py"], Path("OUT"))
    }
    assert got["mypy"][-1] == "tools/devtools/benchlock.py"


def test_rust_tests_leaves_the_desktop_crate_to_the_desktop_step() -> None:
    from tools.devtools import rust_ws

    assert "digiemu-desktop" in rust_ws.affected_packages(
        ["packages/emulator/control/src/lib.rs"]
    )
    assert "--exclude" in rust_ws.workspace_args("test")
    assert "digiemu-desktop" in rust_ws.workspace_args("test")
    # a routed run whose crates include the app (or the whole workspace) gets the desktop step
    assert verify.with_desktop(
        ("rust-tests",), ["digi-control", "digiemu-desktop"]
    ) == ("desktop", "rust-tests")
    assert verify.with_desktop(("rust-tests",), None) == ("desktop", "rust-tests")
    assert verify.with_desktop(("rust-tests",), ["periph"]) == ("rust-tests",)
    assert verify.with_desktop(("python-tests",), None) == ("python-tests",)
    assert verify.with_desktop(("desktop", "rust-tests"), None) == (
        "desktop",
        "rust-tests",
    )


# ---- smoke step ------------------------------------------------------------------------------------


def test_smoke_runs_with_any_gate_step_not_for_docs() -> None:
    assert "smoke" in verify.steps_for(["tools/device/dkscreen.py"])
    assert "smoke" in verify.steps_for(["packages/emulator/machine/src/lib.rs"])
    assert "smoke" in verify.EVERYTHING
    assert "smoke" not in verify.steps_for(["docs/x.md"])
    assert verify.steps_for(["AGENTS.md"]) == ()


def test_smoke_waits_for_build_daemons_and_runs_after_it() -> None:
    nodes, deps = verify.plan(("smoke",))
    assert deps["smoke"] == ("build-daemons",)
    assert nodes.index("build-daemons") < nodes.index("smoke")
    assert "build" not in nodes
    assert "smoke" in verify.DAEMON_NODES


def test_smoke_alone_with_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert verify.main(["--explain", "--only", "smoke"]) == 0
    out = capsys.readouterr().out
    assert "smoke" in out
    assert "pins" not in out
