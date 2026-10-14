#!/usr/bin/env python3
"""Region-call sources of a tools/devtools/perf.py diagnostic run: per DSP function, the
region calls per audio second, instructions per call and why each call ended.

    uv run python -m tools.devtools.perf_calls out/runs/perf/<stamp>/logs/<config>.diag.err \
        [--seconds 10.5] [--top 12] [--image dn2-1.11]

Exit kinds (SHARC_FAST_COV "cov run ... exits="): `call`/`ret` a call or a
return the kernel ran, `edge` a taken branch (or loop-back) to an address the
kernel does not hold -- split here into `edge>fn` (the target is a function
entry) and `edge>pc` -- `stop` an instruction the lowering cannot run, `guard`
a failed guard at an instruction of the region, `budget`. Declines are calls
that did not run the kernel (req, shape, window, budget, exit0).
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
from tools.devtools import fastcov  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("diag")
    ap.add_argument("--seconds", type=float, default=10.5)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--image", default="dn2-1.11")
    a = ap.parse_args()
    text = pathlib.Path(a.diag).read_text(errors="replace")
    diag = fastcov.Diag(text)
    funcs = fastcov.Functions(a.image)
    entries = set()
    if funcs.db is not None:
        entries = {
            r[0]
            for r in funcs.db.execute(
                "SELECT entry_sw FROM functions WHERE image=?", (a.image,)
            )
        }
    names = ("req", "shape", "window", "budget", "exit0")
    dec = collections.defaultdict(lambda: [0] * 5)
    for line in text.splitlines():
        m = re.match(r"cov run root=(0x[0-9a-f]+) .*declined=\[([\d, ]+)\]", line)
        if m:
            e = funcs.of(int(m[1], 16))[0]
            for i, x in enumerate(int(v) for v in m[2].split(",")):
                dec[e][i] += x
    kinds: dict[int, dict[str, list[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0])
    )
    nroots: collections.Counter = collections.Counter()
    for r in diag.runs:
        e = funcs.of(r["root"])[0]
        nroots[e] += 1
        for at, kind, cnt, ins, _gap in r["exits"]:
            if kind == "edge":
                kind = "edge>fn" if at in entries else "edge>pc"
            kinds[e][kind][0] += cnt
            kinds[e][kind][1] += ins
    s = a.seconds

    def row(label: str, ks: dict[str, list[int]], d: list[int]) -> str:
        n = sum(v[0] for v in ks.values())
        ins = sum(v[1] for v in ks.values())
        parts = ", ".join(
            f"{k} {v[0] / s / 1e6:.2f}M ({v[1] / max(v[0], 1):.1f}i)"
            for k, v in sorted(ks.items(), key=lambda kv: -kv[1][0])
            if v[0] / s > 5e4
        )
        dparts = ", ".join(
            f"{names[i]} {x / s / 1e6:.2f}M" for i, x in enumerate(d) if x / s > 5e4
        )
        return f"| {label} | {n / s / 1e6:.2f}M | {ins / max(n, 1):.1f} | {parts} | {dparts} |"

    allk: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    alld = [0] * 5
    for e, ks in kinds.items():
        for k, v in ks.items():
            allk[k][0] += v[0]
            allk[k][1] += v[1]
        for i in range(5):
            alld[i] += dec[e][i]
    print(
        "| function | ok calls/s | insns/call | exits per audio s (insns/call) | declines/s |"
    )
    print("|---|---:|---:|---|---|")
    print(row("**all**", allk, alld))
    order = sorted(kinds, key=lambda e: -sum(v[0] for v in kinds[e].values()))
    for e in order[: a.top]:
        print(row(f"{e:#x} {funcs.of(e)[1]} ({nroots[e]} roots)", kinds[e], dec[e]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
