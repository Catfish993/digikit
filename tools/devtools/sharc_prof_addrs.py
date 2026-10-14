#!/usr/bin/env python3
"""Leaf addresses of the sharc-dsp thread from an xctrace CPU Profiler export,
symbolicated with atos (inline frames) and grouped by source function/file.

    xcrun xctrace export --input X.trace --xpath '/trace-toc/run[@number="1"]/data/table[@schema="cpu-profile"]' > X.xml
    uv run python -m tools.devtools.sharc_prof_addrs X.xml BINARY_WITH_DSYM [--top 40]
"""

import argparse
import collections
import re
import subprocess
import xml.etree.ElementTree as ET


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml")
    ap.add_argument("binary")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--thread", default="sharc-dsp")
    a = ap.parse_args()
    frames: dict[str, tuple[str, int]] = {}
    threads: dict[str, str] = {}
    load = None
    leafs: collections.Counter = collections.Counter()
    total = 0
    for _, el in ET.iterparse(a.xml):
        if el.tag == "frame" and el.get("id"):
            frames[el.get("id")] = (el.get("name"), int(el.get("addr"), 16))
        elif el.tag == "thread" and el.get("id"):
            threads[el.get("id")] = el.get("fmt") or ""
        elif (
            el.tag == "binary"
            and el.get("load-addr")
            and el.get("name") == "coupled-run"
        ):
            load = int(el.get("load-addr"), 16)
        elif el.tag == "row":
            th = el.find("thread")
            tfmt = (
                threads.get(th.get("id") or th.get("ref"), "") if th is not None else ""
            )
            if a.thread not in tfmt:
                el.clear()
                continue
            bt = el.find(".//backtrace")
            fr = bt.find("frame") if bt is not None else None
            if fr is not None:
                fid = fr.get("id") or fr.get("ref")
                name, addr = frames[fid]
                leafs[addr] += 1
                total += 1
            el.clear()
    addrs = list(leafs)
    out = subprocess.run(
        ["atos", "-o", a.binary, "-l", hex(load), "-i", *[hex(x) for x in addrs]],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()  # fmt: skip
    # atos prints one line per address (the innermost inline frame last with -i)
    by = collections.Counter()
    by_file = collections.Counter()
    by_line: collections.Counter = collections.Counter()
    by_line_all: collections.Counter = collections.Counter()
    idx = 0
    lines = out
    # With -i each address gives several lines then a blank separator.
    groups, cur = [], []
    for ln in lines:
        if ln.strip() == "":
            groups.append(cur)
            cur = []
        else:
            cur.append(ln)
    if cur:
        groups.append(cur)
    if len(groups) != len(addrs):
        # No separators: one line per address.
        groups = [[ln] for ln in lines]
    for addr, g in zip(addrs, groups, strict=False):
        n = leafs[addr]
        inner = g[0]  # the innermost inline frame first
        m = re.search(r"\(in [^)]*\) \((.*?):(\d+)\)", inner)
        f = m.group(1) if m else "?"
        fn = re.sub(r" \(in .*", "", inner)
        fn = re.sub(r"^_RNv[A-Za-z0-9_]*?\d+(\w+)$", r"\1", fn)
        by[(fn, f)] += n
        by_file[f] += n
        if m:
            by_line[(f, int(m.group(2)))] += n
            by_line_all[(m.group(1), int(m.group(2)))] += n
    import json

    json.dump(
        [[f, ln, n] for (f, ln), n in by_line.most_common()],
        open(a.xml + ".lines.json", "w"),
    )
    print("samples", total)
    print("--- by source file")
    for f, n in by_file.most_common(a.top):
        print(f"{100 * n / total:5.1f}% {f}")
    print("--- by function (innermost frame)")
    for (fn, f), n in by.most_common(a.top):
        print(f"{100 * n / total:5.1f}% {fn[:70]}  {f}")


if __name__ == "__main__":
    main()
