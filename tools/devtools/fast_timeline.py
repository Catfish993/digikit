#!/usr/bin/env python3
"""Summarise a fast-tier build/compile timeline (SHARC_FAST_TL=1 on stderr).

``coupled-run`` with ``SHARC_FAST_TL=1`` prints one ``tl build`` line per region
build (build and Cranelift compile microseconds, kind, ops, helper count, IR
hash) and one ``tl tick`` line per quarter audio second (DSP thread CPU,
cumulative compile and build ms, calls). This reads such an stderr file:

    uv run python -m tools.devtools.fast_timeline ERRFILE [--ipas 1e9] [--tail 5]

Audio time is the DSP instruction counter over ``--ipas`` (1e9 insns per audio
second, idle skip included). Output: builds and compile time per audio-time
window, by kind, the per-build distribution, the worst stalls against the
0.667 ms period, and the steady state over the last ``--tail`` audio seconds
(DSP CPU minus compile and build time over audio time).
"""

from __future__ import annotations

import argparse
import collections
import re
import statistics

KV = re.compile(r"(\w+)=(\S+)")
PERIOD_US = 667


def parse(path: str):
    builds, ticks = [], []
    with open(path, errors="replace") as f:
        lines = f.readlines()
    for line in lines:
        if not line.startswith("tl "):
            continue
        d = {k: v for k, v in KV.findall(line)}
        kind = line.split()[1]
        if kind == "build":
            builds.append(
                {
                    "t": float(d["t_ms"]),
                    "ic": int(d["ic"]),
                    "pc": d["pc"],
                    "kind": d["kind"],
                    "ok": d["ok"] == "1",
                    "build": int(d["build_us"]),
                    "compile": int(d["compile_us"]),
                    "wall": int(d["wall_us"]),
                    "scalls": int(d.get("scalls", 0)),
                    "sinsns": int(d.get("sinsns", 0)),
                    "ops": int(d["ops"]),
                    "helpers": int(d["helpers"]),
                    "words": int(d["words"]),
                    "hash": d["hash"],
                }
            )
        elif kind == "tick":
            ticks.append(
                {
                    "t": float(d["t_ms"]),
                    "ic": int(d["ic"]),
                    "cpu": float(d["cpu_ms"]),
                    "compile": float(d["compile_ms"]),
                    "build": float(d["build_ms"]),
                    "builds": int(d["builds"]),
                    "calls": int(d["calls"]),
                }
            )
    return builds, ticks


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("err")
    ap.add_argument("--ipas", type=float, default=1e9)
    ap.add_argument("--tail", type=float, default=5.0)
    a = ap.parse_args()
    builds, ticks = parse(a.err)
    if not builds:
        raise SystemExit("no tl lines (run with SHARC_FAST_TL=1)")
    end_ic = ticks[-1]["ic"] if ticks else builds[-1]["ic"]
    secs = lambda ic: ic / a.ipas  # noqa: E731
    print(f"{len(builds)} builds, {len(ticks)} ticks, {secs(end_ic):.2f} audio s")
    edges = [0.0, 0.1, 0.25, 1.0, 2.0, 5.0, 1e9]
    print(
        "\nwindow (audio s)    builds  ok  compile_s  build_s  max_stall_ms  stalls>0.667ms"
    )
    for lo, hi in zip(edges, edges[1:], strict=False):
        w = [b for b in builds if lo <= secs(b["ic"]) < hi]
        if not w:
            continue
        label = f"{lo:g}-{hi:g}" if hi < 1e9 else f">={lo:g}"
        print(
            f"{label:<18}{len(w):8d}{sum(b['ok'] for b in w):5d}"
            f"{sum(b['compile'] for b in w) / 1e6:11.2f}{sum(b['build'] for b in w) / 1e6:9.2f}"
            f"{max(b['wall'] for b in w) / 1e3:14.2f}{sum(b['wall'] > PERIOD_US for b in w):13d}"
        )
    print("\nby kind (all)           builds  compile_s  build_s")
    byk = collections.defaultdict(list)
    for b in builds:
        byk[b["kind"]].append(b)
    for k, w in sorted(byk.items(), key=lambda kv: -len(kv[1])):
        print(
            f"{k:<22}{len(w):8d}{sum(b['compile'] for b in w) / 1e6:11.2f}"
            f"{sum(b['build'] for b in w) / 1e6:9.2f}"
        )
    late = [b for b in builds if secs(b["ic"]) >= 1.0]
    print(
        f"\nafter audio s 1: {len(late)} builds, by kind:",
        dict(collections.Counter(b["kind"] for b in late)),
    )
    ok = [b for b in builds if b["ok"] and b["compile"] > 0]
    c = [b["compile"] for b in ok]
    print(
        f"\ncompile per region (us), {len(c)} compiled: mean {statistics.mean(c):.0f} "
        f"p50 {pct(c, 0.5)} p90 {pct(c, 0.9)} p99 {pct(c, 0.99)} max {max(c)}"
    )
    w = [b["wall"] for b in builds]
    print(
        f"build+compile wall per call (us): p50 {pct(w, 0.5)} p99 {pct(w, 0.99)} max {max(w)}; "
        f"> period: {sum(x > PERIOD_US for x in w)} builds "
        f"({sum(x for x in w if x > PERIOD_US) / 1e6:.2f} s of {sum(w) / 1e6:.2f} s)"
    )
    big = sorted(ok, key=lambda b: -b["compile"])[:5]
    print(
        "worst compiles:",
        ", ".join(
            f"{b['pc']} {b['compile'] / 1e3:.1f} ms ({b['ops']} ops, {b['kind']}, "
            f"@{secs(b['ic']):.2f}s)"
            for b in big
        ),
    )
    rb = [
        b for b in builds if b["kind"].startswith("rebuild") or b["kind"] == "dynflat"
    ]
    if rb and any(b["scalls"] for b in rb):
        dead = [b for b in rb if b["sinsns"] == 0]
        print(
            f"rebuilds: {len(rb)}; the slot had run no instruction before the rebuild in "
            f"{len(dead)} ({sum(b['compile'] for b in dead) / 1e6:.2f} s of compile in the rebuilt kernels; "
            f"the discarded kernels cost about as much)"
        )
    print(
        f"kernels with helper calls: {sum(b['helpers'] > 0 for b in ok)} of {len(ok)}; "
        f"IR words mean {statistics.mean(b['words'] for b in ok):.0f} "
        f"total {sum(b['words'] for b in ok) * 4 / 1e6:.1f} MB; "
        f"distinct pointer-free IR hashes {len({b['hash'] for b in ok if b['helpers'] == 0})} "
        f"of {sum(b['helpers'] == 0 for b in ok)}"
    )
    if len(ticks) >= 2:
        last = ticks[-1]
        first = next(
            (t for t in ticks if secs(last["ic"]) - secs(t["ic"]) <= a.tail), ticks[0]
        )
        dic = secs(last["ic"]) - secs(first["ic"])
        if dic > 0:
            cpu = last["cpu"] - first["cpu"]
            cb = (last["compile"] - first["compile"]) + (last["build"] - first["build"])
            print(
                f"\nlast {dic:.2f} audio s: DSP CPU {cpu / 1e3:.2f} s = {cpu / 1e3 / dic:.3f} per audio s; "
                f"compile+build in it {cb / 1e3:.2f} s; run time {(cpu - cb) / 1e3 / dic:.3f} per audio s; "
                f"builds {last['builds'] - first['builds']}"
            )
        print(
            f"whole run: DSP CPU {last['cpu'] / 1e3:.1f} s = compile {last['compile'] / 1e3:.1f} + "
            f"build {last['build'] / 1e3:.1f} + run {(last['cpu'] - last['compile'] - last['build']) / 1e3:.1f}"
        )


if __name__ == "__main__":
    main()
