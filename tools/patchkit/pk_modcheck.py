#!/usr/bin/env python3
"""patchkit: check one module the way the merge gate would, without a patched image or an emulator.

    uv run python -m tools.patchkit.pk_modcheck NAME [--device dt2|dn2]      (mise run module:check NAME)

NAME is a module id (`machines/pluck`) or its last part (`pluck`). A machine module (tools/patchkit/pk_conform.py) runs the conformance
suite, tests/modules/test_machine_conformance.py, for its devices (docs/TESTING.md "Machine conformance"). Then the module's own tests
run: `modules/<group>/<name>/tests/test_*.py`, and until the tests move there, the test files under tests/ whose file name carries the
module's name (`pluck` -> tests/patchkit/test_pluck*.py). One pytest process runs all of them; the exit code is pytest's.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.patchkit import pk_conform  # noqa: E402

CONFORMANCE = "tests/modules/test_machine_conformance.py"


def resolve(name: str) -> str:
    mods = [pk_conform.module_id(d) for d in pk_conform.module_dirs()]
    if name in mods:
        return name
    hit = [m for m in mods if m.split("/", 1)[1] == name]
    if len(hit) != 1:
        raise SystemExit(
            f"{name!r}: {'no such module' if not hit else 'ambiguous: ' + ', '.join(hit)}"
        )
    return hit[0]


def own_tests(mid: str) -> list[str]:
    """Test files of the module: its tests/ directory, then the files under tests/ named after it."""
    found = sorted((ROOT / "modules" / mid / "tests").glob("test_*.py"))
    stem = mid.split("/", 1)[1].replace("-", "_")
    found += [
        p
        for p in sorted((ROOT / "tests").rglob(f"test_*{stem}*.py"))
        if "node_modules" not in p.parts
    ]
    return list(dict.fromkeys(p.relative_to(ROOT).as_posix() for p in found))


def plan(mid: str, device: str | None) -> tuple[list[str], dict[str, str]]:
    """(pytest paths, extra environment) for module MID."""
    paths: list[str] = []
    env: dict[str, str] = {}
    if pk_conform.is_machine_module(mid):
        paths.append(CONFORMANCE)
        env["DIGI_CONFORM_MODULES"] = mid
        if device:
            env["DIGI_CONFORM_DEVICE"] = device
    paths += own_tests(mid)
    return paths, env


def size_lines(mid: str, devices: list[str]) -> list[str]:
    """The `size` line of `patchkit pack` for module MID, once per device (design rule D5). A module that does not pack
    (no ColdFire object, or several, e.g. a patched-in module) gets a "not packed" line with the reason."""
    from tools.patchkit import patchkit

    lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pk-modcheck-") as tmp:
        for dev in devices:
            out = Path(tmp) / f"{mid.rsplit('/', 1)[-1]}-{dev}.dkm"
            r = patchkit.run(
                ["pack", mid, "--device", dev, "-o", str(out)], capture=True
            )
            if r.returncode:
                why = (r.stderr or r.stdout).strip().splitlines()
                lines.append(
                    f"{dev}: size     not packed ({why[-1] if why else 'pack failed'})"
                )
                continue
            lines += [
                f"{dev}: size     {ln.split(': ', 1)[-1]}"
                for ln in r.stdout.splitlines()
                if ln.startswith("size ")
            ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("--device", choices=pk_conform.DEVICES)
    a = ap.parse_args()
    mid = resolve(a.name)
    paths, env = plan(mid, a.device)
    kind = (
        "machine module"
        if CONFORMANCE in paths
        else "module (no machine DSP: no conformance suite)"
    )
    print(f"{mid}: {kind}; pytest {' '.join(paths) or '(nothing to run)'}", flush=True)
    if not paths:
        return 0
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-rf",
        *paths,
    ]
    rc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **env}).returncode
    if rc == 0:
        for line in size_lines(
            mid, [a.device] if a.device else list(pk_conform.DEVICES)
        ):
            print(line, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
