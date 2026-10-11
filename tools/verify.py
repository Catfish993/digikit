"""One command for the exactness gate (the handover's "How to verify after any change").

    uv run python tools/verify.py            # quick: build, DN2 pins, scenarios:check
    uv run python tools/verify.py --full     # + no-AOT suite, tier sweep, Python and Rust tests, DT2 tone
    uv run python tools/verify.py --only tier-sweep,dt2-tone

Prints one PASS/FAIL line per step; each step's output is in out/runs/verify/<stamp>/<step>.log.
Exits 1 when any step failed.
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
PY_TESTS = (
    "tests/test_sharc_golden.py",
    "tests/test_sharc_denormal.py",
    "tests/test_sharc_rsgen_density.py",
    "tests/test_lint.py",
    "tests/test_types.py",
)
STEPS = (
    "build",
    "pins",
    "check",
    "check-no-aot",
    "tier-sweep",
    "python-tests",
    "rust-tests",
    "dt2-tone",
)
QUICK = ("build", "pins", "check")


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
    ) -> None:
        """Run one step, log its output and print its line."""
        start = time.monotonic()
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        text = proc.stdout + proc.stderr
        (self.out / f"{name}.log").write_text(text)
        problem = (
            f"exit {proc.returncode}"
            if proc.returncode
            else (check(text) if check else None)
        )
        self.report(name, problem, time.monotonic() - start, text)


def tier_sweep(gate: Gate) -> None:
    """The FX scenarios must give the same PCM in the default and no-AOT configurations."""
    start = time.monotonic()
    jobs = str(coupled.default_jobs())
    shas: dict[str, dict[str, str]] = {}
    for label, env in (("default", None), ("no-aot", no_aot_env())):
        for name in FX_FILES:
            report = gate.out / f"tier-{label}-{Path(name).stem}" / "report.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            cmd = [*PY, "tools/coupled.py", "scenario", "--scenarios", name, "--all"]
            cmd += ["--no-build", "--jobs", jobs, "--out", str(report)]
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, text=True
            )
            text = proc.stdout + proc.stderr
            (report.parent / "run.log").write_text(text)
            # Exit 2 is a failed gate in an unpinned FX scenario; only the PCM matters here.
            if proc.returncode not in (0, 2) or not report.exists():
                problem = f"{label} {name}: exit {proc.returncode}"
                gate.report("tier-sweep", problem, time.monotonic() - start, text)
                return
            for result in coupled.scenario_results(json.loads(report.read_text())):
                shas.setdefault(result["name"], {})[label] = result["sha256"]
    differ = [n for n, s in shas.items() if s.get("default") != s.get("no-aot")]
    problem = (
        f"{len(differ)} of {len(shas)} differ: {', '.join(differ[:5])}"
        if differ
        else None
    )
    gate.report("tier-sweep", problem, time.monotonic() - start)
    if not differ:
        print(f"     {len(shas)} of {len(shas)} scenarios equal", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the exactness gate, one line per step."
    )
    ap.add_argument(
        "--full", action="store_true", help="every step (default: build, pins, check)"
    )
    ap.add_argument("--only", help=f"comma-separated steps from: {', '.join(STEPS)}")
    args = ap.parse_args(argv)
    steps = (
        tuple(args.only.split(",")) if args.only else (STEPS if args.full else QUICK)
    )
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        ap.error(f"unknown steps: {', '.join(unknown)}")
    out = Path(digi_env.run_dir("verify"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"verify: logs in {out}", flush=True)
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
    for step in steps:
        if step == "build":
            gate.run("build", [*PY, "tools/coupled.py", "build"])
        elif step == "pins":
            gate.run(
                "pins", [*PY, "tools/coupled.py", "scenario", "--all", "--no-build"]
            )
        elif step == "check":
            gate.run("check", scenario_tests, check=pytest_ran)
        elif step == "check-no-aot":
            gate.run("check-no-aot", scenario_tests, env=no_aot_env(), check=pytest_ran)
        elif step == "tier-sweep":
            tier_sweep(gate)
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
        elif step == "dt2-tone":
            gate.run("dt2-tone", [*PY, "tools/dt2_sample_check.py"])
    if gate.failed:
        print(f"verify: FAILED: {', '.join(gate.failed)}", flush=True)
        return 1
    print("verify: all passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
