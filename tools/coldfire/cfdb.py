#!/usr/bin/env python3
"""One queryable SQLite database per ColdFire (MAIN OS) image: functions,
call/jump edges, data references, strings and symbols, imported from a
tools/coldfire/ghidradump.py dump directory (manifest.json, xrefs.sqlite, decomp/,
disasm/).

    uv run python -m tools.coldfire.cfdb build out/ghidra/dt2-1.16-emac
        [--name dt2-1.16] [--out out/cfdb/dt2-1.16.sqlite] [--force]

This exists for the same reason tools/sharc_dev/sharcdb.py does for the SHARC+ side
(see that file's module docstring): "who calls 0x400caf48" or "what writes
0x80004704" should be a `sqlite3` query in milliseconds against a database
built once, not a fresh xrefs.sqlite scan and a throwaway script every time.
It is a separate tool from tools/sharc_dev/sharcdb.py (which also has an
`import-ghidra` subcommand, built for the same xrefs.sqlite/manifest.json
shape) rather than a second image type sharing that file's schema and
DB_VERSION: sharcdb.py's DB_VERSION and code-block bookkeeping exist for the
SHARC+ side's own decode concerns, and a ColdFire image has no `sw` (SHARC
sequencer word) addressing, no code-block selection and no basic-block/
reaching-definitions layer -- keeping this schema and version number
separate means a SHARC-only schema change never forces a ColdFire rebuild
and vice versa. tools/coldfire/cf.py is the query layer, mirroring tools/sharc_dev/sharc.py.

Ghidra's call/data-ref tables miss code outside functions (small trampolines,
raw branch targets never turned into a function) -- an empty `callers()`
here is not evidence of dead code; confirm with tools/coldfire/refscan.py on the raw
image before recording "no caller" (see CLAUDE.md's Ghidra section and
tools/sharc_dev/sharcdb.py's own "empty Ghidra caller list" note).

Decompiled C and disassembly text are NOT copied into the database (60 MB /
72 MB for the current dt2-1.16-emac dump -- see functions.decomp_path/
disasm_path, resolved against meta['ghidra_dump_dir'] by tools/coldfire/cf.py's
Image.decomp()/disasm() instead of duplicating that text here).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import time

DB_VERSION = 2
DEFAULT_DB_DIR = "out/cfdb"

SCHEMA = """
CREATE TABLE meta (image TEXT, key TEXT, value TEXT, PRIMARY KEY (image, key));

CREATE TABLE functions (
  image TEXT, entry INTEGER, end INTEGER, name TEXT, namespace TEXT,
  size INTEGER, signature TEXT, decomp_path TEXT, disasm_path TEXT,
  decomp_error TEXT,
  PRIMARY KEY (image, entry)
);
CREATE INDEX functions_range ON functions(image, entry, end);
CREATE INDEX functions_name ON functions(image, name);

-- kind: normalized ('call'/'jump'/'cond_jump'); raw_kind: Ghidra's own
-- RefType name (UNCONDITIONAL_CALL, COMPUTED_JUMP, ...).
CREATE TABLE edges (
  image TEXT, site INTEGER, from_func INTEGER, to_func INTEGER,
  to_addr INTEGER, kind TEXT, raw_kind TEXT
);
CREATE INDEX edges_to_func ON edges(image, to_func);
CREATE INDEX edges_to_addr ON edges(image, to_addr);
CREATE INDEX edges_from_func ON edges(image, from_func);

CREATE TABLE datarefs (
  image TEXT, site INTEGER, func INTEGER, to_addr INTEGER, kind TEXT,
  label TEXT, block TEXT
);
CREATE INDEX datarefs_addr ON datarefs(image, to_addr);
CREATE INDEX datarefs_func ON datarefs(image, func);

CREATE TABLE strings (image TEXT, addr INTEGER, text TEXT);
CREATE INDEX strings_addr ON strings(image, addr);

CREATE TABLE symbols (
  image TEXT, addr INTEGER, name TEXT, namespace TEXT, type TEXT,
  is_primary INTEGER
);
CREATE INDEX symbols_addr ON symbols(image, addr);
CREATE INDEX symbols_name ON symbols(image, name);

