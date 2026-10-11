"""One command for the exactness gate, scaled to what changed.

    uv run python tools/verify.py                  # steps for the uncommitted changes
    uv run python tools/verify.py --base BRANCH    # a lane: its commits since BRANCH plus uncommitted changes
    uv run python tools/verify.py --full           # every step, with the full 134-scenario tier sweep
    uv run python tools/verify.py --full --dt2     # plus the full DT2 FX tier sweep
    uv run python tools/verify.py --only pins,check

Prints the chosen steps, then one PASS/FAIL line per step; each step's output is in
out/runs/verify/<stamp>/<step>.log. Exits 1 when any step failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import coupled  # noqa: E402
import digi_env  # noqa: E402
import fastcov  # noqa: E402

PY = ["uv", "run", "python"]
FX_FILES = ("scenarios/dn2-trackfx.json", "scenarios/dn2-sendfx.json")
DT2_FX_FILES = ("scenarios/dt2-trackfx.json", "scenarios/dt2-sendfx.json")
PY_TESTS = (
    "tests/test_sharc_golden.py",
    "tests/test_sharc_denormal.py",
    "tests/test_sharc_rsgen_density.py",
    "tests/test_lint.py",
    "tests/test_types.py",
)
STEPS = (
    "build",
    "desktop",
    "web",
    "pins",
    "check",
    "check-no-aot",
    "tier-quick",
    "tier-sweep",
    "python-tests",
    "rust-tests",
    "card",
    "dt2-tone",
    "dt2-check",
    "dt2-tier-quick",
    "dt2-tier-sweep",
)
# Every seventh FX scenario of each file: about 20 of 134, at least one per FX family.
TIER_QUICK_EVERY = 7
EVERYTHING = tuple(s for s in STEPS if s not in ("tier-sweep", "dt2-tier-sweep"))
FALLBACK = ("build", "desktop", "pins", "check", "python-tests")
# The first rule whose prefix matches a changed path decides that path's steps.
RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("docs/", "README", "AGENTS.md", "CLAUDE.md", "scenarios/README"), ()),
    (
        (
            "scenarios/dt2-",
            "tests/scenarios/test_dt2",
            "tools/gen_dt2fx.py",
            "tools/dt2fx.py",
        ),
        ("dt2-check", "dt2-tier-quick", "python-tests"),
    ),
    (("packages/web/",), ("web",)),
    (("packages/desktop/",), ("desktop", "web")),
    (
        (
            "tools/sharc_core/",
            "tools/sharc_transpile.py",
            "tools/sharc_rsgen.py",
            "native/sharc-gen/",
            "native/sharc-rt/",
        ),
        EVERYTHING,
    ),
    (
        ("native/sharc/src/fast/", "native/sharc-fast-cl/", "native/sharc-fast-wasm/"),
        ("build", "pins", "check", "check-no-aot", "tier-quick", "rust-tests"),
    ),
    (
        ("native/sharc/",),
        (
            "build",
            "desktop",
            "pins",
            "check",
            "check-no-aot",
            "tier-quick",
            "rust-tests",
            "dt2-tone",
        ),
    ),
    (("native/control/",), ("desktop", "rust-tests", "card")),
    (
        ("native/plusdrive/", "native/card/", "tools/card_check.py"),
        ("desktop", "rust-tests", "card"),
    ),
    (
        ("native/",),
        ("build", "desktop", "pins", "check", "rust-tests", "card", "dt2-tone"),
    ),
    (
        (
            "scenarios/",
            "tests/scenarios/",
            "tools/fxcheck.py",
            "tools/audiocheck.py",
            "tools/scenario_dsl.py",
        ),
        ("check", "python-tests"),
    ),
    (
        (
            "tools/coupled.py",
            "tools/digi_env.py",
            "tools/native_emu.py",
            "tools/native_pgo.py",
            "tools/native_target.py",
            "tools/fastcov.py",
        ),
        ("build", "pins", "python-tests"),
    ),
    (("tools/", "tests/", "pyproject.toml"), ("python-tests",)),
    (
        ("Cargo.toml", "Cargo.lock", "mise.toml", "package.json", "pnpm-lock.yaml"),
        EVERYTHING,
    ),
)


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def changed_files(base: str) -> list[str]:
    """Files changed since the merge base with BASE, committed or not, plus untracked files."""
    since = git_lines("merge-base", base, "HEAD")[0] if base != "HEAD" else "HEAD"
    files = set(git_lines("diff", "--name-only", since))
    files |= set(git_lines("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def steps_for(files: list[str]) -> tuple[str, ...]:
    """The steps the changed files need, in gate order."""
    wanted: set[str] = set()
    for path in files:
        rule = next(
            (steps for prefixes, steps in RULES if path.startswith(prefixes)), FALLBACK
        )
        wanted.update(rule)
    return tuple(s for s in STEPS if s in wanted)


def no_aot_env() -> dict[str, str]:
    """The fast-tier-only configuration (fastcov `static-insns+follow`)."""
    _, env = fastcov.parse_config("static-insns+follow")
    return {**os.environ, **env}


def pytest_ran(text: str) -> str | None:
    """A pytest run that only skipped is not a pass."""
    lines = text.strip().splitlines()
    last = lines[-1] if lines else ""
    return None if " passed" in last else f"nothing passed ({last})"


class Gate:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.failed: list[str] = []

    def report(
        self, name: str, problem: str | None, seconds: float, text: str = ""
    ) -> None:
        if problem is None:
            print(f"PASS {name} ({seconds:.0f} s)", flush=True)
            return
        self.failed.append(name)
        tail = " | ".join(text.strip().splitlines()[-3:])
        print(f"FAIL {name} ({seconds:.0f} s): {problem}: {tail}", flush=True)

    def run(
        self,
        name: str,
        cmd: list[str],
        env: dict[str, str] | None = None,
        check: Callable[[str], str | None] | None = None,
        cwd: Path = ROOT,
    ) -> None:
        """Run one step, log its output and print its line."""
        start = time.monotonic()
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        text = proc.stdout + proc.stderr
        (self.out / f"{name}.log").write_text(text)
        if proc.returncode:
            problem: str | None = f"exit {proc.returncode}"
        else:
            problem = check(text) if check else None
        self.report(name, problem, time.monotonic() - start, text)


def tier(gate: Gate, name: str, every: int, device: str = "dn2") -> None:
    """FX scenarios must give the same PCM in the default and no-AOT configurations.

    DT2 runs its scenarios on the one-sample card; its no-AOT configuration is the
    interpreter alone (`SHARC_AOT=0`, about 80 s per audio second), as DT2 has no
    fast regions."""
    start = time.monotonic()
    jobs = str(coupled.default_jobs())
    shas: dict[str, dict[str, str]] = {}
    dt2 = device == "dt2"
    no_aot = {**os.environ, "SHARC_AOT": "0"} if dt2 else no_aot_env()
    for label, env in (("default", None), ("no-aot", no_aot)):
        for path in DT2_FX_FILES if dt2 else FX_FILES:
            names = sorted(coupled.scenario_seconds(path))[::every]
            report = gate.out / f"{name}-{label}-{Path(path).stem}" / "report.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            cmd = [*PY, "tools/coupled.py", "scenario", "--scenarios", path, *names]
            cmd += ["--no-build", "--jobs", jobs, "--out", str(report)]
            if dt2:
                cmd += ["--device", "dt2", "--dt2-card"]
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, text=True
            )
            text = proc.stdout + proc.stderr
            (report.parent / "run.log").write_text(text)
            # Exit 2 is a failed gate in an unpinned FX scenario; only the PCM matters here.
            if proc.returncode not in (0, 2) or not report.exists():
                problem = f"{label} {path}: exit {proc.returncode}"
                gate.report(name, problem, time.monotonic() - start, text)
                return
            for result in coupled.scenario_results(json.loads(report.read_text())):
                shas.setdefault(result["name"], {})[label] = result["sha256"]
    differ = [n for n, s in shas.items() if s.get("default") != s.get("no-aot")]
    problem = (
        f"{len(differ)} of {len(shas)} differ: {', '.join(differ[:5])}"
        if differ
        else None
    )
    gate.report(name, problem, time.monotonic() - start)
    if not differ:
        print(f"     {len(shas)} of {len(shas)} scenarios equal", flush=True)


def desktop_check(gate: Gate) -> None:
    """The desktop app crate must compile with its own features."""
    env = digi_env.rust_env()
    env["SHARC_GEN_DIR"] = str(digi_env.generated_dir())
    cmd = ["cargo", "check", "--manifest-path", "packages/desktop/src-tauri/Cargo.toml"]
    cmd += ["--target-dir", str(ROOT / "out" / "native" / "target" / "check-desktop")]
    gate.run("desktop", cmd, env=env)


def web_check(gate: Gate) -> None:
    web = ROOT / "packages" / "web"
    gate.run("web-tests", ["node", "--test", "tests/*.test.mjs"], cwd=web)
    gate.run("web-types", ["npx", "astro", "check"], cwd=web)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the exactness gate for what changed, one line per step."
    )
    ap.add_argument(
        "--base",
        default="HEAD",
        help="compare with the merge base of this ref (default: HEAD)",
    )
    ap.add_argument("--full", action="store_true", help="every step, full tier sweep")
    ap.add_argument("--only", help=f"comma-separated steps from: {', '.join(STEPS)}")
    ap.add_argument(
        "--dt2",
        action="store_true",
        help="with --full: the full DT2 FX tier sweep (about 35 min more)",
    )
    args = ap.parse_args(argv)
    if args.only:
        steps = tuple(args.only.split(","))
        unknown = sorted(set(steps) - set(STEPS))
        if unknown:
            ap.error(f"unknown steps: {', '.join(unknown)}")
        why = "--only"
    elif args.full:
        skip = {"tier-quick", "dt2-tier-quick" if args.dt2 else "dt2-tier-sweep"}
        steps = tuple(s for s in STEPS if s not in skip)
        why = "--full"
    else:
        files = changed_files(args.base)
        steps = steps_for(files)
        why = f"{len(files)} changed files since {args.base}"
    out = Path(digi_env.run_dir("verify"))
    out.mkdir(parents=True, exist_ok=True)
    print(
        f"verify: {', '.join(steps) or 'nothing to run'} ({why}); logs in {out}",
        flush=True,
    )
    gate = Gate(out)
    scenario_tests = [
        *PY,
        "-m",
        "pytest",
        "tests/scenarios",
        "--slow",
        "-q",
        "--no-build",
    ]
    jobs = str(coupled.default_jobs())
    for step in steps:
        if step == "build":
            gate.run("build", [*PY, "tools/coupled.py", "build"])
        elif step == "desktop":
            desktop_check(gate)
        elif step == "web":
            web_check(gate)
        elif step == "pins":
            cmd = [
                *PY,
                "tools/coupled.py",
                "scenario",
                "--all",
                "--no-build",
                "--jobs",
                jobs,
            ]
            gate.run("pins", cmd)
        elif step == "check":
            gate.run("check", scenario_tests, check=pytest_ran)
        elif step == "check-no-aot":
            gate.run("check-no-aot", scenario_tests, env=no_aot_env(), check=pytest_ran)
        elif step == "tier-quick":
            tier(gate, "tier-quick", TIER_QUICK_EVERY)
        elif step == "tier-sweep":
            tier(gate, "tier-sweep", 1)
        elif step == "python-tests":
            gate.run(
                "python-tests", [*PY, "-m", "pytest", "-q", *PY_TESTS], check=pytest_ran
            )
        elif step == "rust-tests":
            cmd = [
                *PY,
                "tools/rust_ws.py",
                "test",
                "--",
                "-p",
                "sharc-native",
                "-p",
                "sharc-rt",
            ]
            gate.run("rust-tests", cmd)
        elif step == "card":
            gate.run("card", [*PY, "tools/card_check.py"])
        elif step == "dt2-tone":
            gate.run("dt2-tone", [*PY, "tools/dt2_sample_check.py"])
        elif step == "dt2-check":
            gate.run(
                "dt2-check", [*scenario_tests, "--device", "dt2"], check=pytest_ran
            )
        elif step == "dt2-tier-quick":
            tier(gate, "dt2-tier-quick", TIER_QUICK_EVERY, "dt2")
        elif step == "dt2-tier-sweep":
            tier(gate, "dt2-tier-sweep", 1, "dt2")
    if gate.failed:
        print(f"verify: FAILED: {', '.join(gate.failed)}", flush=True)
        return 1
    print("verify: all passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
