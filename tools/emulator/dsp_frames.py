"""Compare per-frame DSP CSV logs from two runs.

Reads the model log (`DIGI_DSP_MODEL_LOG`) or the watch log
(`DIGI_DSP_WATCH_OUT`): any CSV with a `frame` column works.

  diff A.csv B.csv --align PA PB [--before N] [--after M] [--cols REGEX]
      align frame PA of A with frame PB of B and print, per offset, the
      columns whose values differ.
  first A.csv B.csv --align PA PB [--before N] [--after M] [--cols REGEX]
      the first aligned offset where any non-frame column differs.
  summary CSV
      per column: total of numeric cells and count of nonzero cells.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys

Row = dict[str, str]


def read_rows(path: str) -> dict[int, Row]:
    """Rows keyed by their integer `frame` column."""
    with open(path, newline="") as f:
        return {int(r["frame"]): r for r in csv.DictReader(f)}


def align(
    rows_a: dict[int, Row],
    rows_b: dict[int, Row],
    pa: int,
    pb: int,
    before: int = 0,
    after: int = 0,
) -> list[tuple[int, Row, Row]]:
    """(offset, row_a, row_b) for offsets -before..after where both exist."""
    out = []
    for off in range(-before, after + 1):
        a = rows_a.get(pa + off)
        b = rows_b.get(pb + off)
        if a is not None and b is not None:
            out.append((off, a, b))
    return out


def differing(
    row_a: Row, row_b: Row, cols: re.Pattern[str] | None = None
) -> list[tuple[str, str, str]]:
    """(column, value_a, value_b) for non-frame columns that differ."""
    out = []
    for col in row_a:
        if col == "frame" or col not in row_b:
            continue
        if cols is not None and not cols.search(col):
            continue
        if row_a[col] != row_b[col]:
            out.append((col, row_a[col], row_b[col]))
    return out


def first_difference(
    aligned: list[tuple[int, Row, Row]], cols: re.Pattern[str] | None = None
) -> tuple[int, list[tuple[str, str, str]]] | None:
    for off, a, b in aligned:
        d = differing(a, b, cols)
        if d:
            return off, d
    return None


def summarize(rows: dict[int, Row]) -> dict[str, tuple[float, int]]:
    """Per column (total of numeric cells, count of nonzero cells)."""
    out: dict[str, tuple[float, int]] = {}
    for row in rows.values():
        for col, val in row.items():
            if col == "frame":
                continue
            total, nz = out.get(col, (0.0, 0))
            try:
                x = float(val)
            except ValueError:
                x = 0.0
                nonzero = val not in ("", "?")
            else:
                nonzero = x != 0
            out[col] = (total + x, nz + (1 if nonzero else 0))
    return out


def fmt(d: list[tuple[str, str, str]]) -> str:
    return " ".join(f"{c}={a}|{b}" for c, a, b in d)


def _aligned(args: argparse.Namespace) -> list[tuple[int, Row, Row]]:
    pa, pb = args.align
    return align(read_rows(args.a), read_rows(args.b), pa, pb, args.before, args.after)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("diff", "first"):
        p = sub.add_parser(name)
        p.add_argument("a")
        p.add_argument("b")
        p.add_argument(
            "--align", nargs=2, type=int, required=True, metavar=("PA", "PB")
        )
        p.add_argument("--before", type=int, default=0)
        p.add_argument("--after", type=int, default=0)
        p.add_argument("--cols", default=None)
    p = sub.add_parser("summary")
    p.add_argument("csv")
    args = ap.parse_args(argv)

    if args.cmd == "summary":
        for col, (total, nz) in summarize(read_rows(args.csv)).items():
            print(f"{col}\ttotal={total:g}\tnonzero={nz}")
        return 0

    cols = re.compile(args.cols) if args.cols else None
    aligned = _aligned(args)
    if args.cmd == "diff":
        for off, a, b in aligned:
            d = differing(a, b, cols)
            if d:
                print(f"{off:+d}: {fmt(d)}")
        return 0
    found = first_difference(aligned, cols)
    if found is None:
        print("no difference")
        return 1
    print(f"{found[0]:+d}: {fmt(found[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
