#!/usr/bin/env python3
"""fw.py: one compact query CLI over the firmware databases, for agents.

    uv run python -m tools.coldfire.fw fn 0x401be1be -i dt2-1.17
    uv run python -m tools.coldfire.fw batch queries.txt -i dt2-1.17      # or '-' for stdin

IMAGE is dt2-1.17, dn2-1.12, ... : ColdFire (out/cfdb/IMAGE-emac.sqlite) and
SHARC (out/sharcdb/IMAGE.sqlite) are both opened from it. The CPU is chosen
from the address (>= 0x400000 is ColdFire) or with --cpu. Set FW_IMAGE to skip
-i. Output is plain text, capped at --max lines (default 60; 0 = no cap) with a
"... N more" tail, so a single call never floods the context.

Subcommands: fn decomp disasm callers callees xref str class path port sql
search atlas catalog batch. In a batch file each line is one command line (without
"fw.py"); lines starting with # are comments; every result block starts with
"## <line>". The call graph is cached on disk (out/cache/fw/), keyed by the
database hash, so a cold process only pays for opening SQLite.

Ghidra's call/data-ref tables miss code outside functions: an empty callers
list is not evidence of dead code (AGENTS.md; tools/coldfire/refscan.py confirms).
"""

from __future__ import annotations

import argparse
import os
import pickle
import re
import shlex
import sqlite3
import sys
from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

CF_DIR = os.path.join(ROOT, "out", "cfdb")
SHARC_DIR = os.path.join(ROOT, "out", "sharcdb")
ATLAS_DIR = os.path.join(ROOT, "out", "atlas")
CACHE_DIR = os.path.join(ROOT, "out", "cache", "fw")
DEFAULT_MAX = 60
CF_BASE = 0x10000000  # ColdFire addresses start at 0x40000000, SHARC words stay below 0x1000000


class FwError(Exception):
    """A usage or lookup problem reported as one line, not a traceback."""


# --- output -----------------------------------------------------------------


def capped(lines: Sequence[str], cap: int) -> str:
    lines = list(lines)
    if cap and len(lines) > cap:
        more = len(lines) - cap
        lines = lines[:cap] + ["... %d more (--max N, 0 = all)" % more]
    return "\n".join(lines)


def clip(text: str, n: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


# --- shared helpers -----------------------------------------------------------


def short_image(name: str) -> str:
    return name[: -len("-emac")] if name.endswith("-emac") else name


def parse_addr(text: str) -> int | None:
    t = text.strip().lower()
    if t.startswith("fun_") or t.startswith("lab_") or t.startswith("dat_"):
        t = t[4:]
    try:
        if t.startswith("0x"):
            return int(t, 16)
        if re.fullmatch(r"[0-9a-f]{6,8}", t) and re.search(r"[a-f]", t + "a"):
            return int(t, 16)
    except ValueError:
        return None
    return None


def tri_query(term: str) -> str:
    """Trigram FTS query: AND of words, or one phrase when a word is under 3 characters."""
    words = [w.replace('"', "") for w in term.split()]
    if not words:
        raise FwError("empty search term")
    if any(len(w) < 3 for w in words):
        return '"%s"' % " ".join(words)
    return " AND ".join('"%s"' % w for w in words)


def fts_query(term: str, prefix: bool = False) -> str:
    """AND of the quoted words in `term` as an FTS5 query."""
    words = [w.replace('"', "") for w in term.split() if w.replace('"', "")]
    if not words:
        raise FwError("empty search term")
    return " AND ".join('"%s"%s' % (w, "*" if prefix else "") for w in words)


def open_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FwError("missing database %s" % os.path.relpath(path, ROOT))
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def db_key(con: sqlite3.Connection, path: str) -> str:
    meta = (
        dict(con.execute("SELECT key, value FROM meta").fetchall())
        if _has(con, "meta")
        else {}
    )
    sha = (meta.get("image_sha256") or "nosha")[:12]
    return "%s-%s-%s-%d" % (
        sha,
        meta.get("db_version", "0"),
        (meta.get("build_time") or "")[-9:-1].replace(":", ""),
        os.path.getsize(path),
    )


def _has(con: sqlite3.Connection, table: str) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        is not None
    )


