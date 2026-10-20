"""The tests the changed files select, for a lane while it iterates. Not the merge gate.

    mise run test:changed [-- --base BRANCH] [--explain] [--deep] [--foreground]

Selects, for the files changed since the merge base with BASE (default: the uncommitted changes): the
changed test files, the tests that import a changed tools/ or tests/ module (through the import graph,
following only tests/ helpers and conftest files) or run it by name in a subprocess, the tests a changed
module of modules/ names, and the Rust tests of the affected crates. No PY_GATE, no build step (a test
that needs a daemon builds it), digikit scenarios on the quick tier, never a test directory. It prints
one line per check; a failed check lists the failed files, their failing tests and log paths.
`--explain` lists each selected file with the reason and what the merge gate (`mise run verify`) adds. A test that reaches
a changed module only through other tools modules runs with --deep. It runs at nice 10 (--foreground:
normal priority) and sets cargo's and libtest's pools from the verify CPU budget.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import grimp

ROOT = Path(__file__).resolve().parent.parent.parent

from tools import python_env  # noqa: E402
from tools.devtools import (  # noqa: E402
    test_outcomes,
    verify,
    verify_budget,
    verify_route,
)
from tools.emulator import digi_env  # noqa: E402
from tools.patchkit import modroots, pk_conform  # noqa: E402


@dataclass
class Selection:
    tests: dict[str, str] = field(default_factory=dict)  # test file -> reason
    gate_adds: list[str] = field(default_factory=list)  # what `mise run verify` adds
    deep_only: dict[str, str] = field(default_factory=dict)  # selected by --deep only


# A module named in a string: `-m tools.x.y`, `-m support.x` (tests/ is on the pytest path), or a script path.
_RUN_NAME = re.compile(r"(?:tools|support)(?:\.\w+)+|(?:tools|tests)/[\w/]+\.py")


def _named_module(text: str) -> str | None:
    if text.startswith("support."):
        return "tests." + text
    if text.endswith(".py"):
        return module_of(text)
    return text


@cache
def graph() -> grimp.ImportGraph:
    """The import graph of tools/ and tests/, corrected in three ways:
    - an import inside a function of a tools module is dropped: it runs only when that function runs, and
      such edges join unrelated tools (tools/emulator/digi_env.py reaches all of patchkit through one);
    - a test's import of a helper by its pytest path (`support.x`, a sibling `scen_cases`) is an edge to the
      tests.* module (grimp does not resolve those names);
    - a module named by a whole string constant (`[sys.executable, "-m", "tools.x.y"]`, a script path) is an
      edge: the module runs it in a subprocess."""
    # tests/ is a namespace package; grimp reads it all the same
    g = grimp.build_graph("tools", "tests", cache_dir=None)
    mods = set(g.modules)
    for mod in sorted(mods):
        try:
            text = (ROOT / path_of(mod)).read_text(encoding="utf-8")
        except OSError:
            continue
        if mod.startswith("tools."):
            lines = text.splitlines()
            for dst in sorted(g.find_modules_directly_imported_by(mod)):
                details = g.get_import_details(importer=mod, imported=dst)
                if details and all(
                    lines[d["line_number"] - 1][:1] in (" ", "\t") for d in details
                ):
                    g.remove_import(importer=mod, imported=dst)
        try:
            nodes = list(ast.walk(ast.parse(text)))
        except SyntaxError:
            nodes = []
        targets = {
            _named_module(n.value) or ""
            for n in nodes
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and _RUN_NAME.fullmatch(n.value)
        }
        if mod.startswith("tests."):
            # pytest puts tests/ and each test's own directory on the path: `support.x`, `from support import x`
            # and a sibling helper (`from scen_cases import run` in tests/digikit) are tests.* modules
            here = mod.rsplit(".", 1)[0]
            for n in nodes:
                if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    names = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
                elif isinstance(n, ast.Import):
                    names = [a.name for a in n.names]
                else:
                    continue
                targets |= {f"tests.{x}" for x in names} | {
                    f"{here}.{x}" for x in names
                }
        for dst in sorted(targets & mods - {mod}):
            g.add_import(importer=mod, imported=dst)
    return g


def module_of(path: str) -> str | None:
    """The dotted module of a .py file under tools/ or tests/."""
    if not path.endswith(".py") or not path.startswith(("tools/", "tests/")):
        return None
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def path_of(module: str) -> str:
    base = module.replace(".", "/")
    return f"{base}.py" if (ROOT / f"{base}.py").is_file() else f"{base}/__init__.py"


def is_test_file(path: str) -> bool:
    return (
        path.startswith("tests/")
        and path.rsplit("/", 1)[-1].startswith("test_")
        and (path.endswith(".py"))
    )


def conftest_tests(conftest_path: str) -> list[str]:
    """The test files below the directory of a conftest, recursively."""
    base = (ROOT / conftest_path).parent
    found = (p.relative_to(ROOT).as_posix() for p in base.rglob("test_*.py"))
    return sorted(p for p in found if is_test_file(p))


@cache
def test_texts() -> dict[str, str]:
    return {
        p: (ROOT / p).read_text(encoding="utf-8")
        for p in sorted(
            q.relative_to(ROOT).as_posix() for q in (ROOT / "tests").rglob("test_*.py")
        )
        if "node_modules" not in p
    }


def importing_tests(module: str) -> dict[str, str]:
    """Test path -> reason for the tests that import MODULE, directly or through a tests/ helper, and
    those whose text runs it by name."""
    g = graph()
    out: dict[str, str] = {}
    by_conftest: dict[str, str] = {}
    seen = {module}
    todo = [module]
    while todo:
        cur = todo.pop(0)
        via = "" if cur == module else f" via {cur}"
        for imp in sorted(g.find_modules_that_directly_import(cur)):
            if imp in seen or not imp.startswith("tests."):
                continue
            seen.add(imp)
            path = path_of(imp)
            if imp.rsplit(".", 1)[-1] == "conftest":
                if path == "tests/conftest.py":
                    continue  # the caller puts the root conftest in gate_adds
                for t in conftest_tests(path):
                    by_conftest.setdefault(t, f"{path} imports {module}")
            elif is_test_file(path):
                out.setdefault(path, f"imports {module}{via}")
            else:
                todo.append(imp)
    for t, r in by_conftest.items():
        out.setdefault(t, r)
    # a helper of tests/ is imported as `support.x` (tests/ is on the pytest path), which the graph
    # does not see as tests.support.x
    if module.startswith("tests."):
        short = re.escape(module.removeprefix("tests."))
        pat = re.compile(rf"^\s*(?:from\s+{short}\s+import|import\s+{short}\b)", re.M)
        for p, text in test_texts().items():
            if pat.search(text):
                out.setdefault(p, f"imports {module}")
    path = path_of(module)
    quoted = (f'"{module}"', f"'{module}'", path)
    for p, text in test_texts().items():
        if any(q in text for q in quoted):
            out.setdefault(p, f"runs {module}")
    return out


def deep_tests(module: str) -> dict[str, str]:
    """Test files that reach MODULE through any chain of imports and that importing_tests did not take."""
    direct = importing_tests(module)
    out: dict[str, str] = {}
    for imp in sorted(graph().find_downstream_modules(module)):
        path = path_of(imp)
        if is_test_file(path) and path not in direct:
            out[path] = f"reaches {module} through tools"
    return out


def gate_tests(files: list[str]) -> tuple[dict[str, str], list[str]]:
    """For the merge gate (tools/devtools/verify.py): test path -> reason for the tests that reach a changed
    Python module of tools/ or tests/ through any chain of the graph or run it by name, and the changed files
    whose module the root conftest reaches (every test imports them: verify keeps their directory suites)."""
    g = graph()
    out: dict[str, str] = {}
    broad: list[str] = []
    for f in files:
        module = module_of(f)
        if module is None or is_test_file(f) or module not in g.modules:
            continue
        if "tests.conftest" in g.find_downstream_modules(module):
            broad.append(f)
        for t, reason in {**importing_tests(module), **deep_tests(module)}.items():
            if not t.startswith("tests/scenarios/"):
                out.setdefault(t, reason)
    return out, broad


def framework_tests(mid: str) -> list[str]:
    """The narrow set for a module a framework module depends on: the tests that name MID itself, the
    conformance suite for a machine module, the digikit tests of the scenarios that name it."""
    tests = [t for t, ids in verify_route.test_refs().items() if mid in ids]
    if mid in verify_route.CONFORMANCE_WRAPPERS or pk_conform.is_machine_module(mid):
        tests.append(verify_route.CONFORMANCE)
    kinds = {
        kind for kind, ids in verify_route.scenario_refs().values() if mid in ids
    } | verify_route.runner_kinds({mid})
    if kinds:
        tests += verify_route.scenario_tests(kinds)
    return list(dict.fromkeys(tests))


def select(files: list[str], deep: bool = False) -> Selection:
    sel = Selection()

    dropped = False

    def selectable(path: str) -> bool:
        nonlocal dropped
        if path.startswith("tests/scenarios/"):
            dropped = True
            return False
        return True

    def add(path: str, reason: str) -> None:
        if selectable(path) and (ROOT / path).is_file():
            sel.tests.setdefault(path, reason)
            sel.deep_only.pop(path, None)

    for f in files:
        if f.endswith(".md"):
            continue
        if verify.is_changed_test(f) and (ROOT / f).is_file():
            add(f, "changed")
        fine = verify_route.fine(f)
        if fine is not None:
            for entry in fine.tests:
                if (ROOT / entry).is_dir():
                    sel.gate_adds.append(f"{entry} ({fine.label})")
                else:
                    add(entry, fine.label)
            mid = modroots.module_id(f)
            if (
                mid is not None
                and f.startswith(modroots.REPO_ROOTS)
                and verify_route.dependents({mid}) & verify_route.FRAMEWORK
            ):
                for t in framework_tests(mid):
                    add(t, f"{fine.label}: names {mid}")
        module = module_of(f)
        if module is None or is_test_file(f) or module not in graph().modules:
            continue
        for t, reason in importing_tests(module).items():
            add(t, reason)
        if "tests.conftest" in graph().find_modules_that_directly_import(module):
            sel.gate_adds.append(f"tests (the root conftest imports {module})")
        for t, reason in deep_tests(module).items():
            if deep:
                add(t, reason)
            elif t not in sel.tests and selectable(t):
                sel.deep_only.setdefault(t, reason)
    if dropped:
        sel.gate_adds.append(
            "tests/scenarios (the check steps run the scenario oracles)"
        )
    steps = [
        s for s in verify.steps_for(files) if s not in ("python-tests", "rust-tests")
    ]
    if steps:
        sel.gate_adds.append(f"steps: {', '.join(steps)}")
    plan_ = verify.python_test_plan(files, False)
    label = {p: s for p, s in plan_}
    more = [
        (rel, label[entry])
        for rel, entry in verify.python_files([p for p, _ in plan_])
        if rel not in sel.tests
    ]
    if more:
        sources = ", ".join(dict.fromkeys(s for _, s in more))
        sel.gate_adds.append(f"python-tests: {len(more)} more files ({sources})")
    return sel


def plan(
    files: list[str], out_dir: Path, deep: bool = False
) -> list[tuple[str, list[str] | None, str]]:
    """(name, command, note): a command of None is skipped, with the note why."""
    tests = list(select(files, deep).tests)
    scope = verify.rust_scope(files)
    out: list[tuple[str, list[str] | None, str]] = [
        (
            "python-tests",
            verify.python_tests_command(
                out_dir, tests, verify.python_jobs(verify_budget.resolve_budget())
            )
            if tests
            else None,
            "no test selected",
        )
    ]
    if scope == []:
        out.append(("rust-tests", None, "no changed file reaches a Rust test"))
    elif scope is None:
        out.append(
            ("rust-tests", None, "needs the whole workspace: left to `mise run verify`")
        )
    else:
        out.append(("rust-tests", verify.rust_tests_command(scope), ""))
    return out


def _tail(path: Path, n: int, indent: str) -> list[str]:
    """The last N lines of the file at PATH, indented."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [f"{indent}{line}" for line in lines[-n:]]


