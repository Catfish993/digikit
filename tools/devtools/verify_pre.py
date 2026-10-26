"""Optional combined preflight for a lane. Not the merge gate; not a mandatory follow-up to test:changed.

    mise run verify:pre [-- --base BRANCH]

Runs, for the files changed since the merge base with BASE (default: the uncommitted changes), in order:
ruff (check and format) on the changed Python files of tests/ and tools/, mypy on the changed files of
the TYPED list (tests/devtools/test_types.py), the path check, `pk_build build-all --check` when
modules, patchkit tools or ports changed, the tests `mise run test:changed` selects
(tools/devtools/changed_tests.py), and the Rust tests of the affected crates (verify.rust_scope). It has no
explicit PCM pin or scenario step; selected tests may build or launch a device/emulator. `mise run verify` is the merge gate. It runs at nice 10
(--foreground: normal priority) and sets cargo's and libtest's pools from the verify CPU budget
(tools/devtools/verify_budget.py), as `verify` does for its steps.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from tools import python_env  # noqa: E402
from tools.devtools import changed_tests, verify, verify_budget  # noqa: E402
from tools.emulator import digi_env  # noqa: E402

PATCHKIT_PREFIXES = (
    "modules/",
    "tests/fixtures/modules/",
    "tools/patchkit/",
    "devices/ports/",
    "packages/patchkit/",
)


def typed_files() -> list[str]:
    """The TYPED list of tests/devtools/test_types.py."""
    tree = ast.parse(
        (ROOT / "tests/devtools/test_types.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TYPED" for t in node.targets
        ):
            return list(ast.literal_eval(node.value))
    raise SystemExit("verify:pre: no TYPED list in tests/devtools/test_types.py")


def plan(files: list[str], out_dir: Path) -> list[tuple[str, list[str] | None, str]]:
    """(name, command, note): a command of None is skipped, with the note why."""
    py = [
        f
        for f in files
        if f.endswith(".py")
        and f.startswith(("tests/", "tools/"))
        and (ROOT / f).is_file()
    ]
    typed = [
        e for e in typed_files() if any(f == e or f.startswith(e + "/") for f in files)
    ]
    tests = list(changed_tests.select(files).tests)
    scope = verify.rust_scope(files)
    rust = verify.rust_tests_command(scope)
    out: list[tuple[str, list[str] | None, str]] = [
        (
            "ruff-check",
            [*verify.PY, "-m", "ruff", "check", "--no-cache", *py] if py else None,
            "no changed Python files",
        ),
        (
            "ruff-format",
            [*verify.PY, "-m", "ruff", "format", "--check", "--no-cache", *py]
            if py
            else None,
            "no changed Python files",
        ),
        (
            "mypy",
            [*verify.PY, "-m", "mypy", *typed] if typed else None,
            "no changed TYPED files",
        ),
        ("pathcheck", [*verify.PY, "-m", "tools.devtools.pathcheck"], ""),
        (
            "pk-build-check",
            [*verify.PY, "-m", "tools.patchkit.pk_build", "build-all", "--check"]
            if any(f.startswith(PATCHKIT_PREFIXES) for f in files)
            else None,
            "no module, patchkit tool or port changed",
        ),
        (
            "tests",
            verify.python_tests_command(
                out_dir, tests, verify.python_jobs(verify_budget.resolve_budget())
            )
            if tests
            else None,
            "no test selected (`mise run test:changed --explain` shows why)",
        ),
    ]
    if scope == []:
        out.append(("rust-tests", None, "no changed file reaches a Rust test"))
    elif scope is None:
        out.append(
            ("rust-tests", None, "needs the whole workspace: left to `mise run verify`")
        )
    else:
        out.append(("rust-tests", rust, ""))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Quick lane checks; not the merge gate.")
    ap.add_argument("--base", default="HEAD", help="merge base ref (default: HEAD)")
    ap.add_argument(
        "--explain", action="store_true", help="print the checks, run nothing"
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
    failed: list[str] = []
    out_dir = Path("OUT") if args.explain else Path(digi_env.run_dir("verify-pre"))
    for name, cmd, note in plan(files, out_dir):
        if cmd is None:
            print(f"SKIP {name}: {note}", flush=True)
            continue
        if args.explain:
            print(f"RUN  {name}: {' '.join(cmd[:8])}{' ...' if len(cmd) > 8 else ''}")
            continue
        start = time.monotonic()
        # the checks run one after the other: each gets the whole budget of the gate
        node = {"tests": "python-tests", "rust-tests": "rust-tests"}.get(name, "")
        env = verify_budget.bounded_env(node, verify_budget.resolve_budget())
        done = subprocess.run(
            cmd, cwd=ROOT, env=python_env(env), capture_output=True, text=True
        )
        seconds = time.monotonic() - start
        if done.returncode:
            failed.append(name)
            tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-15:])
            print(f"FAIL {name} ({seconds:.0f} s)\n{tail}", flush=True)
        else:
            print(f"PASS {name} ({seconds:.0f} s)", flush=True)
    verdict = "explained, nothing run" if args.explain else "all passed"
    if failed:
        verdict = f"FAILED: {', '.join(failed)}"
    print(f"verify:pre: {verdict} for {len(files)} changed files since {args.base}.")
    print(
        "verify:pre is an optional preflight, NOT the merge gate; selected tests may build or launch an emulator. "
        "The coordinator runs `mise run verify` once per merge."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