-- Version 2: cross-version matching facts (tools/coldfire/cffacts.py, tools/coldfire/cfmatch.py).
-- h_*: masked instruction hashes of the function body; mnem_seq: its mnemonics.
CREATE TABLE func_hash (
  image TEXT, entry INTEGER, ninsn INTEGER, nbytes INTEGER,
  h_strict TEXT, h_loose TEXT, h_mnem TEXT, mnem_seq TEXT,
  PRIMARY KEY (image, entry)
);
CREATE INDEX func_hash_strict ON func_hash(image, h_strict);
CREATE INDEX func_hash_loose ON func_hash(image, h_loose);

-- RTTI from the dump (tools/coldfire/rttiscan.py applied): typeinfo objects and one row
-- per vtable slot.
CREATE TABLE typeinfos (
  image TEXT, addr INTEGER, kind TEXT, name TEXT, mangled TEXT, bases TEXT
);
CREATE INDEX typeinfos_name ON typeinfos(image, name);
CREATE TABLE vtables (
  image TEXT, addr INTEGER, class TEXT, typeinfo INTEGER, offset_to_top INTEGER,
  slot INTEGER, target INTEGER, owner TEXT
);
CREATE INDEX vtables_class ON vtables(image, class);
CREATE INDEX vtables_target ON vtables(image, target);

