#!/usr/bin/env python3
"""Exactness check for the HLE table (native/sharc/src/hle.rs).

Runs a coupled DN2 scenario with SHARC_HLE=1 and SHARC_HLE_CHECK=N,V: at the
first N calls of each HLE function the engine runs the hand-written routine
and the interpreter from the same entry state, compares the registers, flags,
stacks, memory, instruction count and return PC, and rolls both back; each
captured call is also checked V more times with synthetic inputs (zeros,
denormals, NaN, infinity, extremes, other trip counts). The run's PCM is
unchanged by the checker. Exit status 1 on any mismatch or when fewer than
--min cases ran both.

    uv run python tools/hle_check.py [-s trig1-taps] [-n 1500] [-v 6] [--min 10000]
        [--binary BIN] [--hle NAME[,NAME]] [--seconds S]

or `mise run hle-check`.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import benchlock
    import coupled
finally:
    sys.path.pop(0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-s", "--scenario", default="trig1-taps")
    ap.add_argument("-n", "--captured", type=int, default=1500)
    ap.add_argument("-v", "--variants", type=int, default=6)
    ap.add_argument("--min", type=int, default=10000)
    ap.add_argument("--binary")
    ap.add_argument("--hle", default="1")
    ap.add_argument("--device", default="dn2")
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()
    binary = args.binary or str(coupled.ensure_binary(args.no_build, args.device))
    env = coupled.run_env(
        {
            "SHARC_HLE": args.hle,
            "SHARC_HLE_CHECK": f"{args.captured},{args.variants}",
            "SHARC_HLE_LOG": "1",
        }
    )
    argv = coupled.run_args(
        binary, coupled.inputs(args.device), ["--scenario", args.scenario]
    )
    with benchlock.hold("shared", "hle_check"):
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    lines = [
        ln
        for ln in done.stderr.splitlines()
        if ln.startswith(("hle", "  hle", "  dn2", "why", "dump", "raw", "trace"))
    ]
    print("\n".join(lines) or done.stderr[-2000:])
    m = re.search(
        r"hle check: (\d+) cases .*?: (\d+) run by both, (\d+) declined, (\d+) MISMATCHES",
        done.stderr,
    )
    if not m:
        print("hle_check: no checker summary (is the HLE table built in?)")
        return 1
    cases, both, declined, bad = map(int, m.groups())
    print(f"cases {cases}, compared {both}, declined {declined}, mismatches {bad}")
    if bad:
        return 1
    if both < args.min:
        print(f"hle_check: only {both} compared cases, wanted {args.min}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
