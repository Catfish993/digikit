#!/usr/bin/env python3
"""Compare two runs of the DT2 FX scenarios (tests/scenarios/test_dt2_fx.py --keep-runs).

    uv run python tools/dt2fx_compare.py RUN_A RUN_B

RUN_A and RUN_B are run directories under out/runs/tests/ (each holds dt2-trackfx-N
and dt2-sendfx-N batches). Prints, per scenario, whether the decoded read-back
arrays (live Sound and Fx parameters) and the PCM hash agree, and a summary. Two
firmware versions on one ready-state recipe agree on the read-back when the
encoder-turn behaviour is the same, whatever the oracle intents say.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import fxcheck
finally:
    sys.path.pop(0)


def load(run: pathlib.Path) -> dict[str, tuple[dict[str, int], str, int]]:
    names = fxcheck.name_maps(fxcheck.DEFAULT_IMAGE)
    out: dict[str, tuple[dict[str, int], str, int]] = {}
    for rep in sorted(run.glob("*/report.json")) + sorted(
        run.glob("*/shard-*/report.json")
    ):
        data: dict[str, Any] = json.loads(rep.read_text())
        for s in data.get("scenarios", []):
            vals = fxcheck.decode_arrays(s.get("peek") or {}, names)
            out[s["name"]] = (vals, s["pcm"]["sha256"], s["guest_instructions"])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("a", type=pathlib.Path)
    p.add_argument("b", type=pathlib.Path)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    fxcheck.configure("dt2")
    ra, rb = load(a.a), load(a.b)
    common = sorted(set(ra) & set(rb))
    same_vals = same_pcm = 0
    for n in common:
        (va, pa, _), (vb, pb, _) = ra[n], rb[n]
        sv, sp = va == vb, pa == pb
        same_vals += sv
        same_pcm += sp
        if not sv or a.verbose:
            diff = {
                k: (va.get(k), vb.get(k))
                for k in set(va) | set(vb)
                if va.get(k) != vb.get(k)
            }
            print(
                f"{n:28s} vals={'same' if sv else 'DIFF'} pcm={'same' if sp else 'DIFF'} {diff if not sv else ''}"
            )
    print(
        f"{len(common)} common scenarios ({len(ra)} in A, {len(rb)} in B): read-back equal "
        f"{same_vals}, PCM equal {same_pcm}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
