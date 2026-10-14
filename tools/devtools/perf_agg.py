#!/usr/bin/env python3
"""Aggregate tools/devtools/perf.py runs over scenarios: which interpreted functions to cover.

Reads the diagnostic stderr of each scenario (logs/<scenario>/*.diag.err, or
logs/*.diag.err for a single-scenario run) from one or more perf output
directories, and prints per-scenario totals and the union of interpreted
functions ranked by their mean interpreted cost per audio second. A function is
FX-only when an FX scenario interprets it at more than twice the rate of every
baseline scenario (default: trig1-taps, tf-dry-hold, sf-dry-hold) plus 5k insns/s.

    uv run python -m tools.devtools.perf_agg DIR [DIR ...] [--baseline NAME ...] [--top 10]
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
from tools.devtools import fastcov, perf

BASELINES = ["trig1-taps", "tf-dry-hold", "sf-dry-hold"]


def load(dirs: list[pathlib.Path], image: str):
    """{scenario: (audio seconds, Diag, config label)} from perf output dirs."""
    out = {}
    for d in dirs:
        for run in json.loads((d / "perf.json").read_text()):
            logs = d / "logs"
            err = next(
                p
                for p in [
                    logs
                    / run["scenario"]
                    / f"{pathlib.Path(run['run']['report']).stem.replace('.timed', '')}.diag.err",
                    logs
                    / f"{pathlib.Path(run['run']['report']).stem.replace('.timed', '')}.diag.err",
                ]
                if p.is_file()
            )
            diag = fastcov.Diag(err.read_text(errors="replace"))
            out[run["scenario"]] = (run["run"], diag)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--baseline", nargs="+", default=BASELINES)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--image", default="dn2-1.11")
    args = ap.parse_args()
    costs = perf.load_costs()
    funcs = fastcov.Functions(args.image)
    runs = load([pathlib.Path(d) for d in args.dirs], args.image)
    per: dict[str, dict[int, dict]] = {}
    print(
        "| scenario | audio s | interp % | calls/s | est CPU s/audio s | top interpreted function (ms/audio s) |"
    )
    print("|---|---:|---:|---:|---:|---|")
    for name, (t, diag) in runs.items():
        a = t["audio"]
        rows = perf.rank(diag, funcs, costs, a)
        per[name] = {r["entry"]: r for r in rows}
        est = (
            (
                t["interp"] * costs["interp"]
                + t["call"] * costs["call"]
                + t["kernel"] * costs["kernel"]
            )
            / 1e9
            / a
        )
        top = max(rows, key=lambda r: r["interp"])
        print(
            f"| {name} | {a:.2f} | {100 * t['interp'] / t['executed']:.2f} | {t['call'] / a / 1e6:.2f}M | {est:.2f} "
            f"| {top['entry']:#x} ({top['interp'] * costs['interp'] / 1e6 / a:.0f}) |"
        )
    audio = {n: runs[n][0]["audio"] for n in runs}
    agg: dict[int, dict] = collections.defaultdict(
        lambda: {
            "interp_ms": 0.0,
            "calls_ms": 0.0,
            "n": 0,
            "base": 0.0,
            "fx": 0.0,
            "fxn": "",
        }
    )
    base_rate = collections.defaultdict(float)
    for b in args.baseline:
        for e, r in per.get(b, {}).items():
            base_rate[e] = max(base_rate[e], r["interp"] / audio[b])
    for name, rows in per.items():
        for e, r in rows.items():
            g = agg[e]
            g["name"] = r["name"]
            g["interp_ms"] += r["interp"] * costs["interp"] / 1e6 / audio[name]
            g["calls_ms"] += r["calls"] * costs["call"] / 1e6 / audio[name]
            g["n"] += 1
            rate = r["interp"] / audio[name]
            if name not in args.baseline and rate - base_rate[e] > g["fx"]:
                g["fx"], g["fxn"] = rate - base_rate[e], name
    n = len(per)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["interp_ms"])
    print(
        f"\nunion of {len(ranked)} functions over {n} scenarios, ranked by mean interpreted ms per audio s\n"
    )
    print(
        "| rank | function | size | mean interp ms/audio s | mean call ms/audio s | max interp insns/s in baselines | FX extra insns/s (scenario) | refusal |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---|")
    for i, (e, g) in enumerate(ranked[: args.top], 1):
        row = (
            funcs.db.execute(
                "SELECT end_sw FROM functions WHERE image=? AND entry_sw=?",
                (args.image, e),
            ).fetchone()
            if funcs.db
            else None
        )
        size = (row[0] - e) if row else 0
        # the reason at the hottest interpreted pc of this function in the busiest scenario
        best = max(
            ((runs[s][1], s) for s in runs if e in per[s]),
            key=lambda ds: per[ds[1]][e]["interp"] / audio[ds[1]],
        )[0]
        mine = [(c, pc) for pc, c in best.hist.items() if funcs.of(pc)[0] == e]
        pc = max(mine)[1] if mine else e
        why = best.reason_of(pc, funcs)
        flag = (
            f"{g['fx'] / 1e3:.0f}k ({g['fxn']})"
            if g["fx"] > 5e3 and g["fx"] > base_rate[e]
            else "-"
        )
        print(
            f"| {i} | {e:#x} {g['name']} | {size} | {g['interp_ms'] / n:.0f} | {g['calls_ms'] / n:.0f} "
            f"| {base_rate[e] / 1e3:.0f}k | {flag} | pc {pc:#x}: {why} |"
        )
    fx_table(per, runs, audio, args, funcs, costs)
    return 0


def fx_table(per, runs, audio, args, funcs, costs) -> None:
    """What the FX add: interpreted insns/s over the dry hold of the same file."""
    dry = {"tf": "tf-dry-hold", "sf": "sf-dry-hold"}
    print(
        "\nFX scenario against its dry hold (interpreted insns per audio s, est. interp ms)\n"
    )
    print(
        "| scenario | extra interp insns/s | extra est. interp ms/audio s | top extra functions (insns/s) |"
    )
    print("|---|---:|---:|---|")
    best: dict[int, tuple[float, str]] = {}
    for name, rows in per.items():
        base = per.get(dry.get(name[:2], ""))
        if base is None or name in dry.values():
            continue
        a, b = audio[name], audio[dry[name[:2]]]
        delta = {
            e: r["interp"] / a - (base[e]["interp"] / b if e in base else 0)
            for e, r in rows.items()
        }
        tot = sum(delta.values())
        top = sorted(delta.items(), key=lambda kv: -kv[1])[:3]
        print(
            f"| {name} | {tot / 1e3:.0f}k | {tot * costs['interp'] / 1e6:.0f} | "
            + ", ".join(f"{e:#x} ({d / 1e3:.0f}k)" for e, d in top)
            + " |"
        )
        for e, d in delta.items():
            if d > best.get(e, (0.0, ""))[0]:
                best[e] = (d, name)
    print("\nFunctions by largest FX increase\n")
    print(
        "| function | size | extra interp insns/s | scenario | baseline interp insns/s |"
    )
    print("|---|---:|---:|---|---:|")
    for e, (d, n) in sorted(best.items(), key=lambda kv: -kv[1][0])[:10]:
        row = funcs.db.execute(
            "SELECT end_sw FROM functions WHERE image=? AND entry_sw=?", (args.image, e)
        ).fetchone()
        print(
            f"| {e:#x} | {row[0] - e if row else 0} | {d / 1e3:.0f}k | {n} | {per[dry[n[:2]]].get(e, {'interp': 0})['interp'] / audio[dry[n[:2]]] / 1e3:.0f}k |"
        )


if __name__ == "__main__":
    raise SystemExit(main())