-- Names carried from another image by tools/coldfire/cfmatch.py port.
CREATE TABLE ported (
  image TEXT, entry INTEGER, name TEXT, from_image TEXT, from_entry INTEGER,
  method TEXT, score REAL,
  PRIMARY KEY (image, entry, name)
);
"""

_CALL_KIND = {
    "UNCONDITIONAL_CALL": "call",
    "COMPUTED_CALL": "call",
    "UNCONDITIONAL_JUMP": "jump",
    "COMPUTED_JUMP": "jump",
    "CONDITIONAL_JUMP": "cond_jump",
}


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cffacts():
    from tools.coldfire import cffacts
    return cffacts


def _find_image(manifest, sha):
    """The MAIN OS file whose SHA-256 is the dump's (the manifest's recorded
    path can name a later image: sections/ is overwritten by each extract)."""
    import glob
    import hashlib

    cands = [manifest.get("sections_image"), manifest.get("image_path")]
    cands += sorted(
        glob.glob(os.path.join(ROOT, "out/sections/*/section_3_MAIN_OS.bin"))
    )
    for cand in cands:
        if not cand:
            continue
        for path in (cand, os.path.join(ROOT, cand)):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == sha:
                        return path
    return None


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def read_meta(path):
    if not os.path.exists(path):
        return {}
    db = sqlite3.connect(path)
    try:
        image = db.execute("SELECT DISTINCT image FROM meta").fetchone()
        if image is None:
            return {}
        return dict(
            db.execute("SELECT key, value FROM meta WHERE image=?", image).fetchall()
        )
    finally:
        db.close()


def _table_exists(db, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _function_end(entry, size, ranges_by_func):
    """The entry's own contiguous end address: the range whose lo == entry
    when the dump recorded one (a function can have disjoint chunks, e.g. a
    switch's case bodies far from the entry -- see tools/sharc_dev/sharcdb.py's
    build_ghidra_database for the same situation on the SHARC side), else a
    size-based fallback."""
    spans = ranges_by_func.get(entry)
    if not spans:
        return entry + max(size or 1, 1)
    for lo, hi in spans:
        if lo == entry:
            return hi + 1
    return max(hi for _lo, hi in spans) + 1


def build_database(dump_dir, out_path, name=None, force=False):
    """Import a tools/coldfire/ghidradump.py dump directory into out_path. Returns a
    stats dict with skipped=True (and no other keys but name/path/seconds/
    size) when the existing database's image_sha256+DB_VERSION already match
    the dump's manifest and `force` wasn't given."""
    t0 = time.time()
    manifest_path = os.path.join(dump_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit("cfdb build: no manifest.json in %s" % dump_dir)
    with open(manifest_path) as f:
        manifest = json.load(f)
    if not manifest.get("complete"):
        raise SystemExit(
            "cfdb build: %s is not a complete dump (manifest 'complete' is false)"
            % dump_dir
        )
    sha = manifest.get("image_sha256")
    if not sha:
        raise SystemExit(
            "cfdb build: %s has no image_sha256 in its manifest" % dump_dir
        )
    if name is None:
        name = os.path.basename(os.path.normpath(dump_dir))

    if not force and os.path.exists(out_path):
        existing = read_meta(out_path)
        if existing.get("image_sha256") == sha and existing.get("db_version") == str(
            DB_VERSION
        ):
            return {
                "name": name,
                "path": out_path,
                "skipped": True,
                "seconds": time.time() - t0,
                "size": os.path.getsize(out_path),
            }

    xrefs_path = os.path.join(dump_dir, "xrefs.sqlite")
    if not os.path.exists(xrefs_path):
        raise SystemExit("cfdb build: no xrefs.sqlite in %s" % dump_dir)
    src = sqlite3.connect(xrefs_path)
    try:
        functions = src.execute(
            "SELECT entry, name, namespace, size, signature, decomp, disasm, decomp_error "
            "FROM functions ORDER BY entry"
        ).fetchall()
        ranges_by_func = collections.defaultdict(list)
        for func, lo, hi in src.execute("SELECT func, lo, hi FROM function_ranges"):
            ranges_by_func[func].append((lo, hi))
        calls = src.execute(
            "SELECT from_func, to_func, to_addr, site, kind FROM calls"
        ).fetchall()
        data_refs = src.execute(
            "SELECT site, func, to_addr, kind, label, block FROM data_refs"
        ).fetchall()
        strings = (
            src.execute("SELECT addr, text FROM strings").fetchall()
            if _table_exists(src, "strings")
            else []
        )
        symbols = (
            src.execute(
                "SELECT addr, name, namespace, type, is_primary FROM symbols"
            ).fetchall()
            if _table_exists(src, "symbols")
            else []
        )
        typeinfos = (
            src.execute(
                "SELECT addr, kind, name, mangled, bases FROM typeinfos"
            ).fetchall()
            if _table_exists(src, "typeinfos")
            else []
        )
        vtables = (
            src.execute(
                "SELECT addr, class, typeinfo, offset_to_top, slot, target, owner "
                "FROM vtables"
            ).fetchall()
            if _table_exists(src, "vtables")
            else []
        )
    finally:
        src.close()

    facts = {}
    image_file = _find_image(manifest, sha)
    if image_file is not None:
        cffacts = _cffacts()
        facts = cffacts.compute(
            cffacts.Path(image_file),
            cffacts.DEFAULT_BASE,
            {e: ranges_by_func[e] for e, *_ in functions if ranges_by_func.get(e)},
        )

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = os.path.join(
        out_dir,
        ".%s.tmp-%d-%d"
        % (os.path.basename(out_path), os.getpid(), int(time.time() * 1e6)),
    )
    try:
        db = open_db(tmp_path)
        try:
            db.executemany(
                "INSERT INTO meta VALUES (?,?,?)",
                [
                    (name, "image", name),
                    (name, "image_sha256", sha),
                    (name, "db_version", str(DB_VERSION)),
                    (
                        name,
                        "build_time",
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    ),
                    (name, "ghidra_dump_dir", os.path.abspath(dump_dir)),
                    (name, "ghidra_project", manifest.get("project") or ""),
                    (name, "ghidra_program", manifest.get("program") or ""),
                    (name, "language", manifest.get("language") or ""),
                ],
            )

            func_rows = [
                (
                    name,
                    entry,
                    _function_end(entry, size, ranges_by_func),
                    fname,
                    namespace,
                    size,
                    signature,
                    decomp,
                    disasm,
                    decomp_error,
                )
                for entry, fname, namespace, size, signature, decomp, disasm, decomp_error in functions
            ]
            db.executemany(
                "INSERT INTO functions VALUES (?,?,?,?,?,?,?,?,?,?)", func_rows
            )

            edge_rows = [
                (
                    name,
                    site,
                    from_func,
                    to_func,
                    to_addr,
                    _CALL_KIND.get(kind, "jump"),
                    kind,
                )
                for from_func, to_func, to_addr, site, kind in calls
            ]
            db.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", edge_rows)

            dataref_rows = [
                (name, site, func, to_addr, kind, label, block)
                for site, func, to_addr, kind, label, block in data_refs
            ]
            db.executemany("INSERT INTO datarefs VALUES (?,?,?,?,?,?,?)", dataref_rows)

            string_rows = [(name, addr, text) for addr, text in strings]
            db.executemany("INSERT INTO strings VALUES (?,?,?)", string_rows)

            symbol_rows = [
                (name, addr, sname, namespace, typ, is_primary)
                for addr, sname, namespace, typ, is_primary in symbols
            ]
            db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?)", symbol_rows)

            db.executemany(
                "INSERT INTO func_hash VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        name, entry, f["ninsn"], f["nbytes"], f["h_strict"],
                        f["h_loose"], f["h_mnem"], f["mnem_seq"],
                    )
                    for entry, f in facts.items()
                ],
            )  # fmt: skip
            db.executemany(
                "INSERT INTO typeinfos VALUES (?,?,?,?,?,?)",
                [(name, *row) for row in typeinfos],
            )
            db.executemany(
                "INSERT INTO vtables VALUES (?,?,?,?,?,?,?,?)",
                [(name, *row) for row in vtables],
            )

            db.commit()
        finally:
            db.close()
        os.replace(tmp_path, out_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return {
        "name": name,
        "path": out_path,
        "skipped": False,
        "seconds": time.time() - t0,
        "size": os.path.getsize(out_path),
        "n_functions": len(func_rows),
        "n_edges": len(edge_rows),
        "n_datarefs": len(dataref_rows),
        "n_strings": len(string_rows),
        "n_symbols": len(symbol_rows),
    }


def cmd_build(args):
    name = args.name or os.path.basename(os.path.normpath(args.dump_dir))
    out = args.out or os.path.join(args.out_dir, name + ".sqlite")
    r = build_database(args.dump_dir, out, name=name, force=args.force)
    if r["skipped"]:
        print(
            "%-16s SKIPPED (up to date)  %s  %.1fs"
            % (r["name"], r["path"], r["seconds"])
        )
        return 0
    print(
        "%-16s built  %s  %.1fs  %.1f KB  functions=%d edges=%d datarefs=%d strings=%d symbols=%d"
        % (
            r["name"],
            r["path"],
            r["seconds"],
            r["size"] / 1024,
            r["n_functions"],
            r["n_edges"],
            r["n_datarefs"],
            r["n_strings"],
            r["n_symbols"],
        )
    )
    return 0


FTS_VERSION = 2


def fts_path(db_path):
    """The full-text sidecar for a database: <name>.fts.sqlite beside it. It is
    a separate file so adding or rebuilding it never touches DB_VERSION or the
    main database (other lanes share out/cfdb)."""
    return db_path[: -len(".sqlite")] + ".fts.sqlite"


def build_fts(db_path, force=False):
    """Build the FTS5 sidecar for one cfdb database and return its path.

    Tables (all FTS5; the rowid is the function entry or string address):
      fts_names   (name, cf_name, klass)   trigram; function, cf_names and RTTI names
      fts_strings (text)                   trigram, so substrings match
      fts_decomp  (body)                   decompiled C, contentless; hits are
                                           located again by reading the dump
    Rebuilt when the main database's image sha256 or FTS_VERSION moves on."""
    out = fts_path(db_path)
    meta = read_meta(db_path)
    stamp = "%s/%s/%s" % (meta.get("image_sha256"), meta.get("build_time"), FTS_VERSION)
    if os.path.exists(out) and not force:
        try:
            con = sqlite3.connect(out)
            row = con.execute("SELECT value FROM fts_meta WHERE key='stamp'").fetchone()
            con.close()
            if row and row[0] == stamp:
                return out
        except sqlite3.Error:
            pass
    tmp = out + ".tmp.%d" % os.getpid()
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(db_path)
    name = meta["image"]
    dump = meta.get("ghidra_dump_dir")
    con = sqlite3.connect(tmp)
    con.executescript(
        "CREATE TABLE fts_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE VIRTUAL TABLE fts_names USING fts5(name, cf_name, klass, "
        "tokenize='trigram');"
        "CREATE VIRTUAL TABLE fts_strings USING fts5(text, tokenize='trigram');"
        "CREATE VIRTUAL TABLE fts_decomp USING fts5(body, content='', detail=column, "
        "tokenize=\"unicode61 tokenchars '_'\");"
    )
    klass_of: dict[int, str] = {}
    for target, cls in src.execute(
        "SELECT target, class FROM vtables WHERE image=? ORDER BY addr, slot", (name,)
    ):
        klass_of.setdefault(target, cls)
    names = _image_names(name)
    rows = []
    for entry, fname in src.execute(
        "SELECT entry, name FROM functions WHERE image=?", (name,)
    ):
        rows.append((entry, fname, names.get(entry, ""), klass_of.get(entry, "")))
    con.executemany(
        "INSERT INTO fts_names(rowid, name, cf_name, klass) VALUES (?,?,?,?)", rows
    )
    con.executemany(
        "INSERT INTO fts_strings(rowid, text) VALUES (?,?)",
        [
            (i, t)
            for i, (_a, t) in enumerate(
                src.execute("SELECT addr, text FROM strings WHERE image=?", (name,))
            )
        ],
    )
    con.execute("CREATE TABLE string_rows (id INTEGER PRIMARY KEY, addr INTEGER)")
    con.executemany(
        "INSERT INTO string_rows VALUES (?,?)",
        [
            (i, a)
            for i, (a,) in enumerate(
                src.execute("SELECT addr FROM strings WHERE image=?", (name,))
            )
        ],
    )
    n = 0
    if dump:
        batch = []
        for entry, path in src.execute(
            "SELECT entry, decomp_path FROM functions WHERE image=? AND decomp_path IS NOT NULL",
            (name,),
        ):
            fp = os.path.join(dump, path)
            if not os.path.exists(fp):
                continue
            with open(fp, errors="replace") as fh:
                batch.append((entry, fh.read()))
            n += 1
            if len(batch) >= 500:
                con.executemany(
                    "INSERT INTO fts_decomp(rowid, body) VALUES (?,?)", batch
                )
                batch = []
        if batch:
            con.executemany("INSERT INTO fts_decomp(rowid, body) VALUES (?,?)", batch)
    con.execute("INSERT INTO fts_meta VALUES ('stamp', ?)", (stamp,))
    con.execute("INSERT INTO fts_meta VALUES ('decomp_files', ?)", (str(n),))
    con.commit()
    con.close()
    src.close()
    os.replace(tmp, out)
    return out