def _failing_cases(xml: Path, limit: int = 10) -> list[str]:
    """`name: message` for the testcases of the JUnit file XML that have a failure or error child."""
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return []
    out: list[str] = []
    for case in root.iter("testcase"):
        child = case.find("failure")
        if child is None:
            child = case.find("error")
        if child is None:
            continue
        text = child.get("message") or child.text or ""
        first = (text.strip().splitlines() or [""])[0][:200]
        out.append(f"    {case.get('name', '?')}: {first}")
        if len(out) >= limit:
            break
    return out


def failure_report(out_dir: Path) -> tuple[str, list[str]]:
    """The PASS/FAIL headline of a python-tests run and the detail lines for its failed files.

    Reads OUT_DIR/summary.tsv, OUT_DIR/xml/<slug>.xml and OUT_DIR/logs/<slug>.log."""
    rows: list[dict[str, str]] = []
    with (out_dir / "summary.tsv").open(encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            if line.strip():
                rows.append(
                    dict(zip(header, line.rstrip("\n").split("\t"), strict=False))
                )
    seconds = sum(float(r.get("seconds") or 0) for r in rows)

    def num(r: dict[str, str], key: str) -> int:
        return int(r.get(key) or 0)

    bad = [
        r
        for r in rows
        if r["status"] != "OK" or num(r, "failed") + num(r, "errors") > 0
    ]
    if not bad:
        passed = sum(num(r, "passed") for r in rows)
        skipped = sum(num(r, "skipped") for r in rows)
        return (
            f"PASS python-tests: {len(rows)} files, {passed} passed, "
            f"{skipped} skipped ({seconds:.0f} s)",
            [],
        )
    head = (
        f"FAIL python-tests: {len(bad)} of {len(rows)} files failed ({seconds:.0f} s)"
    )
    detail: list[str] = []
    for r in bad:
        slug = test_outcomes.slug_of(r["file"])
        log = out_dir / "logs" / f"{slug}.log"
        try:
            shown = log.relative_to(ROOT)
        except ValueError:
            shown = log
        detail.append(
            f"  {r['file']}: {r['status']}, {num(r, 'failed')} failed, "
            f"{num(r, 'errors')} errors  (log: {shown})"
        )
        cases = _failing_cases(out_dir / "xml" / f"{slug}.xml")
        detail += cases or _tail(log, 15, "    ")
    return head, detail


_RUST_KEEP = re.compile(r"^(---- |test result:|FAIL)|panicked at")


def rust_failure_lines(output: str) -> list[str]:
    """The lines of cargo OUTPUT that show a failure (at most 40), else its last 30 lines."""
    lines = output.strip().splitlines()
    hits = [line for line in lines if _RUST_KEEP.search(line)]
    return hits[:40] if hits else lines[-30:]


def _count_gate_adds(gate_adds: list[str]) -> tuple[int, int]:
    """(steps, more python files) that the gate adds."""
    steps = 0
    more = 0
    for line in gate_adds:
        if line.startswith("steps:"):
            steps = len(line.removeprefix("steps:").split(","))
        m = re.match(r"python-tests: (\d+) more files", line)
        if m:
            more = int(m.group(1))
    return steps, more


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tests the changed files select; not the merge gate."
    )
    ap.add_argument("--base", default="HEAD", help="merge base ref (default: HEAD)")
    ap.add_argument(
        "--explain", action="store_true", help="print the selection, run nothing"
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help="also run the tests that reach a changed module only through other tools modules",
    )
    ap.add_argument(
        "--foreground",
        action="store_true",
        help="run at normal priority (default: nice 10, like `verify`)",
    )
    args = ap.parse_args(argv)
    if not args.explain:
        verify_budget.lower_priority(args.foreground)
    files = verify.changed_files(args.base)
    sel = select(files, args.deep)
    scope = verify.rust_scope(files)
    if scope is None:
        rust = "whole workspace, left to the gate"
    else:
        rust = ", ".join(scope) if scope else "no Rust test"
    header = (
        f"test:changed: {len(sel.tests)} test files for {len(files)} changed files "
        f"since {args.base}; rust: {rust}"
    )
    if sel.deep_only and not args.deep:
        header += f"; {len(sel.deep_only)} more with --deep"
    print(header)
    if args.explain:
        for test, reason in sel.tests.items():
            print(f"  {test}  ({reason})")
        print("the gate adds:")
        for line in sel.gate_adds or ["nothing"]:
            print(f"  {line}")
    failed: list[str] = []
    out_dir = Path("OUT") if args.explain else Path(digi_env.run_dir("test-changed"))
    for name, cmd, note in plan(files, out_dir, args.deep):
        if cmd is None:
            if args.explain:
                print(f"SKIP {name}: {note}", flush=True)
            continue
        if args.explain:
            print(f"RUN  {name}: {' '.join(cmd[:8])}{' ...' if len(cmd) > 8 else ''}")
            continue
        start = time.monotonic()
        env = verify_budget.bounded_env(name, verify_budget.resolve_budget())
        env.setdefault("DIGIKIT_TIER", "quick")  # bounded_env starts from os.environ
        done = subprocess.run(
            cmd, cwd=ROOT, env=python_env(env), capture_output=True, text=True
        )
        seconds = time.monotonic() - start
        output = done.stdout + done.stderr
        if name == "python-tests":
            report: tuple[str, list[str]] | None
            try:
                report = failure_report(out_dir)
            except (OSError, KeyError):
                report = None
            if report is not None and not done.returncode:
                print(report[0], flush=True)
                continue
            failed.append(name)
            if report is None or report[0].startswith("PASS"):
                tail = "\n".join(output.strip().splitlines()[-15:])
                print(f"FAIL {name} ({seconds:.0f} s)\n{tail}", flush=True)
            else:
                print("\n".join([report[0], *report[1]]), flush=True)
        elif done.returncode:
            failed.append(name)
            lines = "\n".join(rust_failure_lines(output))
            print(f"FAIL {name} ({seconds:.0f} s)\n{lines}", flush=True)
        else:
            print(f"PASS {name} ({seconds:.0f} s)", flush=True)
    if failed:
        print(f"test:changed: FAILED: {', '.join(failed)}.")
    elif args.explain:
        print("test:changed: explained, nothing run.")
    else:
        steps, more = _count_gate_adds(sel.gate_adds)
        print(
            f"test:changed: all passed. The gate adds {steps} steps and {more} more "
            "python files (--explain lists them)."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
