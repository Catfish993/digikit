#!/usr/bin/env python3
"""Find 2-aligned big-endian words in a ColdFire MAIN OS section that point into address ranges, and clamp compares.

    uv run python -m tools.coldfire.cf_wordscan IMAGE --range LO HI [--range LO HI ...] [--bound N ...] [--base 0x40000400]

IMAGE is a section_3_MAIN_OS.bin (out/sections/<image>/). The word scan is the one tools/patchkit/port_resolve.py scan_param_sites uses
(a hit is any aligned long inside [LO, HI), instruction operand or data); `--bound N` lists `cmpi.l #N,Dn` / `cmpa.l #N,An`
followed by a clamp-shaped branch or Scc (bcs/bcc/bhi/bls/scs/scc), the shape of the bounds checks on a table of N rows.
A hit is a candidate, not proof: confirm with tools/coldfire/fw.py. The containing function is printed when the cfdb has one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--base", default="0x40000400")
    ap.add_argument(
        "--range", nargs=2, action="append", default=[], metavar=("LO", "HI")
    )
    ap.add_argument("--bound", action="append", default=[], type=lambda x: int(x, 0))
    ap.add_argument(
        "--db",
        help="cfdb sqlite for function names (e.g. out/cfdb/dn2-1.12-emac.sqlite)",
    )
    a = ap.parse_args()
    d = Path(a.image).read_bytes()
    base = int(a.base, 0)
    funcs: list[tuple[int, int, str]] = []
    if a.db:
        con = sqlite3.connect(a.db)
        cols = [r[1] for r in con.execute("pragma table_info(functions)")]
        sel = [c for c in ("addr", "start", "entry") if c in cols][0]
        for r in con.execute(f"select {sel}, size, name from functions"):
            s = int(r[0], 0) if isinstance(r[0], str) else r[0]
            funcs.append((s, s + (r[1] or 0), r[2]))
        funcs.sort()

    def fn(addr: int) -> str:
        lo, hi = 0, len(funcs)
        while lo < hi:
            m = (lo + hi) // 2
            if funcs[m][0] <= addr:
                lo = m + 1
            else:
                hi = m
        if lo and funcs[lo - 1][0] <= addr < funcs[lo - 1][1]:
            return f"{funcs[lo - 1][2]}@{funcs[lo - 1][0]:#x}"
        return ""

    ranges = [(int(lo, 0), int(hi, 0)) for lo, hi in a.range]
    for o in range(0, len(d) - 3, 2):
        v = int.from_bytes(d[o : o + 4], "big")
        for lo, hi in ranges:
            if lo <= v < hi:
                print(f"ref  {base + o:#x} -> {v:#x} (+{v - lo:#x}) {fn(base + o)}")
        if a.bound and o + 7 <= len(d):
            op = int.from_bytes(d[o : o + 2], "big")
            imm = int.from_bytes(d[o + 2 : o + 6], "big")
            if (
                (op & 0xFFF8 == 0x0C80 or op & 0xF1FF == 0xB1FC)
                and imm in a.bound
                and d[o + 6]
                in (
                    0x54,
                    0x55,
                    0x62,
                    0x63,
                    0x64,
                    0x65,
                    0x66,
                    0x67,
                    0x6C,
                    0x6D,
                    0x6E,
                    0x6F,
                )
            ):
                print(
                    f"bound {base + o:#x} #{imm:#x} next={d[o + 6]:#x}{d[o + 7]:02x} {fn(base + o)}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
