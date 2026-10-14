"""Import of a ColdFire Ghidra dump into a database."""

from __future__ import annotations

import collections
import json
import os
import re
import sqlite3
import time

from .analysis import analyze_image
from .schema import DB_VERSION, git_commit, open_db, read_meta

# --- ColdFire import from a tools/coldfire/ghidradump.py dump -------------------------
#
# See the DB_VERSION comment above for the full column-by-column schema
# mapping and the tables this deliberately leaves empty.

_GHIDRA_CALL_KIND = {
    "UNCONDITIONAL_CALL": "call",
    "COMPUTED_CALL": "call",
    "UNCONDITIONAL_JUMP": "jump",
    "COMPUTED_JUMP": "jump",
    "CONDITIONAL_JUMP": "cond_jump",
}

_GHIDRA_DATAREF_ROLE = {
    "READ": "read", "WRITE": "write", "READ_WRITE": "read_write", "DATA": "data",
}

_DISASM_LINE = re.compile(r"^[0-9a-f]{8}  ")


def _parse_disasm_insns(path):
    """[(sw, raw_hex, mnemonic), ...] from one tools/coldfire/ghidradump.py
    disasm/*.s file, in file order -- see that file's disasm_line()'s own
    '%08x  %-24s  %s' format (address, space-joined 16-bit words, rendered
    text). A comment (';...') or label ('name:') line doesn't start with 8
    hex digits and two spaces and is skipped."""
    rows = []
    with open(path) as f:
        for line in f:
            if not _DISASM_LINE.match(line):
                continue
            parts = re.split(r" {2,}", line.rstrip("\n"), maxsplit=2)
            if len(parts) != 3:
                continue
            addr_hex, words, text = parts
            rows.append((int(addr_hex, 16), words.replace(" ", ""), text))
    return rows


def _fill_ghidra_database(db, name, sha, dump_dir, manifest, functions, ranges, calls, data_refs):
    meta_rows = [
        (name, "image", name),
        (name, "image_sha256", sha),
        (name, "db_version", str(DB_VERSION)),
        (name, "tool_commit", git_commit() or ""),
        (name, "build_time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        (name, "cpu", "coldfire"),
        (name, "ghidra_dump_dir", os.path.abspath(dump_dir)),
        (name, "ghidra_project", manifest.get("project") or ""),
        (name, "ghidra_program", manifest.get("program") or ""),
        (name, "language", manifest.get("language") or ""),
    ]
    db.executemany("INSERT INTO meta VALUES (?,?,?)", meta_rows)

    callers = collections.defaultdict(set)
    callees = collections.defaultdict(set)
    edge_rows = []
    for from_fn, to_fn, to_addr, site, kind in calls:
        if to_fn is not None:
            callees[from_fn].add(to_fn)
            callers[to_fn].add(from_fn)
        edge_rows.append((name, site, to_addr, _GHIDRA_CALL_KIND.get(kind, "jump"), None, None,
                          from_fn, to_fn, kind))

    dataref_rows = [
        (name, to_addr, site, "ghidra", _GHIDRA_DATAREF_ROLE.get(kind, kind.lower()))
        for site, _func, to_addr, kind in data_refs
    ]

    func_rows, insn_rows, decomp_rows = [], [], []
    for entry, fname, size, decomp_path, disasm_path in functions:
        # A Ghidra function's body can be several disjoint ranges (a switch
        # whose case bodies live far from the entry, seen on ~130 functions
        # in dt2-1.16-emac -- e.g. ParameterSet::vfunc_23 spans entry..entry
        # +141 plus a dozen scattered chunks up near 0x400e2258). functions.
        # end_sw only covers the ENTRY's own contiguous range (the one whose
        # lo == entry), not every scattered chunk: using max(hi) across all
        # ranges instead made func()'s [entry_sw, end_sw) span swallow
        # unrelated code in between -- confirmed by hand on 0x400caf48, which
        # sits inside its own small FUN_400cae8c, not inside vfunc_23's
        # bogus multi-megabyte span. A scattered function's other chunks are
        # simply not covered by any functions row here (func() on an address
        # in one of them returns None or, if another function's own entry
        # range happens to include it, that other function -- never this
        # one); their instructions are still in `insn` (grouped by disasm
        # file, i.e. by owning function, not by this span).
        spans = ranges.get(entry) or [(entry, entry + max(size, 1) - 1)]
        entry_span = next((hi for lo, hi in spans if lo == entry), max(hi for _lo, hi in spans))
        end_sw = entry_span + 1
        insns = _parse_disasm_insns(os.path.join(dump_dir, disasm_path)) if disasm_path else []
        for sw, raw_hex, mnemonic in insns:
            insn_rows.append((name, sw, None, len(raw_hex) // 2, raw_hex, None, None, None,
                              mnemonic, 1, None, entry))
        func_rows.append((
            name, entry, end_sw, len(insns), None, fname,
            int(bool(callers.get(entry))), int(not callees.get(entry)), None, None,
        ))
        if decomp_path:
            full = os.path.join(dump_dir, decomp_path)
            if os.path.exists(full):
                with open(full) as f:
                    decomp_rows.append((name, entry, f.read()))

    db.executemany("INSERT INTO functions VALUES (?,?,?,?,?,?,?,?,?,?)", func_rows)
    db.executemany("INSERT INTO insn VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", insn_rows)
    db.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)", edge_rows)
    db.executemany("INSERT INTO dataref VALUES (?,?,?,?,?)", dataref_rows)
    db.executemany("INSERT INTO decomp VALUES (?,?,?)", decomp_rows)

    return {
        "n_functions": len(func_rows), "n_insn": len(insn_rows), "n_edges": len(edge_rows),
        "n_dataref": len(dataref_rows), "n_decomp": len(decomp_rows),
    }


