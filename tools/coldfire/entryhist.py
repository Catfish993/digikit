"""Function-entry histogram of a Ghidra dump, for an image's code ranges.

    uv run python -m tools.coldfire.entryhist out/ghidra/dt2-1.16-emac/functions.jsonl
        [--base 0x40000400] [--bucket 0x10000] [--min 1]

Counts function entries per bucket of --bucket bytes, aligned to --base, and
joins adjacent buckets with at least --min entries into ranges. A function body
that reaches into the next range joins the two: 1.15C's FUN_4014ea20 is 98,196
bytes long and leaves bucket 0x40150400 without an entry. Each range is
printed twice: aligned to buckets, and from its first entry to the end of its
last function body. tools/coldfire/codeseeds.py and tools/coldfire/rttiscan.py take a range as
--code LO HI.
"""

import argparse
import json


def load(path):
    """-> sorted [(entry, end)] from a ghidradump functions.jsonl; end is exclusive."""
    funcs = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            entry = int(rec["entry"], 16)
            ends = [int(hi, 16) + 1 for _, hi in rec["ranges"]]
            funcs.append((entry, max(ends) if ends else entry))
    return sorted(funcs)


def histogram(funcs, base, bucket):
    """-> {bucket index: entry count}; entries below base are not counted."""
    counts = {}
    for entry, _ in funcs:
        if entry >= base:
            i = (entry - base) // bucket
            counts[i] = counts.get(i, 0) + 1
    return counts


def ranges(funcs, base, bucket, minimum=1):
    """-> [{'aligned': (lo, hi), 'exact': (lo, hi), 'functions': n}], one per run
    of adjacent buckets that hold at least `minimum` entries each, where a run
    whose last function body reaches the next run's first bucket joins it."""
    counts = histogram(funcs, base, bucket)
    runs = []
    for i in sorted(i for i, n in counts.items() if n >= minimum):
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    out = []
    for first, last in runs:
        lo, hi = base + first * bucket, base + (last + 1) * bucket
        inside = [(e, end) for e, end in funcs if lo <= e < hi]
        exact = (inside[0][0], max(end for _, end in inside))
        if out and out[-1]["exact"][1] > lo:
            prev = out[-1]
            prev["aligned"] = (prev["aligned"][0], hi)
            prev["exact"] = (prev["exact"][0], max(prev["exact"][1], exact[1]))
            prev["functions"] += len(inside)
        else:
            out.append({"aligned": (lo, hi), "exact": exact, "functions": len(inside)})
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Function-entry histogram and code ranges.")
    p.add_argument(
        "functions", help="functions.jsonl from tools/coldfire/ghidradump.py"
    )
    p.add_argument("--base", type=lambda s: int(s, 0), default=0x40000400)
    p.add_argument("--bucket", type=lambda s: int(s, 0), default=0x10000)
    p.add_argument(
        "--min",
        type=int,
        default=1,
        dest="minimum",
        help="entries a bucket needs to count as code",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    funcs = load(args.functions)
    counts = histogram(funcs, args.base, args.bucket)
    print("%d functions" % len(funcs))
    below = sum(1 for entry, _ in funcs if entry < args.base)
    if below:
        print("%d entries below the base" % below)
    for i in range(max(counts) + 1 if counts else 0):
        print("0x%08x %6d" % (args.base + i * args.bucket, counts.get(i, 0)))
    print("ranges: aligned, exact, functions")
    for r in ranges(funcs, args.base, args.bucket, args.minimum):
        print(
            "  --code 0x%08x 0x%08x   0x%08x-0x%08x %6d"
            % (r["aligned"] + r["exact"] + (r["functions"],))
        )


if __name__ == "__main__":
    main()
