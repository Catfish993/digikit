"""Check Version Tracking matches against the image bytes and the Ghidra dumps.

    uv run python -m tools.coldfire.vtcheck out/vt/dt2-1.15C_to_dt2-1.16.json \\
        --source-image sections/section_3_MAIN_OS.bin --source-dump out/ghidra/dt2-1.15C-emac \\
        --dest-image out/sections/dt2-1.16/section_3_MAIN_OS.bin --dest-dump out/ghidra/dt2-1.16-emac \\
        [--base 0x40000400] [--sample 20] [--seed 1] [--lookup ADDR ...] [--json OUT]

The matches come from tools/coldfire/ghidravt.py. Correlators overlap, so each accepted
function association (source entry, destination entry) is checked once, with
the correlators that found it. The dumps must be made before Version Tracking,
so that their names are independent of it. Per association:

- bytes: 'equal' if the function bodies are byte-identical in the two images,
  'same-size' (with the number of differing bytes) if not but their sizes
  agree, 'resized' otherwise, 'no-function' if a dump lacks the entry;
- names: 'agree' or 'differ' when both dumps give a non-default name (from
  RTTI or code seeds), else 'n/a'. `switchD_<addr>::caseD_<n>` labels count
  as default: they carry the switch's address, so they differ across versions;
- callees: the share of the source function's matched callees whose match is
  among the destination function's callees, or none if it has no matched
  callee.

It prints totals per correlator set, a seeded sample with that evidence, the
functions matched to more than one function on the other side, and for each
--lookup source address the function that holds it and the same offset in its
match.
"""

import argparse
import bisect
import json
import os
import random

DEFAULT_PREFIXES = ("FUN_", "thunk_FUN_", "switchD_")


def load_dump(path):
    """-> {entry: {'name', 'ranges': [(lo, hi)], 'callees': {entry}}} from functions.jsonl;
    hi is inclusive."""
    funcs = {}
    with open(os.path.join(path, "functions.jsonl")) as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            funcs[int(rec["entry"], 16)] = {
                "name": rec["name"],
                "ranges": [(int(lo, 16), int(hi, 16)) for lo, hi in rec["ranges"]],
                "callees": {int(c["entry"], 16) for c in rec["callees"]},
            }
    return funcs


def associations(report):
    """-> {(source, dest): [correlator, ...]} over accepted function matches."""
    assoc = {}
    for m in report["matches"]:
        if m["type"] == "Function" and m["status"] == "ACCEPTED":
            key = (int(m["source"], 16), int(m["dest"], 16))
            assoc.setdefault(key, set()).add(m["correlator"])
    return {key: sorted(c) for key, c in assoc.items()}


def forward_map(assoc):
    """-> {source: {dest}} and {dest: {source}}."""
    fwd, back = {}, {}
    for src, dst in assoc:
        fwd.setdefault(src, set()).add(dst)
        back.setdefault(dst, set()).add(src)
    return fwd, back


def body(image, base, func):
    return b"".join(image[lo - base : hi + 1 - base] for lo, hi in func["ranges"])


def is_default(name):
    return name.startswith(DEFAULT_PREFIXES)


def check(key, src_funcs, dst_funcs, src_image, dst_image, base, fwd):
    """-> the evidence for one association."""
    src, dst = key
    sf, df = src_funcs.get(src), dst_funcs.get(dst)
    if sf is None or df is None:
        return {"bytes": "no-function", "names": "n/a", "callees": None}
    sb, db = body(src_image, base, sf), body(dst_image, base, df)
    out = {
        "source_name": sf["name"],
        "dest_name": df["name"],
        "source_size": len(sb),
        "dest_size": len(db),
    }
    if sb == db:
        out["bytes"] = "equal"
    elif len(sb) == len(db):
        out["bytes"] = "same-size"
        out["diff_bytes"] = sum(1 for a, b in zip(sb, db) if a != b)
    else:
        out["bytes"] = "resized"
    if is_default(sf["name"]) or is_default(df["name"]):
        out["names"] = "n/a"
    else:
        out["names"] = "agree" if sf["name"] == df["name"] else "differ"
    matched = [c for c in sorted(sf["callees"]) if c in fwd]
    if matched:
        hits = sum(1 for c in matched if fwd[c] & df["callees"])
        out["callees"] = round(hits / len(matched), 3)
    else:
        out["callees"] = None
    return out


def stratified_sample(assoc, n, seed):
    """-> up to n association keys, taken in turn from each correlator set."""
    rng = random.Random(seed)
    groups = {}
    for key in sorted(assoc):
        groups.setdefault("+".join(assoc[key]), []).append(key)
    for keys in groups.values():
        rng.shuffle(keys)
    picked = []
    while len(picked) < n and any(groups.values()):
        for label in sorted(groups):
            if groups[label] and len(picked) < n:
                picked.append(groups[label].pop())
    return sorted(picked)


def range_index(funcs):
    return sorted(
        (lo, hi, entry) for entry, f in funcs.items() for lo, hi in f["ranges"]
    )


def containing(index, addr):
    """-> the entry of the function whose body holds addr, or None."""
    i = bisect.bisect_right(index, (addr, float("inf"), float("inf"))) - 1
    if i >= 0 and index[i][0] <= addr <= index[i][1]:
        return index[i][2]
    return None


