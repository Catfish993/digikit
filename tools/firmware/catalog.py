"""catalog.py: the CPU-independent part of the anchor catalogue.

The ColdFire collector (tools/coldfire/atlas.py) and the SHARC collector
(tools/sharc_dev/sharc_catalog.py) build rows of candidate anchor functions.
This module holds what they share: rule suggestion, grade, call-graph groups
and the sqlite, TSV and JSON writers. Rows are dicts keyed by column name.
"""

from __future__ import annotations

import collections
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

# Rule kinds that find a function by its hash.
FUNC_KINDS = ("cf_func", "sharc_func")
# Pair methods that count as a reliable match between the two devices.
STRONG_PAIR = frozenset({"strict", "loose", "strings", "vtable"})
MAX_SAME_HASH = (
    4  # up to this many functions with one hash, the address order picks one
)
# Louvain resolution: 2.0 gives about 50 groups of 10+ rows on DT2 1.17 (1.0 gives 23, with two of 600+ rows).
GROUP_RESOLUTION = 2.0

GROUP_COLUMNS = (
    ("id", "INTEGER"),
    ("size", "INTEGER"),
    ("anchored", "INTEGER"),
    ("grade", "INTEGER"),
    ("top_classes", "TEXT"),
    ("top_strings", "TEXT"),
    ("top_tags", "TEXT"),
    ("best", "INTEGER"),
)


def suggest_rule(
    entry: int,
    same_hash_entries_sorted: Sequence[int],
    is_vfunc: bool,
    func_kind: str = "cf_func",
) -> tuple[str, int | None]:
    """Rule kind and ordinal that would find `entry` again on another image."""
    n = len(same_hash_entries_sorted)
    if n <= 1:
        return (func_kind, None)
    if n <= MAX_SAME_HASH and entry in same_hash_entries_sorted:
        return (func_kind, list(same_hash_entries_sorted).index(entry))
    if n > MAX_SAME_HASH and is_vfunc:
        return ("vtable_slot", None)  # label only; not resolvable yet
    return ("none", None)


def catalog_grade(
    rule_kind: str, stable: int | None, pair_method: str | None, rule_both: int | None
) -> int:
    """1 when the function can be an anchor on both devices by hash."""
    return int(
        rule_kind in FUNC_KINDS
        and stable == 1
        and pair_method in STRONG_PAIR
        and rule_both == 1
    )


def number_groups(communities: Iterable[Iterable[int]]) -> dict[int, int]:
    """Address -> group id. Biggest community first, then lowest address; singletons get -1."""
    sets = sorted((sorted(c) for c in communities), key=lambda c: (-len(c), c[0]))
    out: dict[int, int] = {}
    gid = 0
    for members in sets:
        if len(members) < 2:
            ids = -1
        else:
            ids = gid
            gid += 1
        for a in members:
            out[a] = ids
    return out


def count_list(counts: dict[str, int], limit: int) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def group_edges(
    addrs: Iterable[int], callee_n: dict[int, collections.Counter[int]]
) -> dict[tuple[int, int], int]:
    """Undirected edges (low, high) -> call sites in either direction, between `addrs`."""
    nodes = set(addrs)
    out: dict[tuple[int, int], int] = {}
    for f in nodes:
        for t, n in callee_n.get(f, {}).items():
            if t in nodes and t != f:
                key = (f, t) if f < t else (t, f)
                out[key] = out.get(key, 0) + n
    return out


def louvain_groups(
    addrs: Iterable[int],
    edges: dict[tuple[int, int], int],
    resolution: float = GROUP_RESOLUTION,
) -> dict[int, int]:
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(sorted(addrs))
    g.add_weighted_edges_from((a, b, w) for (a, b), w in sorted(edges.items()))
    comms = nx.community.louvain_communities(
        g, weight="weight", resolution=resolution, seed=0
    )
    return number_groups(comms)


def parse_counts(text: str) -> list[tuple[str, int]]:
    # Class names can hold commas (template arguments), so match "name:count" up to the next "," or the end.
    return [(k, int(n)) for k, n in re.findall(r"(.+?):(\d+)(?:,|$)", text)]


