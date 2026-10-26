#!/usr/bin/env python3
"""Compare two runs of the DT2 FX scenarios (DIGI_FX_KEEP_RUNS=1 pnpm --filter @digi/regression test:fx:dt2).

    uv run python -m tools.emulator.dt2fx_compare RUN_A RUN_B

RUN_A and RUN_B are run directories under out/regression/dt2-fx/; older
out/runs/tests/ directories with per-family batches also work. Prints whether the read-back
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

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.emulator import fxcheck  # noqa: E402


def load(run: pathlib.Path) -> dict[str, tuple[dict[str, int], str, int]]:
    names = fxcheck.name_maps(fxcheck.DEFAULT_IMAGE)
    out: dict[str, tuple[dict[str, int], str, int]] = {}
    reports = [run / "report.json"] if (run / "report.json").exists() else []
    reports += sorted(run.glob("*/report.json"))
    reports += sorted(run.glob("*/shard-*/report.json"))
    for rep in reports:
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
