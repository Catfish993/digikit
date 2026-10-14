#!/usr/bin/env python3
"""Every patchkit emulator proof on its quick tier, one after the other: one summary line per tool and a total.

    uv run python tools/pk_proofs.py [--only gain,drive,...] [--jobs N] [--out DIR]
    mise run patch:proofs

Each tool is `tools/pk_proof_<name>.py --quick` (parts in parallel processes inside the tool, `--jobs`); the tools
run in sequence so that their timings do not disturb each other. Logs and the tools' output go under OUT (default
out/runs/proofs). Exit 0 only when every tool passes. The full tiers run only at integration (AGENTS.md).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# (label, script, extra arguments)
TOOLS = [
    ("gain dt2", "pk_proof_gain.py", ["dt2"]),
    ("gain dn2", "pk_proof_gain.py", ["dn2"]),
    ("drive dt2", "pk_proof_drive.py", ["dt2"]),
    ("voice dt2", "pk_proof_voice.py", []),
    ("machines dt2", "pk_proof_machines.py", []),
    ("pluck dt2", "pk_proof_pluck.py", []),
    ("voice dn2", "pk_proof_voice_dn2.py", []),
    ("machines dn2", "pk_proof_machines_dn2.py", []),
    ("pages dn2", "pk_proof_pages_dn2.py", []),
    ("pluck dn2", "pk_proof_pluck_dn2.py", []),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--only", help="comma-separated tool labels or first words (gain, drive, ...)"
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="passed to every tool (default: the tool's own)",
    )
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "runs" / "proofs")
    a = ap.parse_args()
    want = a.only.split(",") if a.only else None
    a.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    failed: list[str] = []
    n = 0
    for label, script, extra in TOOLS:
        if want and not any(label == w or label.split()[0] == w for w in want):
            continue
        n += 1
        slug = label.replace(" ", "-")
        cmd = [
            sys.executable,
            "-u",
            str(HERE / script),
            *extra,
            "--quick",
            "--out",
            str(a.out / slug),
        ]
        if a.jobs:
            cmd += ["--jobs", str(a.jobs)]
        t0 = time.monotonic()
        log = a.out / f"{slug}.log"
        with log.open("wb") as f:
            rc = subprocess.run(
                cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT
            ).returncode
        wall = time.monotonic() - t0
        lines = log.read_text(errors="replace").strip().splitlines()
        bad = [
            ln.strip()
            for ln in lines
            if ln.startswith("     FAIL") or ln.strip().startswith("FAIL ")
        ]
        state = "PASS" if rc == 0 else "FAIL"
        print(f"{state}  {label:<13} {wall:>5.0f} s   {log}", flush=True)
        for ln in bad[:6]:
            print(f"      {ln}", flush=True)
        if rc:
            failed.append(label)
    total = time.monotonic() - started
    print(
        f"{'PASS' if not failed else 'FAIL: ' + ', '.join(failed)}  patch:proofs: {n} tools, total {total:.0f} s",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