def totals(assoc, results):
    """-> {correlator set: {'associations', 'bytes': {...}, 'names': {...},
    'callees_checked', 'callees_mean'}}."""
    out = {}
    for key, corr in assoc.items():
        t = out.setdefault(
            "+".join(corr),
            {
                "associations": 0,
                "bytes": {},
                "names": {},
                "callees_checked": 0,
                "callees_sum": 0.0,
            },
        )
        r = results[key]
        t["associations"] += 1
        t["bytes"][r["bytes"]] = t["bytes"].get(r["bytes"], 0) + 1
        t["names"][r["names"]] = t["names"].get(r["names"], 0) + 1
        if r["callees"] is not None:
            t["callees_checked"] += 1
            t["callees_sum"] += r["callees"]
    for t in out.values():
        s = t.pop("callees_sum")
        t["callees_mean"] = (
            round(s / t["callees_checked"], 3) if t["callees_checked"] else None
        )
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("matches", help="JSON from tools/coldfire/ghidravt.py")
    p.add_argument("--source-image", required=True)
    p.add_argument("--source-dump", required=True)
    p.add_argument("--dest-image", required=True)
    p.add_argument("--dest-dump", required=True)
    p.add_argument("--base", type=lambda s: int(s, 0), default=0x40000400)
    p.add_argument("--sample", type=int, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument(
        "--lookup",
        nargs="+",
        default=[],
        metavar="ADDR",
        type=lambda s: int(s, 0),
        help="source addresses to carry across",
    )
    p.add_argument(
        "--json", help="write totals, sample, multiples, lookups and every result"
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with open(args.matches) as f:
        report = json.load(f)
    with open(args.source_image, "rb") as f:
        src_image = f.read()
    with open(args.dest_image, "rb") as f:
        dst_image = f.read()
    src_funcs, dst_funcs = load_dump(args.source_dump), load_dump(args.dest_dump)
    assoc = associations(report)
    fwd, back = forward_map(assoc)
    results = {
        key: check(key, src_funcs, dst_funcs, src_image, dst_image, args.base, fwd)
        for key in assoc
    }

    print("%s -> %s" % (report.get("source"), report.get("dest")))
    print(
        "%d accepted function associations: %d of %d source functions (%.1f%%), "
        "%d of %d destination functions (%.1f%%)"
        % (
            len(assoc),
            len(fwd),
            len(src_funcs),
            100.0 * len(fwd) / len(src_funcs),
            len(back),
            len(dst_funcs),
            100.0 * len(back) / len(dst_funcs),
        )
    )
    tot = totals(assoc, results)
    for label in sorted(tot, key=lambda k: -tot[k]["associations"]):
        t = tot[label]
        print("  %6d  %s" % (t["associations"], label))
        print(
            "          bytes %s  names %s  callees %s over %d"
            % (
                json.dumps(t["bytes"], sort_keys=True),
                json.dumps(t["names"], sort_keys=True),
                t["callees_mean"],
                t["callees_checked"],
            )
        )

    sample = stratified_sample(assoc, args.sample, args.seed)
    print("sample (seed %d):" % args.seed)
    for key in sample:
        r = results[key]
        print(
            "  0x%08x -> 0x%08x  %-9s %s  names %-5s callees %s  %s -> %s  [%s]"
            % (
                key[0],
                key[1],
                r["bytes"],
                r.get("diff_bytes", ""),
                r["names"],
                r["callees"],
                r.get("source_name"),
                r.get("dest_name"),
                "+".join(assoc[key]),
            )
        )

    multi_src = {s: sorted(d) for s, d in fwd.items() if len(d) > 1}
    multi_dst = {d: sorted(s) for d, s in back.items() if len(s) > 1}
    print(
        "%d source functions have several matches, %d destination functions have several"
        % (len(multi_src), len(multi_dst))
    )
    for s in sorted(multi_src)[:10]:
        print("  0x%08x -> %s" % (s, " ".join("0x%08x" % d for d in multi_src[s])))

    index = range_index(src_funcs)
    lookups = []
    for addr in args.lookup:
        entry = containing(index, addr)
        item = {"addr": "0x%08x" % addr, "source_function": None, "matches": []}
        if entry is not None:
            item["source_function"] = {
                "entry": "0x%08x" % entry,
                "name": src_funcs[entry]["name"],
                "offset": addr - entry,
            }
            for dst in sorted(fwd.get(entry, ())):
                r = results[(entry, dst)]
                moved = (
                    dst + (addr - entry)
                    if r["bytes"] in ("equal", "same-size")
                    else None
                )
                item["matches"].append(
                    {
                        "dest": "0x%08x" % dst,
                        "dest_name": r.get("dest_name"),
                        "bytes": r["bytes"],
                        "callees": r["callees"],
                        "addr": "0x%08x" % moved if moved is not None else None,
                        "correlators": assoc[(entry, dst)],
                    }
                )
        lookups.append(item)
        print("lookup %s: %s" % (item["addr"], json.dumps(item, sort_keys=True)))

    if args.json:
        out = {
            "matches": args.matches,
            "source": report.get("source"),
            "dest": report.get("dest"),
            "totals": tot,
            "sample": [
                dict(
                    results[k],
                    source="0x%08x" % k[0],
                    dest="0x%08x" % k[1],
                    correlators=assoc[k],
                )
                for k in sample
            ],
            "multiple_sources": {
                "0x%08x" % s: ["0x%08x" % d for d in v] for s, v in multi_src.items()
            },
            "multiple_dests": {
                "0x%08x" % d: ["0x%08x" % s for s in v] for d, v in multi_dst.items()
            },
            "lookups": lookups,
            "results": [
                dict(
                    results[k],
                    source="0x%08x" % k[0],
                    dest="0x%08x" % k[1],
                    correlators=assoc[k],
                )
                for k in sorted(assoc)
            ],
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
