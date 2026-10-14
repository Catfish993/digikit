"""CPU Bottlenecks split of a command, from xctrace's CPU Counters template.

Records `xctrace record --template "CPU Counters"` around a command (or reads
an existing .trace) and prints the process's average split of cycles into the
four classes Instruments reports for Apple silicon (1 ms buckets, values in
1/10000 of the cycles). The export carries no labels; the order was calibrated
with synthetic programs (docs/design/aot-mask-free.md, "Slices 0-1 results"): a
dependent multiply chain and a 64 MB pointer chase put nearly everything in
the second value (back end), 6000 small functions called in random order
(2.6 MB of code) raise the third (front end: instruction delivery) from 0 to
29%. So the values are [useful, backend, frontend, discarded/other]. Treat the
first and last as probable, the middle two as calibrated.

    xctrace_bottleneck.py record [--time-limit 10s] [--env K=V ...] OUT.trace -- CMD [ARGS]
    xctrace_bottleneck.py read IN.trace [--skip-ms 300]

No root needed. The export also holds per-core idle rows: only buckets of the
target process are used.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

NAMES = ["useful?", "backend", "frontend", "discarded?"]


def export(trace: str, schema: str) -> ET.Element:
    out = subprocess.run(
        [
            "xctrace",
            "export",
            "--input",
            trace,
            "--xpath",
            '/trace-toc/run[1]/data/table[@schema="%s"]' % schema,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return ET.fromstring(out)


def rows_by_thread(root: ET.Element) -> list[tuple[int, int, list[int], str]]:
    ids: dict[str, ET.Element] = {}
    for el in root.iter():
        if "id" in el.attrib:
            ids[el.attrib["id"]] = el

    def get(row: ET.Element, tag: str) -> ET.Element:
        el = row.find(tag)
        assert el is not None
        if "ref" in el.attrib:
            return ids[el.attrib["ref"]]
        return el

    out = []
    for row in root.iter("row"):
        start = int(get(row, "start-time").text or 0)
        dur = int(get(row, "duration").text or 0)
        vals = [int(x) for x in (get(row, "uint64-array").text or "").split()]
        out.append((start, dur, vals, get(row, "thread").attrib.get("fmt", "?")))
    return out


def report(trace: str, skip_ms: float, min_share: float = 0.05) -> None:
    rows = rows_by_thread(export(trace, "CounterMetricByThread"))
    if not rows:
        sys.exit("no counter rows")
    t0 = rows[0][0] + int(skip_ms * 1e6)
    threads: dict[str, list[float]] = {}
    for start, dur, vals, thread in rows:
        if start < t0 or len(vals) < 4:
            continue
        acc = threads.setdefault(thread, [0.0] * 5)
        for i in range(4):
            acc[i] += vals[i] * dur
        acc[4] += dur
    total = sum(a[4] for a in threads.values())
    for thread, acc in sorted(threads.items(), key=lambda kv: -kv[1][4]):
        if acc[4] < min_share * total:
            continue
        print("%s: %.2f s busy" % (thread, acc[4] / 1e9))
        for name, v in zip(NAMES, acc, strict=False):
            print("  %-10s %5.1f%%" % (name, 100 * v / acc[4] / 10000))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("out")
    r.add_argument("--time-limit", default="10s")
    r.add_argument("--skip-ms", type=float, default=300)
    r.add_argument("--env", action="append", default=[], metavar="K=V")
    r.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("read")
    p.add_argument("trace")
    p.add_argument("--skip-ms", type=float, default=300)
    args = ap.parse_args()
    if args.cmd == "record":
        cmd = [c for c in args.rest if c != "--"]
        # xctrace exits non-zero when the time limit kills the target: judge
        # by the trace file instead.
        subprocess.run(
            ["xctrace", "record", "--template", "CPU Counters", "--output", args.out]
            + [x for kv in args.env for x in ("--env", kv)]
            + ["--time-limit", args.time_limit, "--launch", "--"]
            + cmd,
            check=False,
            stdout=subprocess.DEVNULL,
        )
        if not os.path.exists(args.out):
            sys.exit("xctrace left no trace")
        report(args.out, args.skip_ms)
    else:
        report(args.trace, args.skip_ms)


if __name__ == "__main__":
    main()