class Graph:
    """Function-level call graph as plain dicts (no networkx import)."""

    def __init__(
        self, succ: dict[int, tuple[int, ...]], pred: dict[int, tuple[int, ...]]
    ):
        self.succ = succ
        self.pred = pred

    @staticmethod
    def load(tag: str, key: str, edges: Iterable[tuple[int, int]]) -> tuple[Graph, str]:
        path = os.path.join(CACHE_DIR, "%s.%s.graph.pkl" % (tag, key))
        try:
            with open(path, "rb") as fh:
                succ, pred = pickle.load(fh)
            return Graph(succ, pred), "cached"
        except (OSError, pickle.UnpicklingError, EOFError, ValueError):
            pass
        s: dict[int, set[int]] = {}
        p: dict[int, set[int]] = {}
        for a, b in edges:
            s.setdefault(a, set()).add(b)
            p.setdefault(b, set()).add(a)
        succ = {k: tuple(sorted(v)) for k, v in s.items()}
        pred = {k: tuple(sorted(v)) for k, v in p.items()}
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            for old in os.listdir(CACHE_DIR):
                if old.startswith(tag + ".") and old.endswith(".graph.pkl"):
                    os.remove(os.path.join(CACHE_DIR, old))
            tmp = path + ".%d" % os.getpid()
            with open(tmp, "wb") as fh:
                pickle.dump((succ, pred), fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except OSError:
            pass
        return Graph(succ, pred), "built"

    def shortest(self, a: int, b: int, limit: int = 200000) -> list[int] | None:
        if a == b:
            return [a]
        prev = {a: a}
        q = deque([a])
        seen = 0
        while q:
            n = q.popleft()
            for m in self.succ.get(n, ()):
                if m in prev:
                    continue
                prev[m] = n
                if m == b:
                    out = [b]
                    while out[-1] != a:
                        out.append(prev[out[-1]])
                    return out[::-1]
                q.append(m)
            seen += 1
            if seen > limit:
                break
        return None


# --- ColdFire -----------------------------------------------------------------


class CF:
    cpu = "cf"

    def __init__(self, image: str):
        self.image = short_image(image)
        self.db_name = self.image + "-emac"
        self.path = os.path.join(CF_DIR, self.db_name + ".sqlite")
        self.db = open_ro(self.path)
        self.meta = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        self._graph: Graph | None = None
        self._fts: sqlite3.Connection | None = None
        self._names: dict[int, str] | None = None
        self._starts: list[int] | None = None
        self.graph_state = ""

    # names and lookup
    def names(self) -> dict[int, str]:
        if self._names is None:
            from tools.coldfire import cfdb

            self._names = cfdb._image_names(self.db_name)
        return self._names

    def func_row(self, addr: int) -> tuple[Any, ...] | None:
        return self.db.execute(
            "SELECT entry, end, name, namespace, size, signature, decomp_path, disasm_path "
            "FROM functions WHERE entry<=? AND end>? ORDER BY entry DESC LIMIT 1",
            (addr, addr),
        ).fetchone()

    def resolve(self, token: str) -> int:
        """Entry of the function named or containing `token`."""
        addr = parse_addr(token)
        if addr is None:
            for k in self.names():
                if token == _cfname(self, k):
                    addr = k
                    break
        if addr is None:
            row = self.db.execute(
                "SELECT entry FROM functions WHERE name=?", (token,)
            ).fetchone()
            if row is None:
                row = self.db.execute(
                    "SELECT entry FROM functions WHERE name LIKE ? ORDER BY entry LIMIT 1",
                    (token + "%",),
                ).fetchone()
            if row is None:
                raise FwError(
                    "no function matches %r (try: search %s)" % (token, token)
                )
            addr = row[0]
        f = self.func_row(addr)
        if f is None:
            raise FwError("0x%x is not inside a known function" % addr)
        return f[0]

    def label(self, entry: int) -> str:
        n = self.names().get(entry)
        row = self.db.execute(
            "SELECT name FROM functions WHERE entry=?", (entry,)
        ).fetchone()
        g = row[0] if row else "FUN_%08x" % entry
        return "%08x %s%s" % (entry, g, " [%s]" % n if n else "")

    def label_any(self, addr: int) -> str:
        f = self.func_row(addr)
        if f is None:
            return "%08x" % addr
        if f[0] == addr:
            return self.label(addr)
        return "%s+0x%x" % (self.label(f[0]), addr - f[0])

    def graph(self) -> Graph:
        if self._graph is None:
            self._graph, self.graph_state = Graph.load(
                self.db_name,
                db_key(self.db, self.path),
                self.db.execute(
                    "SELECT DISTINCT from_func, to_func FROM edges WHERE kind='call' "
                    "AND from_func IS NOT NULL AND to_func IS NOT NULL"
                ),
            )
        return self._graph

    def fts(self) -> sqlite3.Connection:
        if self._fts is None:
            from tools.coldfire import cfdb

            path = cfdb.fts_path(self.path)
            if not os.path.exists(path):
                print("# building full-text index (one time)", file=sys.stderr)
                cfdb.build_fts(self.path)
            self._fts = open_ro(path)
        return self._fts

    def read_dump(self, rel: str | None) -> str | None:
        dump = self.meta.get("ghidra_dump_dir")
        if not rel or not dump:
            return None
        p = os.path.join(dump, rel)
        if not os.path.exists(p):
            p = os.path.join(ROOT, "out", "ghidra", self.db_name, rel)
        try:
            with open(p, errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    # queries
    def callers(self, entry: int) -> list[tuple[int, int, str]]:
        """(from_func, site, kind) for edges into the function or its entry."""
        return self.db.execute(
            "SELECT from_func, site, kind FROM edges WHERE to_addr=? OR to_func=? ORDER BY from_func, site",
            (entry, entry),
        ).fetchall()

    def callees(self, entry: int) -> list[tuple[int, str]]:
        return self.db.execute(
            "SELECT DISTINCT to_func, kind FROM edges WHERE from_func=? AND to_func IS NOT NULL ORDER BY to_func",
            (entry,),
        ).fetchall()

    def strings_of(self, entry: int, limit: int = 8) -> list[tuple[int, str]]:
        return self.db.execute(
            "SELECT DISTINCT s.addr, s.text FROM datarefs d JOIN strings s ON s.addr=d.to_addr "
            "WHERE d.func=? ORDER BY s.addr LIMIT ?",
            (entry, limit),
        ).fetchall()

    def vtable_slots(self, entry: int) -> list[tuple[str, int]]:
        return self.db.execute(
            "SELECT DISTINCT class, slot FROM vtables WHERE target=? ORDER BY class, slot",
            (entry,),
        ).fetchall()


def _cfname(img: CF, entry: int) -> str | None:
    return img.names().get(entry)


# --- SHARC --------------------------------------------------------------------


class Sharc:
    cpu = "sharc"

    def __init__(self, image: str):
        self.image = short_image(image)
        self.path = os.path.join(SHARC_DIR, self.image + ".sqlite")
        self.db = open_ro(self.path)
        self.meta = (
            dict(self.db.execute("SELECT key, value FROM meta").fetchall())
            if _has(self.db, "meta")
            else {}
        )
        self.meta = {k.split("|")[-1]: v for k, v in self.meta.items()} or self.meta
        rows = self.db.execute(
            "SELECT key, value FROM meta WHERE image=?", (self.image,)
        ).fetchall()
        if rows:
            self.meta = dict(rows)
        self._graph: Graph | None = None
        self.graph_state = ""
        notes = os.path.join(SHARC_DIR, self.image + ".notes.sqlite")
        self.notes = open_ro(notes) if os.path.exists(notes) else None

    def func_row(self, sw: int) -> tuple[Any, ...] | None:
        return self.db.execute(
            "SELECT entry_sw, end_sw, n_insns, block, name, label, entry_kind, is_leaf FROM functions "
            "WHERE entry_sw<=? AND end_sw>? ORDER BY entry_sw DESC LIMIT 1",
            (sw, sw),
        ).fetchone()

    def resolve(self, token: str) -> int:
        addr = parse_addr(token)
        if addr is None:
            row = self.db.execute(
                "SELECT entry_sw FROM functions WHERE name=?", (token,)
            ).fetchone()
            if row is None:
                raise FwError(
                    "no SHARC function named %r (try: search %s)" % (token, token)
                )
            addr = row[0]
        f = self.func_row(addr)
        if f is None:
            raise FwError("0x%x is not inside a known SHARC function" % addr)
        return f[0]

    def note(self, entry: int) -> tuple[str, str] | None:
        if self.notes is None:
            return None
        try:
            row = self.notes.execute(
                "SELECT role, summary FROM notes WHERE function_sw=?", (entry,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return row

    def label(self, entry: int) -> str:
        row = self.db.execute(
            "SELECT name, label FROM functions WHERE entry_sw=?", (entry,)
        ).fetchone()
        n = self.note(entry)
        extra = ""
        if n:
            extra = " {%s}" % n[0]
        elif row and row[1]:
            extra = " (%s)" % row[1]
        return "%06x %s%s" % (entry, row[0] if row else "FUN_%06x" % entry, extra)

    def graph(self) -> Graph:
        if self._graph is None:
            self._graph, self.graph_state = Graph.load(
                "sharc-" + self.image,
                db_key(self.db, self.path),
                self.db.execute(
                    "SELECT DISTINCT from_function, to_function FROM edges WHERE kind='call' "
                    "AND from_function IS NOT NULL AND to_function IS NOT NULL"
                ),
            )
        return self._graph


# --- command implementations ---------------------------------------------------

Image = CF | Sharc


def want_sharc(token: str | None, cpu: str | None) -> bool:
    if cpu:
        return cpu == "sharc"
    if token:
        a = parse_addr(token)
        if a is not None:
            return a < CF_BASE
        if re.match(r"(?i)fun_[0-9a-f]{6}$", token):
            return True
    return False


class Session:
    """Opened databases for one process (batch shares it)."""

    def __init__(self) -> None:
        self.imgs: dict[tuple[str, str], Image] = {}

    def get(
        self, image: str | None, token: str | None = None, cpu: str | None = None
    ) -> Image:
        image = image or os.environ.get("FW_IMAGE")
        if not image:
            raise FwError("no image: pass -i dt2-1.17 or set FW_IMAGE")
        kind = "sharc" if want_sharc(token, cpu) else "cf"
        key = (short_image(image), kind)
        if key not in self.imgs:
            self.imgs[key] = Sharc(image) if kind == "sharc" else CF(image)
        return self.imgs[key]


def cmd_fn(img: Image, token: str, lines_n: int = 12) -> list[str]:
    if isinstance(img, Sharc):
        return sharc_fn(img, token)
    entry = img.resolve(token)
    f = img.func_row(entry)
    assert f is not None
    out = ["%s  size=%d  end=%08x" % (img.label(entry), f[4], f[1])]
    if f[5]:
        out.append("sig: " + clip(f[5]))
    slots = img.vtable_slots(entry)
    if slots:
        out.append("vtable: " + ", ".join("%s#%d" % s for s in slots[:6]))
    callers = img.callers(entry)
    cfuncs = sorted({c[0] for c in callers if c[0] is not None})
    callees = img.callees(entry)
    out.append("callers: %d sites in %d functions" % (len(callers), len(cfuncs)))
    for c in cfuncs[:5]:
        out.append("  <- " + img.label(c))
    if len(cfuncs) > 5:
        out.append("  <- ... %d more" % (len(cfuncs) - 5))
    out.append("callees: %d" % len(callees))
    for c, _k in callees[:6]:
        out.append("  -> " + img.label(c))
    if len(callees) > 6:
        out.append("  -> ... %d more" % (len(callees) - 6))
    strs = img.strings_of(entry)
    if strs:
        out.append("strings:")
        for a, t in strs:
            out.append("  %08x %s" % (a, repr(clip(t, 70))))
    text = img.read_dump(f[6])
    if text:
        body = _decomp_body(text)
        out.append("decomp (%d of %d lines):" % (min(lines_n, len(body)), len(body)))
        out.extend("  " + ln[:110] for ln in body[:lines_n])
    else:
        out.append("decomp: none (%s)" % ("failed" if f[6] is None else "missing file"))
    return out


_DECL = re.compile(r"^\s+[A-Za-z_][\w ]*[ *]\**\w+(\[\w+\])?;$")
_STMT = ("return", "goto", "break", "continue", "else", "case", "default")


def _decomp_body(text: str) -> list[str]:
    """Decompiled C without comments, blank lines and the local declarations."""
    out: list[str] = []
    header = True
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith(("/*", "*", "//")):
            continue
        if header:
            st = ln.strip()
            if st == "{" or (_DECL.match(ln) and not st.startswith(_STMT)):
                continue
            if ln[:1] not in (" ", "\t"):
                out.append(ln)
                continue
            header = False
        out.append(ln)
    return out


def sharc_fn(img: Sharc, token: str) -> list[str]:
    entry = img.resolve(token)
    f = img.func_row(entry)
    assert f is not None
    out = [
        "%s  insns=%d end=%06x block=%s %s%s"
        % (
            img.label(entry),
            f[2],
            f[1],
            f[3],
            "leaf" if f[7] else "non-leaf",
            " " + f[6] if f[6] else "",
        )
    ]
    n = img.note(entry)
    if n:
        out.append("note: %s: %s" % (n[0], clip(n[1] or "", 140)))
    callers = img.db.execute(
        "SELECT DISTINCT from_function FROM edges WHERE to_function=? AND from_function IS NOT NULL "
        "AND from_function<>? AND kind IN ('call','jump','indirect')",
        (entry, entry),
    ).fetchall()
    callees = img.db.execute(
        "SELECT DISTINCT to_function FROM edges WHERE from_function=? AND kind='call' AND to_function IS NOT NULL",
        (entry,),
    ).fetchall()
    out.append("callers: %d" % len(callers))
    out.extend("  <- " + img.label(c[0]) for c in callers[:5])
    out.append("callees: %d" % len(callees))
    out.extend("  -> " + img.label(c[0]) for c in callees[:6])
    lit = img.db.execute(
        "SELECT sw, value, form FROM literals WHERE sw>=? AND sw<? ORDER BY sw LIMIT 6",
        (entry, f[1]),
    ).fetchall()
    if lit:
        out.append("literals: " + ", ".join("%x:%#x" % (a, b) for a, b, _ in lit))
    out.extend(sharc_disasm(img, entry, 8)[:9])
    return out


def sharc_disasm(img: Sharc, entry: int, n: int | None = None) -> list[str]:
    f = img.func_row(entry)
    if f is None:
        raise FwError("no SHARC function at 0x%x" % entry)
    rows = img.db.execute(
        "SELECT sw, mnemonic FROM insn WHERE aligned=1 AND sw>=? AND sw<? ORDER BY sw",
        (entry, f[1]),
    ).fetchall()
    out = ["  %06x  %s" % (sw, m) for sw, m in rows]
    return out[:n] if n else out


def cmd_decomp(img: Image, token: str) -> list[str]:
    if isinstance(img, Sharc):
        raise FwError(
            "no decompiler for SHARC; use: disasm %s (or sharc.py card)" % token
        )
    entry = img.resolve(token)
    f = img.func_row(entry)
    assert f is not None
    text = img.read_dump(f[6])
    if text is None:
        raise FwError(
            "no decompiled C for %s (decompile failed); use disasm" % img.label(entry)
        )
    return [img.label(entry)] + text.splitlines()


def cmd_disasm(img: Image, token: str) -> list[str]:
    if isinstance(img, Sharc):
        entry = img.resolve(token)
        return [img.label(entry)] + sharc_disasm(img, entry)
    entry = img.resolve(token)
    f = img.func_row(entry)
    assert f is not None
    text = img.read_dump(f[7])
    if text is None:
        raise FwError("no disassembly file for %s" % img.label(entry))
    return [img.label(entry)] + [
        ln for ln in text.splitlines() if not ln.startswith(";")
    ][1:]


def cmd_callers(img: Image, token: str) -> list[str]:
    entry = img.resolve(token)
    if isinstance(img, Sharc):
        rows = img.db.execute(
            "SELECT from_function, from_sw, kind FROM edges WHERE (to_sw=? OR to_function=?) "
            "AND kind IN ('call','jump','indirect') AND IFNULL(from_function,0)<>? ORDER BY 1, 2",
            (entry, entry, entry),
        ).fetchall()
    else:
        rows = img.callers(entry)
    out = ["%s  %d call sites" % (img.label(entry), len(rows))]
    last = None
    for fn, site, kind in rows:
        tag = img.label(fn) if fn is not None else "(no function)"
        fmt = "%06x" if isinstance(img, Sharc) else "%08x"
        out.append(
            "  %s %s @%s" % (kind[:4], tag if tag != last else '  "', fmt % site)
        )
        last = tag
    return out


def cmd_callees(img: Image, token: str) -> list[str]:
    entry = img.resolve(token)
    if isinstance(img, Sharc):
        rows = [
            (r[0], "call")
            for r in img.db.execute(
                "SELECT DISTINCT to_function FROM edges WHERE from_function=? AND kind='call' "
                "AND to_function IS NOT NULL ORDER BY 1",
                (entry,),
            )
        ]
    else:
        rows = img.callees(entry)
    out = ["%s  %d callees" % (img.label(entry), len(rows))]
    out.extend("  %s %s" % (k[:4], img.label(c)) for c, k in rows)
    return out


def cmd_xref(img: Image, token: str) -> list[str]:
    addr = parse_addr(token)
    if addr is None:
        addr = img.resolve(token)
    if isinstance(img, Sharc):
        out = ["xref %06x" % addr]
        for fn, site, kind in img.db.execute(
            "SELECT from_function, from_sw, kind FROM edges WHERE to_sw=? AND kind IN ('call','jump','indirect') "
            "ORDER BY from_sw",
            (addr,),
        ):
            out.append(
                "  code %-9s %06x in %s" % (kind, site, img.label(fn) if fn else "?")
            )
        for sw, direction, base in img.db.execute(
            "SELECT sw, direction, base_reg FROM ptr WHERE address=? ORDER BY sw LIMIT 200",
            (addr,),
        ):
            f = img.func_row(sw)
            out.append(
                "  data %-9s %06x in %s (%s)"
                % (direction, sw, img.label(f[0]) if f else "?", base)
            )
        for sw, form in img.db.execute(
            "SELECT sw, form FROM literals WHERE value=? ORDER BY sw LIMIT 100", (addr,)
        ):
            f = img.func_row(sw)
            out.append(
                "  literal %s %06x in %s" % (form, sw, img.label(f[0]) if f else "?")
            )
        if len(out) == 1:
            out.append("  no references recorded (indirect calls are not edges)")
        return out
    out = ["xref %08x  %s" % (addr, img.label_any(addr))]
    s = img.db.execute("SELECT text FROM strings WHERE addr=?", (addr,)).fetchone()
    if s:
        out.append("  string %r" % clip(s[0], 80))
    for site, fn, kind, _label in img.db.execute(
        "SELECT site, func, kind, label FROM datarefs WHERE to_addr=? ORDER BY site",
        (addr,),
    ):
        out.append(
            "  data %-10s %08x in %s"
            % (kind, site, img.label(fn) if fn else "(outside functions)")
        )
    for site, fn, kind in img.db.execute(
        "SELECT site, from_func, kind FROM edges WHERE to_addr=? ORDER BY site", (addr,)
    ):
        out.append(
            "  code %-10s %08x in %s"
            % (kind, site, img.label(fn) if fn else "(outside functions)")
        )
    for cls, slot, vt in img.db.execute(
        "SELECT class, slot, addr FROM vtables WHERE target=? ORDER BY class, slot",
        (addr,),
    ):
        out.append("  vtable %s slot %d (vtable %08x)" % (cls, slot, vt))
    if len(out) == 1:
        out.append(
            "  no references recorded (Ghidra misses code outside functions; try uv run python -m tools.coldfire.refscan)"
        )
    return out


def cmd_str(img: Image, term: str) -> list[str]:
    if isinstance(img, Sharc):
        raise FwError(
            "strings are a ColdFire concept; use -i with a ColdFire address or search"
        )
    rows = _find_strings(img, term, 200)
    out = ["%d strings match %r" % (len(rows), term)]
    for addr, text in rows:
        users = img.db.execute(
            "SELECT DISTINCT func FROM datarefs WHERE to_addr=? AND func IS NOT NULL ORDER BY func",
            (addr,),
        ).fetchall()
        out.append("%08x %s" % (addr, repr(clip(text, 70))))
        for (u,) in users[:3]:
            out.append("    used by " + img.label(u))
        if len(users) > 3:
            out.append("    used by ... %d more" % (len(users) - 3))
        if not users:
            out.append("    no direct users recorded")
    return out


def _find_strings(img: CF, term: str, limit: int) -> list[tuple[int, str]]:
    if len(term) >= 3:
        fts = img.fts()
        ids = fts.execute(
            "SELECT rowid FROM fts_strings WHERE fts_strings MATCH ? ORDER BY rank LIMIT ?",
            (tri_query(term), limit),
        ).fetchall()
        if ids:
            marks = ",".join(str(i[0]) for i in ids)
            addrs = fts.execute(
                "SELECT id, addr FROM string_rows WHERE id IN (%s)" % marks
            ).fetchall()
            out = []
            for _id, a in sorted(addrs, key=lambda r: r[1]):
                row = img.db.execute(
                    "SELECT text FROM strings WHERE addr=? LIMIT 1", (a,)
                ).fetchone()
                if row:
                    out.append((a, row[0]))
            return out
        return []
    return img.db.execute(
        "SELECT addr, text FROM strings WHERE text LIKE ? ORDER BY addr LIMIT ?",
        ("%" + term + "%", limit),
    ).fetchall()


def _bases(img: CF, bases: str | None) -> str:
    if not bases:
        return "-"
    names = []
    for tok in re.findall(r"\d+", bases):
        r = img.db.execute(
            "SELECT name FROM typeinfos WHERE addr=?", (int(tok),)
        ).fetchone()
        names.append(r[0] if r else tok)
    return ", ".join(names) or "-"


def cmd_class(img: Image, name: str) -> list[str]:
    if isinstance(img, Sharc):
        raise FwError("RTTI classes exist on the ColdFire side only")
    rows = img.db.execute(
        "SELECT name, kind, bases, addr FROM typeinfos WHERE name=? ORDER BY addr",
        (name,),
    ).fetchall()
    if not rows:
        rows = img.db.execute(
            "SELECT name, kind, bases, addr FROM typeinfos WHERE name LIKE ? AND kind NOT IN ('pointer','fundamental','function') "
            "AND name NOT LIKE 'std::_Sp%' ORDER BY name",
            ("%" + name + "%",),
        ).fetchall()
        if not rows:
            raise FwError("no class like %r" % name)
        if len(rows) > 1:
            return ["%d classes match %r (use the exact name):" % (len(rows), name)] + [
                "  %s  (%s)" % (r[0], r[1]) for r in rows
            ]
    cname, kind, bases, taddr = rows[0]
    out = ["%s  %s  typeinfo=%08x  bases=%s" % (cname, kind, taddr, _bases(img, bases))]
    derived = img.db.execute(
        "SELECT name FROM typeinfos WHERE bases LIKE ? ORDER BY name",
        ("%" + cname + "%",),
    ).fetchall()
    if derived:
        out.append(
            "derived: "
            + ", ".join(d[0] for d in derived[:12])
            + (" ..." if len(derived) > 12 else "")
        )
    vts = img.db.execute(
        "SELECT addr, offset_to_top, slot, target, owner FROM vtables WHERE class=? ORDER BY addr, slot",
        (cname,),
    ).fetchall()
    if not vts:
        out.append("no vtable")
        return out
    cur = None
    for addr, ott, slot, target, owner in vts:
        if addr != cur:
            out.append("vtable %08x (offset_to_top %d)" % (addr, ott))
            cur = addr
        n = img.names().get(target)
        out.append(
            "  #%-3d %s%s%s"
            % (
                slot,
                img.label(target) if target else "0",
                "" if owner == cname else "  <" + str(owner) + ">",
                "",
            )
        )
        del n
    return out


def cmd_path(img: Image, a: str, b: str) -> list[str]:
    ea, eb = img.resolve(a), img.resolve(b)
    g = img.graph()
    p = g.shortest(ea, eb)
    out = ["path %s -> %s (graph %s)" % (img.label(ea), img.label(eb), img.graph_state)]
    if p is None:
        out.append("  no call path (indirect calls through vtables are not edges)")
        return out
    out.append("  %d hops" % (len(p) - 1))
    out.extend(
        "  %s%s" % ("" if i == 0 else "-> ", img.label(n)) for i, n in enumerate(p)
    )
    return out


def cmd_port(sess: Session, name: str, src: str, dst: str) -> list[str]:
    a = sess.get(src, name)
    b = sess.get(dst, name)
    if isinstance(a, Sharc) and isinstance(b, Sharc):
        entry = a.resolve(name)
        h = a.db.execute(
            "SELECT reloc_hash FROM func_hash WHERE entry_sw=?", (entry,)
        ).fetchone()
        out = ["%s  (%s) -> %s" % (a.label(entry), short_image(src), short_image(dst))]
        if h:
            for (e,) in b.db.execute(
                "SELECT entry_sw FROM func_hash WHERE reloc_hash=?", (h[0],)
            ):
                out.append("  match (reloc hash) " + b.label(e))
        if len(out) == 1:
            out.append("  no match")
        return out
    assert isinstance(a, CF) and isinstance(b, CF)
    entry = a.resolve(name)
    out = ["%s  (%s) -> %s" % (a.label(entry), a.image, b.image)]
    rows = b.db.execute(
        "SELECT dst_entry, method FROM matches WHERE src_image=? AND src_entry=?",
        (a.db_name, entry),
    ).fetchall()
    for dst_entry, method in rows:
        out.append("  %s  [%s]" % (b.label(dst_entry), method))
    if not rows:
        rev = b.db.execute(
            "SELECT src_entry, method FROM matches WHERE src_image=? AND dst_entry=?",
            (a.db_name, entry),
        ).fetchall()
        for e, method in rev:
            out.append("  (reverse) %s [%s]" % (b.label(e), method))
    if len(out) == 1:
        out.append(
            "  no recorded match (uv run python -m tools.coldfire.cfmatch run %s %s writes them)"
            % (a.db_name, b.db_name)
        )
    return out


def cmd_sql(img: Image, query: str) -> list[str]:
    if not re.match(r"(?is)\s*(select|with|pragma|explain)\b", query):
        raise FwError("sql is read-only: SELECT/WITH only")
    cur = img.db.execute(query)
    cols = [d[0] for d in cur.description or []]
    out = ["\t".join(cols)]
    for row in cur:
        out.append("\t".join(_cell(v) for v in row))
    return out


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, int) and v >= 0x100000:
        return "0x%x" % v
    return clip(str(v), 120)


def cmd_search(
    sess: Session, image: str | None, term: str, where: str, cap: int, cpu: str | None
) -> list[str]:
    img = sess.get(image, None, cpu)
    parts = set(where.split(","))
    out: list[str] = []
    per = max(6, cap // 4) if cap else 50
    if isinstance(img, Sharc):
        like = "%" + term + "%"
        rows = img.db.execute(
            "SELECT entry_sw FROM functions WHERE name LIKE ? OR label LIKE ? ORDER BY entry_sw LIMIT ?",
            (like, like, per * 2),
        ).fetchall()
        out.append("functions: %d" % len(rows))
        out.extend("  " + img.label(r[0]) for r in rows)
        if img.notes is not None:
            for fn, role, summ in img.notes.execute(
                "SELECT function_sw, role, summary FROM notes WHERE summary LIKE ? OR role LIKE ? LIMIT ?",
                (like, like, per),
            ):
                out.append("  note %06x %s: %s" % (fn, role, clip(summ or "", 90)))
        return out
    assert isinstance(img, CF)
    fts = img.fts()
    if "names" in parts:
        if len(term) >= 3:
            rows = fts.execute(
                "SELECT rowid, name, cf_name, klass FROM fts_names WHERE fts_names MATCH ? ORDER BY rank LIMIT ?",
                (tri_query(term), per * 2),
            ).fetchall()
        else:
            rows = []
        out.append("names: %d" % len(rows))
        for rowid, name, cfn, _k in rows:
            out.append("  %08x %s%s" % (rowid, name, " [%s]" % cfn if cfn else ""))
    if "strings" in parts:
        rows2 = _find_strings(img, term, per)
        out.append("strings: %d" % len(rows2))
        for a, t in rows2:
            users = img.db.execute(
                "SELECT DISTINCT func FROM datarefs WHERE to_addr=? AND func IS NOT NULL LIMIT 2",
                (a,),
            ).fetchall()
            out.append(
                "  %08x %s%s"
                % (
                    a,
                    repr(clip(t, 60)),
                    "  <- " + ", ".join("%08x" % u[0] for u in users) if users else "",
                )
            )
    if "decomp" in parts:
        hits = fts.execute(
            "SELECT rowid FROM fts_decomp WHERE fts_decomp MATCH ? ORDER BY rank LIMIT ?",
            (fts_query(term, prefix=True), per),
        ).fetchall()
        out.append(
            "decomp: %d functions (best %d)" % (_count_decomp(fts, term), len(hits))
        )
        words = [w.lower() for w in term.split()]
        for (rowid,) in hits:
            f = img.func_row(rowid)
            line = ""
            if f:
                text = img.read_dump(f[6]) or ""
                for ln in text.splitlines():
                    if any(w in ln.lower() for w in words):
                        line = ln
                        break
            out.append("  %s : %s" % (img.label(rowid), clip(line, 70)))
    if "atlas" in parts and os.path.exists(
        os.path.join(ATLAS_DIR, img.image + ".sqlite")
    ):
        out.append("atlas:")
        out.extend("  " + ln for ln in cmd_atlas(img.image, term, 8))
    return out


def _count_decomp(fts: sqlite3.Connection, term: str) -> int:
    return fts.execute(
        "SELECT count(*) FROM fts_decomp WHERE fts_decomp MATCH ?",
        (fts_query(term, prefix=True),),
    ).fetchone()[0]


def cmd_atlas(image: str, term: str, per: int = 15) -> list[str]:
    out = _atlas_one(image, term, per)
    words = term.split()
    if len(words) > 1 and out and out[0].startswith("atlas has nothing"):
        out = []
        for w in words:
            out.append("-- %s" % w)
            out.extend(_atlas_one(image, w, max(4, per // len(words))))
    return out


def _atlas_one(image: str, term: str, per: int) -> list[str]:
    path = os.path.join(ATLAS_DIR, short_image(image) + ".sqlite")
    if not os.path.exists(path):
        raise FwError(
            "no atlas for %s: run uv run python -m tools.coldfire.atlas build %s"
            % (short_image(image), short_image(image))
        )
    con = open_ro(path)
    like = "%" + term + "%"
    out: list[str] = []
    g = con.execute(
        "SELECT term, kind, ref, detail, confidence FROM glossary WHERE term LIKE ? ORDER BY term, instr('function class struct descriptor param string manual', kind), score DESC",
        (like,),
    ).fetchall()
    if g:
        out.append("glossary %r: %d rows" % (term, len(g)))
        kinds: dict[str, int] = {}
        for _t, kind, ref, detail, conf in g:
            kinds[kind] = kinds.get(kind, 0) + 1
            if kinds[kind] <= per // 3 + 2:
                out.append(
                    "  %-9s %s  %s (%s)" % (kind, ref, clip(detail or "", 70), conf)
                )
    for table, sql, fmt in (
        (
            "descriptors",
            "SELECT idx, role, name1, name2, addr FROM descriptors WHERE name1 LIKE ?1 OR name2 LIKE ?1 OR role LIKE ?1 LIMIT ?2",
            "  descriptor #%s %s %r/%r @%#x",
        ),
        (
            "params",
            "SELECT code, long_name, short_name, owner_name, addr FROM params WHERE long_name LIKE ?1 OR short_name LIKE ?1 OR owner_name LIKE ?1 LIMIT ?2",
            "  param %s %r %r owner=%s @%#x",
        ),
        (
            "classes",
            "SELECT name, role, vtable, nslots FROM classes WHERE name LIKE ?1 OR role LIKE ?1 ORDER BY name LIMIT ?2",
            "  class %s role=%s vtable=%#x slots=%s",
        ),
        (
            "ui_strings",
            "SELECT text, view_class, user_func, confidence FROM ui_strings WHERE text LIKE ?1 OR view_class LIKE ?1 LIMIT ?2",
            "  ui %r in %s fn=%#x (%s)",
        ),
    ):
        try:
            rows = con.execute(sql, (like, per)).fetchall()
        except sqlite3.Error:
            continue
        if rows:
            out.append("%s: %d shown" % (table, len(rows)))
            for r in rows:
                try:
                    out.append(fmt % tuple(r))
                except (TypeError, ValueError):
                    out.append("  " + " ".join(str(x) for x in r))
    if not out:
        out.append("atlas has nothing for %r" % term)
    return out


def cmd_catalog(
    image: str,
    term: str,
    tag: str | None,
    top: int,
    grade: bool,
    group: int | None = None,
    groups: bool = False,
    cpu: str = "cf",
) -> list[str]:
    """Rows of the atlas `catalog` table (candidate anchor functions), best score first.

    With cpu="sharc" the rows come from out/atlas/<image>-dsp.sqlite (tools/sharc_dev/sharc_catalog.py).
    """
    short = short_image(image)
    if cpu == "sharc":
        path = os.path.join(ATLAS_DIR, short + "-dsp.sqlite")
        build = "uv run python -m tools.sharc_dev.sharc_catalog %s OTHER" % short
        if not os.path.exists(path):
            raise FwError(
                "no DSP catalog for %s: run %s (e.g. dt2-1.17 dn2-1.12)"
                % (short, build)
            )
    else:
        path = os.path.join(ATLAS_DIR, short + ".sqlite")
        build = "uv run python -m tools.coldfire.atlas catalog %s OTHER" % short
        if not os.path.exists(path):
            raise FwError(
                "no atlas for %s: run uv run python -m tools.coldfire.atlas build %s"
                % (short, short)
            )
    con = open_ro(path)
    if not _has(con, "catalog"):
        raise FwError(
            "no catalog for %s: run %s (e.g. dt2-1.17 dn2-1.12)" % (short, build)
        )
    if (groups or group is not None) and not _has(con, "catalog_groups"):
        raise FwError(
            "no catalog groups for %s: re-run %s (e.g. dt2-1.17 dn2-1.12)"
            % (short, build)
        )
    if groups:
        grows = con.execute(
            "SELECT id, size, anchored, grade, top_classes, top_strings, top_tags, best "
            "FROM catalog_groups ORDER BY size DESC, id LIMIT %d" % top
        ).fetchall()
        out = ["catalog groups %s: %d shown" % (short, len(grows))]
        for gid, size, anc, gr, tcl, tst, ttg, best in grows:
            out.append(
                "grp=%d size=%d anchored=%d grade=%d best=%08x classes=%s tags=%s%s"
                % (
                    gid,
                    size,
                    anc,
                    gr,
                    best,
                    tcl or "-",
                    ttg or "-",
                    " strings=" + repr(tst) if tst else "",
                )
            )
        return out
    where = ["1"]
    args: list[Any] = []
    if group is not None:
        where.append('"group"=?')
        args.append(group)
    if term:
        cls_col, hint_col = ("role", "hints") if cpu == "sharc" else ("class", "hint")
        where.append(
            "(name LIKE ?%d OR %s LIKE ?%d OR %s LIKE ?%d OR tags LIKE ?%d)"
            % (
                len(args) + 1,
                cls_col,
                len(args) + 1,
                hint_col,
                len(args) + 1,
                len(args) + 1,
            )
        )
        args.append("%" + term + "%")
    if tag:
        where.append("(',' || tags) LIKE ?%d" % (len(args) + 1))
        args.append("%," + tag + "%")
    if grade:
        where.append("anchor_grade=1")
    sql = "FROM catalog WHERE " + " AND ".join(where)
    total = con.execute("SELECT count(*) " + sql, args).fetchone()[0]
    if cpu == "sharc":
        drows = con.execute(
            "SELECT addr, score, anchor_grade, name, role, tags, callers, hints, pair_addr, "
            'pair_method, rule_kind, rule_ordinal, anchors, "group" '
            + sql
            + " ORDER BY score DESC, addr LIMIT %d" % top,
            args,
        ).fetchall()
        out = [
            "catalog %s (sharc): %d rows match, %d shown" % (short, total, len(drows))
        ]
        for a, sc, g, name, role, tags, nc, hints, pa, pm, rk, ro, anch, grp in drows:
            rule = rk if ro is None else "%s#%d" % (rk, ro)
            out.append(
                "%06x %5.2f %s %s (%s) grp=%d  %s  callers=%d rule=%s pair=%s%s%s"
                % (
                    a,
                    sc,
                    "G" if g else "-",
                    name,
                    role,
                    grp,
                    tags,
                    nc,
                    rule,
                    "%06x/%s" % (pa, pm) if pa is not None else "-",
                    " " + repr(hints) if hints else "",
                    " anchored=" + anch if anch else "",
                )
            )
        return out
    rows = con.execute(
        "SELECT addr, score, anchor_grade, name, class, tags, ui_callers, hint, pair_addr, "
        'pair_method, rule_kind, rule_ordinal, anchors, work_name, "group", caller_classes '
        + sql
        + " ORDER BY score DESC, addr LIMIT %d" % top,
        args,
    ).fetchall()
    out = ["catalog %s: %d rows match, %d shown" % (short, total, len(rows))]
    for a, sc, g, name, cls, tags, uic, hint, pa, pm, rk, ro, anch, wn, grp, by in rows:
        rule = rk if ro is None else "%s#%d" % (rk, ro)
        out.append(
            "%08x %5.2f %s %s%s%s grp=%d  %s  ui=%d rule=%s pair=%s%s%s%s"
            % (
                a,
                sc,
                "G" if g else "-",
                name,
                " [%s]" % wn if wn else "",
                " (%s)" % cls if cls else "",
                grp,
                tags,
                uic,
                rule,
                "%08x/%s" % (pa, pm) if pa is not None else "-",
                " " + repr(hint) if hint else "",
                " anchored=" + anch if anch else "",
                " by=" + by if by and not cls else "",
            )
        )
    return out


# --- argument parsing and dispatch -----------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fw.py", description=__doc__.splitlines()[0], add_help=True
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument(
            "-i", "--image", help="e.g. dt2-1.17, dn2-1.12 (default $FW_IMAGE)"
        )
        sp.add_argument(
            "--cpu",
            choices=("cf", "sharc"),
            help="override the address-based CPU choice",
        )
        sp.add_argument(
            "--max",
            type=int,
            default=None,
            help="line cap (default %d, 0 = none)" % DEFAULT_MAX,
        )
        return sp

    for name, helptext in (
        ("fn", "function summary"),
        ("decomp", "decompiled C"),
        ("disasm", "disassembly"),
        ("callers", "call sites into a function"),
        ("callees", "functions a function calls"),
        ("xref", "everything that references an address"),
    ):
        sp = common(sub.add_parser(name, help=helptext))
        sp.add_argument("target", help="address (hex), Ghidra name or cf_names name")
    sp = common(sub.add_parser("str", help="strings containing TEXT and who uses them"))
    sp.add_argument("text", nargs="+")
    sp = common(sub.add_parser("class", help="RTTI class: bases, derived, vtable"))
    sp.add_argument("name")
    sp = common(sub.add_parser("path", help="shortest call path A -> B"))
    sp.add_argument("a")
    sp.add_argument("b")
    sp = common(sub.add_parser("port", help="carry a function between versions"))
    sp.add_argument("name")
    sp.add_argument("--from", dest="src", required=True)
    sp.add_argument("--to", dest="dst", required=True)
    sp = common(sub.add_parser("sql", help="read-only SQL"))
    sp.add_argument("query")
    sp = common(
        sub.add_parser(
            "search", help="full-text search over names, strings, decompiled C, atlas"
        )
    )
    sp.add_argument("term", nargs="+")
    sp.add_argument("--in", dest="where", default="names,strings,decomp,atlas")
    sp = common(
        sub.add_parser("atlas", help="semantic atlas lookup (tools/coldfire/atlas.py)")
    )
    sp.add_argument("term", nargs="+")
    sp = common(
        sub.add_parser(
            "catalog", help="candidate anchor functions (atlas.py catalog), best first"
        )
    )
    sp.add_argument("term", nargs="*", help="substring of name, class, hint or tags")
    sp.add_argument("--tag", help="tag prefix, e.g. near:cf.menu, role:model, term:fx")
    sp.add_argument("--top", type=int, default=30)
    sp.add_argument("--grade", action="store_true", help="only anchor_grade=1")
    sp.add_argument("--group", type=int, help="only rows of this group id")
    sp.add_argument(
        "--groups", action="store_true", help="list the groups (top N by size)"
    )
    sp = common(sub.add_parser("batch", help="run many queries from FILE or - (stdin)"))
    sp.add_argument("file")
    return p


def run(
    sess: Session, args: argparse.Namespace, default_image: str | None = None
) -> str:
    cap = DEFAULT_MAX if args.max is None else args.max
    image = args.image or default_image
    c = args.cmd
    if c == "batch":
        raise FwError("batch cannot be nested")
    if c == "sql":
        return capped(cmd_sql(sess.get(image, None, args.cpu), args.query), cap)
    if c == "search":
        return capped(
            cmd_search(sess, image, " ".join(args.term), args.where, cap, args.cpu), cap
        )
    if c == "atlas":
        image = image or os.environ.get("FW_IMAGE")
        if not image:
            raise FwError("no image: pass -i dt2-1.17 or set FW_IMAGE")
        return capped(cmd_atlas(image, " ".join(args.term)), cap)
    if c == "catalog":
        image = image or os.environ.get("FW_IMAGE")
        if not image:
            raise FwError("no image: pass -i dt2-1.17 or set FW_IMAGE")
        return capped(
            cmd_catalog(
                image,
                " ".join(args.term),
                args.tag,
                args.top,
                args.grade,
                args.group,
                args.groups,
                args.cpu or "cf",
            ),
            cap,
        )
    if c == "port":
        return capped(cmd_port(sess, args.name, args.src, args.dst), cap)
    if c == "str":
        return capped(cmd_str(sess.get(image, None, "cf"), " ".join(args.text)), cap)
    if c == "class":
        return capped(cmd_class(sess.get(image, None, "cf"), args.name), cap)
    if c == "path":
        return capped(cmd_path(sess.get(image, args.a, args.cpu), args.a, args.b), cap)
    img = sess.get(image, args.target, args.cpu)
    fn = {
        "fn": cmd_fn,
        "decomp": cmd_decomp,
        "disasm": cmd_disasm,
        "callers": cmd_callers,
        "callees": cmd_callees,
        "xref": cmd_xref,
    }[c]
    return capped(fn(img, args.target), cap)


def run_line(
    sess: Session, argv: list[str], default_image: str | None, default_max: int | None
) -> str:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return "error: bad arguments: " + " ".join(argv)
    if args.max is None:
        args.max = default_max
    try:
        return run(sess, args, default_image)
    except FwError as e:
        return "error: %s" % e
    except sqlite3.Error as e:
        return "sql error: %s" % e


def cmd_batch(sess: Session, args: argparse.Namespace) -> str:
    if args.file == "-":
        text = sys.stdin.read()
    else:
        with open(args.file) as fh:
            text = fh.read()
    blocks = []
    cap = args.max if args.max is not None else 40
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            argv = shlex.split(line)
        except ValueError as e:
            blocks.append("## %s\nerror: %s" % (line, e))
            continue
        if argv and argv[0] in ("fw.py", "fw"):
            argv = argv[1:]
        blocks.append("## %s\n%s" % (line, run_line(sess, argv, args.image, cap)))
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sess = Session()
    try:
        if args.cmd == "batch":
            print(cmd_batch(sess, args))
        else:
            print(run(sess, args))
    except FwError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print("sql error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