def build_ghidra_database(dump_dir, out_path, name=None, force=False):
    """Import a tools/coldfire/ghidradump.py dump directory (manifest.json, xrefs.sqlite,
    disasm/, decomp/) into the same sharcdb schema as build_database(), for
    a ColdFire image. Returns a stats dict; see the DB_VERSION comment for
    the exact column mapping and the tables this leaves empty."""
    t0 = time.time()
    manifest_path = os.path.join(dump_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit("sharcdb import-ghidra: no manifest.json in %s" % dump_dir)
    with open(manifest_path) as f:
        manifest = json.load(f)
    if not manifest.get("complete"):
        raise SystemExit("sharcdb import-ghidra: %s is not a complete dump (manifest 'complete' is false)" % dump_dir)
    sha = manifest.get("image_sha256")
    if not sha:
        raise SystemExit("sharcdb import-ghidra: %s has no image_sha256 in its manifest" % dump_dir)
    if name is None:
        name = os.path.basename(os.path.normpath(dump_dir))

    if not force and os.path.exists(out_path):
        existing = read_meta(out_path)
        if existing.get("image_sha256") == sha and existing.get("db_version") == str(DB_VERSION):
            return {
                "name": name, "path": out_path, "skipped": True,
                "seconds": time.time() - t0, "size": os.path.getsize(out_path),
            }

    xrefs_path = os.path.join(dump_dir, "xrefs.sqlite")
    src = sqlite3.connect(xrefs_path)
    try:
        functions = src.execute(
            "SELECT entry, name, size, decomp, disasm FROM functions ORDER BY entry"
        ).fetchall()
        ranges = collections.defaultdict(list)
        for func, lo, hi in src.execute("SELECT func, lo, hi FROM function_ranges"):
            ranges[func].append((lo, hi))
        calls = src.execute("SELECT from_func, to_func, to_addr, site, kind FROM calls").fetchall()
        data_refs = src.execute("SELECT site, func, to_addr, kind FROM data_refs").fetchall()
    finally:
        src.close()

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = os.path.join(
        out_dir, ".%s.tmp-%d-%d" % (os.path.basename(out_path), os.getpid(), int(time.time() * 1e6))
    )
    try:
        db = open_db(tmp_path)
        try:
            counts = _fill_ghidra_database(db, name, sha, dump_dir, manifest, functions, ranges, calls, data_refs)
            counts["analyze"] = analyze_image(db, name, mem=None, blocks=None)
            db.commit()
        finally:
            db.close()
        os.replace(tmp_path, out_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return {
        "name": name, "path": out_path, "skipped": False,
        "seconds": time.time() - t0, "size": os.path.getsize(out_path), **counts,
    }