def _image_names(name):
    """tools/coldfire/cf_names*.py names for an image name like dt2-1.17-emac."""
    import importlib

    short = name[: -len("-emac")] if name.endswith("-emac") else name
    mod = {"dt2-1.16": "cf_names", "dt2-1.17": "cf_names_dt2_1_17"}.get(short)
    if mod is None:
        return {}
    try:
        return dict(importlib.import_module(mod).NAME_BY_ADDR)
    except ImportError:
        return {}


def cmd_fts(args):
    path = args.db or os.path.join(args.db_dir, args.name + ".sqlite")
    t = time.time()
    out = build_fts(path, force=args.force)
    print("%s (%.1f s, %.1f MB)" % (out, time.time() - t, os.path.getsize(out) / 1e6))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser(
        "build", help="build a ColdFire image database from a Ghidra dump"
    )
    b.add_argument("dump_dir", help="e.g. out/ghidra/dt2-1.16-emac")
    b.add_argument(
        "--name", help="image name; default is the dump directory's own name"
    )
    b.add_argument(
        "--out", help="output .sqlite path (default: --out-dir/<name>.sqlite)"
    )
    b.add_argument("--out-dir", default=DEFAULT_DB_DIR)
    b.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if sha256+DB_VERSION already match",
    )

    f = sub.add_parser("fts", help="build the FTS5 sidecar for a database")
    f.add_argument("name", help="e.g. dt2-1.17-emac")
    f.add_argument("--db")
    f.add_argument("--db-dir", default=DEFAULT_DB_DIR)
    f.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "fts":
        return cmd_fts(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