def catalog_group_rows(
    rows: list[dict[str, Any]], class_col: str = "class", hint_col: str = "hint"
) -> list[dict[str, Any]]:
    """One row per group (id >= 0) with a summary of its members.

    `class_col` and `hint_col` name the row fields counted as top_classes and top_strings.
    """
    members: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        if r["group"] >= 0:
            members.setdefault(r["group"], []).append(r)
    out = []
    for gid in sorted(members):
        ms = members[gid]
        classes: dict[str, int] = {}
        hints: dict[str, int] = {}
        tags: dict[str, int] = {}
        for r in ms:
            if r[class_col]:
                classes[r[class_col]] = classes.get(r[class_col], 0) + 1
            for c, n in parse_counts(r.get("caller_classes") or ""):
                classes[c] = classes.get(c, 0) + n
            if r[hint_col]:
                hints[r[hint_col]] = hints.get(r[hint_col], 0) + 1
            for t in r["tags"].split(","):
                if t:
                    k = t.split(":")[0]
                    tags[k] = tags.get(k, 0) + 1
        best = min(ms, key=lambda r: (-r["score"], r["addr"]))
        out.append(
            {
                "id": gid,
                "size": len(ms),
                "anchored": sum(1 for r in ms if r["anchors"]),
                "grade": sum(1 for r in ms if r["anchor_grade"] == 1),
                "top_classes": ",".join("%s:%d" % kv for kv in count_list(classes, 5)),
                "top_strings": " | ".join(k for k, _n in count_list(hints, 3)),
                "top_tags": ",".join("%s:%d" % kv for kv in count_list(tags, 5)),
                "best": best["addr"],
            }
        )
    return out


def _write_table(
    db_path: str,
    table: str,
    columns: Sequence[tuple[str, str]],
    rows: list[dict[str, Any]],
) -> None:
    """Drop and recreate `table` in the sqlite file and fill it with `rows`."""
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE IF EXISTS %s" % table)
    con.execute(
        "CREATE TABLE %s (%s)" % (table, ", ".join('"%s" %s' % c for c in columns))
    )
    if rows:
        quoted = ",".join('"%s"' % c for c, _t in columns)
        con.executemany(
            "INSERT INTO %s(%s) VALUES (%s)"
            % (table, quoted, ",".join("?" * len(columns))),
            [tuple(r[c] for c, _t in columns) for r in rows],
        )
    if table == "catalog":
        con.execute("CREATE INDEX IF NOT EXISTS catalog_score ON catalog(score)")
    con.commit()
    con.close()


def write_catalog(
    db_path: str,
    tsv_path: str,
    rows: list[dict[str, Any]],
    columns: Sequence[tuple[str, str]],
    addr_fmt: str = "0x%08x",
) -> None:
    """Write table `catalog` and the catalog TSV, best score first."""
    _write_table(db_path, "catalog", columns, rows)
    rows = sorted(rows, key=lambda r: (-r["score"], r["addr"]))
    with open(tsv_path, "w") as fh:
        fh.write("\t".join(c for c, _t in columns) + "\n")
        for r in rows:
            cells = []
            for c, _t in columns:
                v = r[c]
                if v is None:
                    cells.append("")
                elif c in ("addr", "pair_addr"):
                    cells.append(addr_fmt % v)
                elif c == "score":
                    cells.append("%.2f" % v)
                else:
                    cells.append(str(v))
            fh.write("\t".join(cells) + "\n")


def write_catalog_groups(
    image: str,
    rows: list[dict[str, Any]],
    edges: dict[tuple[int, int], int],
    db_path: str,
    tsv_path: str,
    graph_path: str,
    addr_fmt: str = "0x%08x",
    class_col: str = "class",
    hint_col: str = "hint",
) -> int:
    """Write table catalog_groups, the groups TSV and the graph JSON; return the group count."""
    groups = catalog_group_rows(rows, class_col, hint_col)
    _write_table(db_path, "catalog_groups", GROUP_COLUMNS, groups)
    with open(tsv_path, "w") as fh:
        fh.write("\t".join(c for c, _t in GROUP_COLUMNS) + "\n")
        for g in groups:
            fh.write(
                "\t".join(
                    addr_fmt % g[c] if c == "best" else str(g[c])
                    for c, _t in GROUP_COLUMNS
                )
                + "\n"
            )
    graph = {
        "image": image,
        "nodes": [
            {
                "addr": addr_fmt % r["addr"],
                "name": r["name"],
                "work_name": r.get("work_name", ""),
                "class": r[class_col],
                "group": r["group"],
                "score": round(r["score"], 2),
                "anchors": r["anchors"],
            }
            for r in sorted(rows, key=lambda r: r["addr"])
        ],
        "edges": [
            [addr_fmt % a, addr_fmt % b, w] for (a, b), w in sorted(edges.items())
        ],
    }
    with open(graph_path, "w") as fh:
        json.dump(graph, fh, indent=1)
        fh.write("\n")
    return len(groups)
