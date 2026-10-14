#!/usr/bin/env python3
"""Apply a file move map to the repository.

Reads a JSON map of moves (old path to new path) and rewrites what the moves
break: Python imports, sys.path setup, __file__ anchors and path text in docs,
scripts and configs. What it cannot change safely it reports instead of
editing. It uses only the standard library and imports nothing from the
repository.

Usage:
    uv run python tools/devtools/relocate.py MAP.json [--apply]
        [--report REPORT.json] [--root DIR]

MAP.json has "moves" (old to new path), "packages" (directory to the docstring
of its new __init__.py) and "exclude_text" (path prefixes the text rewrites
skip). Without --apply nothing changes: every edit and report is computed in
memory, written to the report file and summarised. With --apply, and only when
every check passes, the files are moved with git mv, the edited contents are
written to the new paths and the package files are created.

Exit status: 0 on success, 1 when a check fails (nothing is changed), 2 on
usage errors.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import fnmatch
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

SELF = "tools/devtools/relocate.py"
SUMMARY_LIMIT = 10

CALL_SITES = {
    "importlib.import_module",
    "import_module",
    "__import__",
    "mock.patch",
    "unittest.mock.patch",
    "patch",
    "monkeypatch.setattr",
    "monkeypatch.delattr",
    "sys.modules.get",
    "sys.modules.pop",
    "sys.modules.setdefault",
}
SYS_PATH_CALLS = {
    f"sys.path.{name}"
    for name in ("insert", "append", "remove", "pop", "extend", "clear")
}
PATH_ADDS = {"sys.path.insert", "sys.path.append"}
PATH_POPS = {"sys.path.pop", "sys.path.remove"}
MARKERS = (
    "__file__",
    "ROOT",
    "REPO",
    "HERE",
    "_here",
    "TOOLS",
    "tools",
    "SPEC_DIR",
    "_TESTS_DIR",
    "_TOOLS_DIR",
    "dirname",
    "parent",
)
OS_BASES = ("os.path.abspath", "os.path.realpath")
RESOLVERS = ("resolve", "absolute")
EXTENSIONS = {
    "py",
    "json",
    "toml",
    "sql",
    "md",
    "sh",
    "mjs",
    "rs",
    "txt",
    "bin",
    "syx",
    "wav",
}
CORE_FILES = (
    "tools/sharc_transpile.py",
    "tools/sharc_transpile_infer.py",
    "tools/sharc_transpile_run.py",
    "tools/sharc_rsgen.py",
    "tools/sharc_rsvec.py",
    "tools/sharc_lowergen*.py",
)
KNOWN_ROOTS = {"REPO_ROOT": [], "TOOLS": ["tools"]}

T1_RE = re.compile(
    rb"(?P<py>\bpython3?)(?P<flags>(?:[ \t]+-[A-Za-z]\S*)*)[ \t]+"
    rb"(?P<rel>(?:\.{1,2}/)*)tools/(?P<path>[\w./-]+?\.py)\b"
)
T2_RE = re.compile(rb"(?<![\w.-])(?P<pre>(?:\.\./)*)tools/(?P<rest>[\w.*/-]+)")
T3_RE = re.compile(rb"\btools\.(?P<name>[A-Za-z_]\w*)\b")
BARE_RE = re.compile(rb"(?<![\w.-])tools/(?![\w.*/-])")
DOTTED_RE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+")

Dirs = tuple[list[str], list[str]]  # (old, new) path components


@dataclass
class Edit:
    start: int
    end: int
    text: bytes
    kind: str
    line: int
    old: str
    new: str
    prio: int = 0  # 0 structural, 1 anchor, 2 text; higher yields on overlap


@dataclass
class Source:
    old: str
    new: str
    data: bytes
    starts: list[int] = field(init=False)
    edits: list[Edit] = field(default_factory=list)
    out: bytes = b""

    def __post_init__(self) -> None:
        self.starts = [0, *(m.end() for m in re.finditer(rb"\n", self.data))]

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def add(
        self,
        start: int,
        end: int,
        text: str,
        kind: str,
        prio: int = 0,
        label: tuple[str, str] | None = None,
    ) -> None:
        old = self.data[start:end].decode("utf-8", "replace")
        shown_old, shown_new = label or (old, text)
        self.edits.append(
            Edit(
                start,
                end,
                text.encode(),
                kind,
                self.line_of(start),
                clip(shown_old),
                clip(shown_new),
                prio,
            )
        )

    def apply(self) -> bytes:
        out = self.data
        for edit in sorted(self.edits, key=lambda e: (e.start, e.end), reverse=True):
            start, end = edit.start, edit.end
            out = out[:start] + edit.text + out[end:]
        return out


@dataclass(frozen=True)
class Resolved:
    n: int  # leading components of the written name that name the module
    new: str  # new module name
    stdlib: bool  # matched a flat name that is also a standard library module


@dataclass
class Anchor:
    ups: int
    members: list[ast.AST]
    style: str  # "path" or "os"


@dataclass
class Val:
    """A directory expression: old and new path components after the move."""

    old: list[str]
    new: list[str]
    ups: int | None  # up-count from the file; None for an absolute root
    tail: int  # literal components appended after the anchored directory


def clip(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def mod_parts(path: str) -> list[str]:
    """Module name components of a .py path."""
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def find_units(moves: dict[str, str], tracked: set[str]) -> dict[str, str]:
    """Directories under tools/ that move as a whole to one new directory."""
    dirs: set[str] = set()
    for old in moves:
        parts = old.split("/")
        if parts[0] == "tools":
            dirs.update("/".join(parts[:i]) for i in range(2, len(parts)))
    files = sorted(tracked)
    units: dict[str, str] = {}
    for d in sorted(dirs):
        skip = len(d) + 1
        inside = [f for f in files if f.startswith(d + "/")]
        targets = {new_dir(moves.get(f), f[skip:]) for f in inside}
        target = targets.pop()
        if not targets and target is not None:
            units[d] = target
    return units


def new_dir(new: str | None, rel: str) -> str | None:
    if new is None or not new.endswith("/" + rel):
        return None
    cut = len(new) - len(rel) - 1
    return new[:cut]


class Ctx:
    """Tables derived from the move map, and the collected reports."""

    def __init__(self, moves: dict[str, str], tracked: set[str]) -> None:
        self.moves = moves
        self.tracked = tracked
        self.reports: list[tuple[str, int, str, str]] = []
        self.stale_docs = 0
        self.py_moves = {o: n for o, n in moves.items() if o.endswith(".py")}
        self.units = find_units(moves, tracked)
        self.unit_order = sorted(self.units, key=len, reverse=True)
        self.moved_sorted = sorted(moves)
        self.modfiles: dict[str, str] = {}
        self.flat_top: set[str] = set()
        self.subs: dict[str, set[str]] = {}
        self.stems: set[str] = set()
        for old, new in self.py_moves.items():
            self.index_module(old, new)
        self.modmap = dict(self.modfiles)
        for old, new in self.units.items():
            key = old.replace("/", ".")
            names = key.split(".")
            if key not in self.modmap and all(p.isidentifier() for p in names):
                self.modmap[key] = new.replace("/", ".")
        self.toplevel = {k.split(".")[1] for k in self.modmap if k.count(".") == 1}
        self.oldnames = self.flat_top | {"tools"}
        if "tools.sharc_core" in self.modfiles:
            self.oldnames.add("sharc_core")
        self.basenames = {o.rsplit("/", 1)[-1] for o in moves}
        self.basenames |= {d.rsplit("/", 1)[-1] for d in self.units}
        self.old_py_tools = sorted(o for o in self.py_moves if o.startswith("tools/"))

    def index_module(self, old: str, new: str) -> None:
        parts = old.split("/")
        stem = parts[-1].removesuffix(".py")
        if stem != "__init__":
            self.stems.add(stem)
        if parts[0] != "tools":
            return
        self.modfiles[".".join(mod_parts(old))] = ".".join(mod_parts(new))
        if stem == "__init__":
            return
        if len(parts) == 2:
            self.flat_top.add(stem)
        elif len(parts) == 3:
            self.subs.setdefault(stem, set()).add(parts[1])

    def unit_of(self, path: str) -> str:
        for unit in self.unit_order:
            if path == unit or path.startswith(unit + "/"):
                return unit
        return ""

    def moved_to(self, path: str) -> str | None:
        if path in self.moves:
            return self.moves[path]
        unit = self.unit_of(path)
        if not unit:
            return None
        cut = len(unit)
        return self.units[unit] + path[cut:]

    def rewrite_path(self, rest: str) -> tuple[str, str]:
        """New path for tools/REST, or ("", category) when it cannot be edited."""
        old = "tools/" + rest
        if old in self.moves:
            return self.moves[old], ""
        unit = self.unit_of(old)
        if unit:
            cut = len(unit)
            return self.units[unit] + old[cut:], ""
        if "*" in rest:
            hits = [f for f in self.moved_sorted if fnmatch.fnmatchcase(f, old)]
            if hits:
                return self.rewrite_glob(old, hits)
        if any(f.startswith(old) for f in self.moved_sorted):
            return "", "prefix-ref"
        if any(f.startswith(old) for f in self.tracked):
            return "", ""
        return "", "stale-ref"

    def rewrite_glob(self, pattern: str, hits: list[str]) -> tuple[str, str]:
        news = [self.moves[f] for f in hits]
        dirs = {n.rpartition("/")[0] for n in news}
        old_names = [f.rpartition("/")[2] for f in hits]
        same_base = old_names == [n.rpartition("/")[2] for n in news]
        if len(dirs) != 1 or not same_base:
            return "", "glob-ambiguous"
        return dirs.pop() + "/" + pattern.rpartition("/")[2], ""


def dotted(node: ast.AST) -> str:
    """Dotted name of a Name/Attribute chain, or an empty string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def str_const(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def call_arg(node: ast.AST, *names: str) -> ast.expr | None:
    """The only argument of a call to one of the dotted names, else None."""
    if not isinstance(node, ast.Call) or node.keywords or len(node.args) != 1:
        return None
    return node.args[0] if dotted(node.func) in names else None


def is_file_name(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Name) and node.id == "__file__"


def parents_step(node: ast.AST) -> tuple[int, ast.expr] | None:
    """For X.parents[k] return (k, X)."""
    if not isinstance(node, ast.Subscript):
        return None
    attr, index = node.value, node.slice
    if not (isinstance(attr, ast.Attribute) and attr.attr == "parents"):
        return None
    if not (isinstance(index, ast.Constant) and type(index.value) is int):
        return None
    return index.value, attr.value


def strip_resolve(node: ast.AST) -> ast.AST:
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Attribute) and func.attr in RESOLVERS and not node.args:
        return func.value
    return node


def parse_os_anchor(node: ast.AST) -> Anchor | None:
    members: list[ast.AST] = []
    ups = 0
    cur = node
    while (arg := call_arg(cur, "os.path.dirname")) is not None:
        members.append(cur)
        ups += 1
        cur = arg
    base = call_arg(cur, *OS_BASES)
    if is_file_name(cur) or (base is not None and is_file_name(base)):
        members.append(cur)
        return Anchor(ups, members, "os")
    return None


def parse_path_anchor(node: ast.AST) -> Anchor | None:
    members: list[ast.AST] = []
    ups = 0
    cur = node
    while True:
        if isinstance(cur, ast.Attribute) and cur.attr == "parent":
            members.append(cur)
            ups += 1
            cur = cur.value
            continue
        step = parents_step(cur)
        if step is None:
            break
        assert isinstance(cur, ast.Subscript)
        members.extend([cur, cur.value])
        ups += step[0] + 1
        cur = step[1]
    stripped = strip_resolve(cur)
    if stripped is not cur:
        members.append(cur)
        cur = stripped
    arg = call_arg(cur, "Path", "pathlib.Path")
    if arg is None or not is_file_name(arg):
        return None
    members.append(cur)
    return Anchor(ups, members, "path")


def parse_anchor(node: ast.AST) -> Anchor | None:
    return parse_os_anchor(node) or parse_path_anchor(node)


def parse_chain(node: ast.AST) -> tuple[int, list[ast.AST], ast.AST]:
    """Peel .parent, .parents[k] and os.path.dirname layers: (count, members, base)."""
    members: list[ast.AST] = []
    count = 0
    cur = node
    while True:
        if isinstance(cur, ast.Attribute) and cur.attr == "parent":
            members.append(cur)
            count += 1
            cur = cur.value
            continue
        step = parents_step(cur)
        if step is not None and step[0] >= 0:
            assert isinstance(cur, ast.Subscript)
            members.extend([cur, cur.value])
            count += step[0] + 1
            cur = step[1]
            continue
        arg = call_arg(cur, "os.path.dirname")
        if arg is None:
            return count, members, cur
        members.append(cur)
        count += 1
        cur = arg


def split_join(expr: ast.AST) -> tuple[ast.expr, list[ast.expr]] | None:
    """(base, literals) of A / "l" / ..., os.path.join(A, "l", ...), A.joinpath("l")."""
    if is_div(expr):
        consts: list[ast.expr] = []
        cur = expr
        while isinstance(cur, ast.BinOp) and is_div(cur) and str_const(cur.right):
            consts.append(cur.right)
            cur = cur.left
        if not consts:
            return None
        assert isinstance(cur, ast.expr)
        return cur, consts[::-1]
    if not isinstance(expr, ast.Call) or expr.keywords:
        return None
    args = expr.args
    if dotted(expr.func) == "os.path.join":
        if len(args) >= 2 and all(str_const(a) for a in args[1:]):
            return args[0], args[1:]
        return None
    func = expr.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "joinpath"
        and args
        and all(str_const(a) for a in args)
    ):
        return func.value, list(args)
    return None


def path_parts(consts: list[ast.expr]) -> list[str] | None:
    """Plain path components of join literals, or None for absolute or '..'."""
    parts: list[str] = []
    for const in consts:
        assert isinstance(const, ast.Constant)
        pieces = const.value.split("/")
        if const.value.startswith("/") or ".." in pieces:
            return None
        parts.extend(p for p in pieces if p not in ("", "."))
    return parts


def is_sys_path(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript):
        node = node.value
    return dotted(node) == "sys.path"


def is_syspath_stmt(stmt: ast.AST) -> bool:
    if isinstance(stmt, ast.Expr):
        call = stmt.value
        return isinstance(call, ast.Call) and dotted(call.func) in SYS_PATH_CALLS
    if isinstance(stmt, ast.Assign | ast.Delete):
        return any(is_sys_path(t) for t in stmt.targets)
    if isinstance(stmt, ast.AugAssign | ast.AnnAssign):
        return is_sys_path(stmt.target)
    return False


def syspath_call(stmt: ast.AST) -> str:
    """Dotted name of the call in an expression statement, else an empty string."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return dotted(stmt.value.func)
    return ""


def is_block(value: object) -> bool:
    """True for a non-empty list of statements (a body, orelse or finalbody)."""
    return isinstance(value, list) and bool(value) and isinstance(value[0], ast.stmt)


def is_div(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)


def site_target(node: ast.AST) -> ast.expr | None:
    """The expression where a module-name string literal is rewritten."""
    if isinstance(node, ast.Call) and node.args:
        return node.args[0] if dotted(node.func) in CALL_SITES else None
    if isinstance(node, ast.Subscript):
        return node.slice if dotted(node.value) == "sys.modules" else None
    if isinstance(node, ast.Compare):
        in_op = isinstance(node.ops[0], ast.In | ast.NotIn)
        modules = dotted(node.comparators[0]) == "sys.modules"
        return node.left if in_op and modules else None
    return None


class PyFile:
    """Analysis and edits of one Python file, from its old content."""

    def __init__(self, ctx: Ctx, src: Source, tree: ast.Module) -> None:
        self.ctx = ctx
        self.src = src
        self.tree = tree
        self.nodes = list(ast.walk(tree))
        self.moved = src.new != src.old
        self.depth = src.old.count("/")
        self.handled: set[int] = set()
        self.covered: set[int] = set()
        self.claimed: set[int] = set()
        self.joined: set[int] = set()
        self.memo: dict[int, bool] = {}
        self.cands = [n for n in self.nodes if is_syspath_stmt(n)]
        self.syspath_text = "\n".join(self.seg(c) for c in self.cands)
        self.adds: set[int] | None = None
        self.skip: dict[int, set[str]] = {}
        self.pending: list[tuple[int, int, int]] = []
        self.strings: list[tuple[int, int]] | None = None
        self.module = id(tree)
        self.scope_of: dict[int, int] = {id(tree): id(tree)}
        self.scope_parent: dict[int, int | None] = {id(tree): None}
        stack: list[tuple[ast.AST, int]] = [(tree, id(tree))]
        while stack:
            parent, scope = stack.pop()
            for child in ast.iter_child_nodes(parent):
                self.scope_of[id(child)] = scope
                inner = scope
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    inner = id(child)
                    self.scope_parent[inner] = scope
                stack.append((child, inner))
        self.stores: Counter[tuple[int, str]] = Counter()
        for node in self.nodes:
            scope = self.scope_of[id(node)]
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.stores[scope, node.id] += 1
            elif isinstance(node, ast.arg):
                self.stores[scope, node.arg] += 1
        self.assigned: dict[tuple[int, str], ast.expr] = {}
        for node in self.nodes:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    key = (self.scope_of[id(node)], target.id)
                    if self.stores[key] == 1:
                        self.assigned[key] = node.value
        self.known: dict[str, Dirs] = {}
        for node in self.nodes:
            if isinstance(node, ast.ImportFrom) and node.module == "support.paths":
                for alias in node.names:
                    parts = KNOWN_ROOTS.get(alias.name)
                    if parts is not None and not alias.asname:
                        self.known[alias.name] = (list(parts), list(parts))

    def run(self) -> None:
        for node in self.nodes:
            if isinstance(node, ast.Import):
                self.do_import(node)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.do_from(node)
        for node in self.nodes:
            target = site_target(node)
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                self.rewrite_string(target)
        self.handle_syspath()
        self.handle_anchors()
        self.drop_unused()
        self.handle_joins()
        self.handle_literals()

    # source helpers

    def pos(self, line: int, col: int) -> int:
        return self.src.starts[line - 1] + col

    def span(self, node: ast.expr | ast.stmt) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (
            self.pos(node.lineno, node.col_offset),
            self.pos(node.end_lineno, node.end_col_offset),
        )

    def seg(self, node: ast.AST) -> str:
        assert isinstance(node, ast.expr | ast.stmt)
        start, end = self.span(node)
        return self.src.data[start:end].decode("utf-8", "replace")

    def report(self, line: int, category: str, text: str) -> None:
        self.ctx.reports.append((self.src.new, line, category, clip(text)))

    def line_text(self, line: int) -> str:
        start, end = self.src.starts[line - 1], self.src.starts[line]
        return self.src.data[start:end].decode("utf-8", "replace")

    def shares_line(self, node: ast.stmt) -> bool:
        """True when other code is on the first or last line of the statement."""
        start, end = self.span(node)
        first = self.src.starts[node.lineno - 1]
        if self.src.data[first:start].strip():
            return True
        stop = self.src.data.find(b"\n", end)
        if stop < 0:
            stop = len(self.src.data)
        rest = self.src.data[end:stop].strip()
        return bool(rest) and not rest.startswith(b"#")

    def indent(self, node: ast.stmt) -> str:
        start, _ = self.span(node)
        first = self.src.starts[node.lineno - 1]
        return self.src.data[first:start].decode("utf-8", "replace")

    # imports

    def resolve(self, comps: list[str]) -> Resolved | None:
        ctx = self.ctx
        x = comps[0]
        n = 1
        rule2 = False
        if x == "tools":
            if len(comps) < 2 or f"tools.{comps[1]}" not in ctx.modmap:
                return None
            cur, n = f"tools.{comps[1]}", 2
        elif x == "sharc_core":
            if "tools.sharc_core" not in ctx.modmap:
                return None
            cur = "tools.sharc_core"
        elif x in ctx.flat_top:
            cur, rule2 = f"tools.{x}", True
        else:
            cur = self.sibling(x)
            if not cur:
                return None
        while n < len(comps) and f"{cur}.{comps[n]}" in ctx.modmap:
            cur += "." + comps[n]
            n += 1
        return Resolved(n, ctx.modmap[cur], rule2 and x in sys.stdlib_module_names)

    def sibling(self, x: str) -> str:
        """Flat name X as a sibling module (rule 3) or via sys.path text (rule 4)."""
        subs = self.ctx.subs.get(x, set())
        parts = self.src.old.split("/")
        if len(parts) >= 3 and parts[0] == "tools" and parts[1] in subs:
            return f"tools.{parts[1]}.{x}"
        for sub in sorted(subs):
            if sub in self.syspath_text:
                return f"tools.{sub}.{x}"
        return ""

    def note_unresolved(self, name: str, line: int) -> None:
        if name in self.ctx.stems:
            self.report(line, "unresolved-flat-import", name)

    def do_import(self, node: ast.Import) -> None:
        lines: list[str] = []
        changed = False
        for alias in node.names:
            comps = alias.name.split(".")
            res = self.resolve(comps)
            tail = f" as {alias.asname}" if alias.asname else ""
            if res is None:
                self.note_unresolved(comps[0], node.lineno)
            elif res.stdlib:
                self.report(node.lineno, "stdlib-shadow", alias.name)
            elif len(comps) > 1:
                self.report(node.lineno, "dotted-import", alias.name)
            else:
                pkg, _, leaf = res.new.rpartition(".")
                lines.append(f"from {pkg} import {leaf}{tail}")
                changed = True
                continue
            lines.append(f"import {alias.name}{tail}")
        if changed:
            self.replace_statement(node, lines, "I4" if len(lines) > 1 else "I1")

    def replace_statement(self, node: ast.stmt, lines: list[str], kind: str) -> None:
        if len(lines) > 1 and self.shares_line(node):
            self.report(node.lineno, "import-shared-line", self.seg(node))
            return
        start, end = self.span(node)
        text = ("\n" + self.indent(node)).join(lines)
        self.src.add(start, end, text, kind)

    def do_from(self, node: ast.ImportFrom) -> None:
        assert node.module is not None
        if node.module == "tools":
            self.do_from_tools(node)
            return
        comps = node.module.split(".")
        res = self.resolve(comps)
        if res is None:
            self.note_unresolved(comps[0], node.lineno)
            return
        if res.stdlib:
            self.report(node.lineno, "stdlib-shadow", node.module)
            return
        cut = res.n
        new = ".".join([res.new, *comps[cut:]])
        start, end = self.span(node)
        found = re.match(rb"from[ \t]+([\w.]+)", self.src.data[start:end])
        if new != node.module and found is not None:
            first, last = start + found.start(1), start + found.end(1)
            self.src.add(first, last, new, "I2")

    def do_from_tools(self, node: ast.ImportFrom) -> None:
        lines: list[str] = []
        unknown = False
        for alias in node.names:
            new = self.ctx.modmap.get(f"tools.{alias.name}")
            if alias.name == "*" or new is None:
                self.report(node.lineno, "from-tools-unknown", alias.name)
                unknown = True
                continue
            pkg, _, leaf = new.rpartition(".")
            tail = f" as {alias.asname}" if alias.asname else ""
            lines.append(f"from {pkg} import {leaf}{tail}")
        if not unknown:
            self.replace_statement(node, lines, "I3")

    def rewrite_string(self, node: ast.Constant) -> None:
        self.handled.add(id(node))
        value = node.value
        comps = value.split(".")
        if not all(c.isidentifier() for c in comps):
            return
        res = self.resolve(comps)
        if res is None:
            return
        if res.stdlib:
            self.report(node.lineno, "stdlib-shadow", value)
            return
        cut = res.n
        new = ".".join([res.new, *comps[cut:]])
        start, end = self.span(node)
        text = self.src.data[start:end]
        raw = value.encode()
        if text.count(raw) != 1:
            self.report(node.lineno, "string-unrewritable", value)
            return
        at = start + text.index(raw)
        self.src.add(at, at + len(raw), new, "string")

    # sys.path removal

    def has_marker(self, stmt: ast.stmt) -> bool:
        text = self.seg(stmt)
        if any(marker in text for marker in MARKERS):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return any(
                isinstance(a, ast.Constant) and a.value in (".", "tools")
                for a in stmt.value.args
            )
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.ListComp):
            return any(
                isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Slice)
                for t in stmt.targets
            )
        return False

    def removable(self, stmt: ast.stmt) -> bool:
        key = id(stmt)
        if key not in self.memo:
            self.memo[key] = self.compute_removable(stmt)
        return self.memo[key]

    def compute_removable(self, stmt: ast.stmt) -> bool:
        if is_syspath_stmt(stmt):
            return self.has_marker(stmt) or self.paired_pop(stmt)
        if isinstance(stmt, ast.If | ast.For) and not stmt.orelse:
            start, _ = self.span(stmt)
            if self.src.data[start : start + 4] == b"elif":
                return False
            return all(self.removable(s) for s in stmt.body)
        return False

    def paired_pop(self, stmt: ast.stmt) -> bool:
        """A sys.path.pop/remove statement in a scope that has a removable insert."""
        if syspath_call(stmt) not in PATH_POPS:
            return False
        if self.adds is None:
            self.adds = {
                self.scope_of[id(c)]
                for c in self.cands
                if syspath_call(c) in PATH_ADDS and self.has_marker(c)
            }
        return self.scope_of[id(stmt)] in self.adds

    def handle_syspath(self) -> None:
        for cand in self.cands:
            if not self.removable(cand):
                self.report(cand.lineno, "sys-path-kept", self.seg(cand))
        for owner in self.nodes:
            if isinstance(owner, ast.stmt) and self.removable(owner):
                continue
            if isinstance(owner, ast.Try):
                self.prepare_try(owner)
            skip = self.skip.get(id(owner), set())
            for name, value in ast.iter_fields(owner):
                if name not in skip and is_block(value):
                    self.prune_block(owner, value)
        self.dedent()

    def prepare_try(self, node: ast.Try) -> None:
        """Handle a finally block that holds only removable statements."""
        final = node.finalbody
        if not final or not all(self.removable(s) for s in final):
            return
        skip = self.skip.setdefault(id(node), set())
        last = final[-1].end_lineno
        assert last is not None
        line = self.finally_line(node)
        if line is not None and not any(self.shares_line(s) for s in final):
            if node.handlers:
                self.cut_lines(line, last)
                skip.add("finalbody")
                return
            if self.unwrap(node, line, last):
                skip.add("finalbody")
                return
        self.report(node.lineno, "sys-path-structure", "try/finally")
        skip.update(("body", "finalbody"))

    def finally_line(self, node: ast.Try) -> int | None:
        first = node.finalbody[0].lineno
        others = [*node.body, *node.handlers, *node.orelse]
        floor = max(getattr(s, "end_lineno", 0) or 0 for s in others)
        for line in range(first, floor, -1):
            if re.match(r"\s*finally\s*:", self.line_text(line)):
                return line
        return None

    def cut_lines(self, first: int, last: int) -> None:
        starts = self.src.starts
        end = starts[last] if last < len(starts) else len(self.src.data)
        self.src.add(starts[first - 1], end, "", "sys-path")

    def unwrap(self, node: ast.Try, fl: int, last: int) -> bool:
        """Replace try/finally by its body: drop the keyword lines, dedent the body."""
        body = node.body
        if all(self.removable(s) for s in body):
            return False
        k = body[0].col_offset - node.col_offset
        header = self.line_text(node.lineno)
        if k <= 0 or not re.fullmatch(r"\s*try\s*:\s*(#.*)?\n?", header):
            return False
        lo, hi = node.lineno + 1, fl - 1
        for line in range(lo, hi + 1):
            text = self.line_text(line)
            if text.strip() and len(text) - len(text.lstrip(" \t")) < k:
                return False
        rows = self.multiline_strings()
        if rows is None or any(a != b and a <= hi and b >= lo for a, b in rows):
            return False
        self.cut_lines(node.lineno, node.lineno)
        self.cut_lines(fl, last)
        self.pending.append((lo, hi, k))
        return True

    def multiline_strings(self) -> list[tuple[int, int]] | None:
        """Start and end rows of string tokens, or None when tokenizing fails."""
        if self.strings is None:
            rows: list[tuple[int, int]] = []
            opened: list[int] = []
            fstart = getattr(tokenize, "FSTRING_START", -1)
            fend = getattr(tokenize, "FSTRING_END", -1)
            try:
                for tok in tokenize.tokenize(io.BytesIO(self.src.data).readline):
                    if tok.type == fstart:
                        opened.append(tok.start[0])
                    elif tok.type == fend and opened:
                        rows.append((opened.pop(), tok.end[0]))
                    elif tok.type == tokenize.STRING:
                        rows.append((tok.start[0], tok.end[0]))
            except (tokenize.TokenError, SyntaxError):
                return None
            self.strings = rows
        return self.strings

    def dedent(self) -> None:
        amount: Counter[int] = Counter()
        for lo, hi, k in self.pending:
            for line in range(lo, hi + 1):
                amount[line] += k
        removed = [(e.start, e.end) for e in self.src.edits if e.kind == "sys-path"]
        for line in sorted(amount):
            start = self.src.starts[line - 1]
            end = start + amount[line]
            if not self.line_text(line).strip():
                continue
            if not any(s < end and start < e for s, e in removed):
                self.src.add(start, end, "", "sys-path")

    def prune_block(self, owner: ast.AST, block: list[ast.stmt]) -> None:
        gone = [s for s in block if self.removable(s)]
        emptied = len(gone) == len(block) and not isinstance(owner, ast.Module)
        for stmt in gone:
            if emptied or self.shares_line(stmt):
                self.report(stmt.lineno, "sys-path-structure", self.seg(stmt))
            else:
                self.remove_lines(stmt)

    def remove_lines(self, stmt: ast.stmt, kind: str = "sys-path") -> None:
        assert stmt.end_lineno is not None
        starts = self.src.starts
        start = starts[stmt.lineno - 1]
        end = len(self.src.data)
        if stmt.end_lineno < len(starts):
            end = starts[stmt.end_lineno]
        if kind == "sys-path":
            first = stmt.lineno - 1
            while first >= 1 and self.line_text(first).lstrip().startswith("#"):
                first -= 1
            if first < stmt.lineno - 1:
                text = f"lines {first + 1}-{stmt.lineno - 1}"
                self.report(stmt.lineno, "orphan-comment", text)
        self.src.add(start, end, "", kind)

    # unused import cleanup

    def drop_unused(self) -> None:
        removed = [(e.start, e.end) for e in self.src.edits if e.kind == "sys-path"]
        if not removed:
            return
        loads: dict[str, list[int]] = {}
        for node in self.nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                offset = self.pos(node.lineno, node.col_offset)
                loads.setdefault(node.id, []).append(offset)
        for stmt in self.tree.body:
            if not isinstance(stmt, ast.Import) or len(stmt.names) != 1:
                continue
            name = stmt.names[0]
            if name.name not in ("sys", "os") or name.asname:
                continue
            before = loads.get(name.name, [])
            after = [o for o in before if not any(s <= o < e for s, e in removed)]
            if before and not after and not self.shares_line(stmt):
                self.remove_lines(stmt, "unused-import")

    # __file__ anchors and joins

    def handle_anchors(self) -> None:
        if not self.moved:
            return
        for node in self.nodes:
            if id(node) in self.claimed:
                continue
            if not isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
                continue
            anchor = parse_anchor(node)
            if anchor is None or anchor.ups == 0:
                continue
            self.claimed.update(id(m) for m in anchor.members)
            if anchor.ups > self.depth:
                self.fix_anchor(node, anchor)
        for node in self.nodes:
            if id(node) in self.claimed:
                continue
            if not isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
                continue
            count, members, base = parse_chain(node)
            if not count or not isinstance(base, ast.Name):
                continue
            val = self.value(base, frozenset())
            if val is None:
                continue
            self.claimed.update(id(m) for m in members)
            if not self.chain_needs_fix(base, val, count):
                continue
            if not self.inside_removed(*self.span(node)):
                style = "os" if isinstance(node, ast.Call) else "path"
                self.fix_anchor(node, Anchor(count, members, style))

    def inside_removed(self, start: int, end: int) -> bool:
        edits = self.src.edits
        return any(
            e.kind == "sys-path" and e.start < end and start < e.end for e in edits
        )

    def chain_needs_fix(self, base: ast.AST, val: Val, count: int) -> bool:
        """A derived anchor that reaches above the file's old directory needs a level."""
        extra = count - min(count, val.tail)
        if not self.moved or not isinstance(base, ast.Name) or val.ups is None:
            return False
        return extra > 0 and val.ups <= self.depth < val.ups + extra

    def fix_anchor(self, node: ast.expr, anchor: Anchor) -> None:
        start, end = self.span(node)
        old = self.seg(node)
        if anchor.style == "os":
            new = f"os.path.dirname({old})"
            self.src.add(start, end, new, "anchor-depth", 1)
        elif isinstance(node, ast.Subscript):
            index = node.slice
            assert isinstance(index, ast.Constant)
            first, last = self.span(index)
            text = str(index.value + 1)
            head = self.src.data[start:first].decode("utf-8", "replace")
            tail = self.src.data[last:end].decode("utf-8", "replace")
            label = (old, head + text + tail)
            self.src.add(first, last, text, "anchor-depth", 1, label)
        else:
            label = (old, old + ".parent")
            self.src.add(end, end, ".parent", "anchor-depth", 1, label)

    def anchor_dirs(self, ups: int) -> Dirs | None:
        old_parts = self.src.old.split("/")
        keep = len(old_parts) - ups
        if keep < 0:
            return None
        old = old_parts[:keep]
        if not self.moved:
            return old, list(old)
        new_ups = ups + 1 if ups > self.depth else ups
        new_parts = self.src.new.split("/")
        keep_new = len(new_parts) - new_ups
        return old, new_parts[:keep_new]

    def eval_dir(self, expr: ast.AST, seen: frozenset[tuple[int, str]]) -> Dirs | None:
        val = self.value(expr, seen)
        return None if val is None else (val.old, val.new)

    def value(self, expr: ast.AST, seen: frozenset[tuple[int, str]]) -> Val | None:
        anchor = parse_anchor(expr)
        if anchor is not None:
            dirs = self.anchor_dirs(anchor.ups)
            return None if dirs is None else Val(dirs[0], dirs[1], anchor.ups, 0)
        if isinstance(expr, ast.Name):
            return self.name_val(expr, seen)
        count, _, base = parse_chain(expr)
        if count:
            return self.chain_val(count, base, seen)
        split = split_join(expr)
        if split is None:
            return None
        val = self.value(split[0], seen)
        parts = path_parts(split[1])
        if val is None or parts is None:
            return None
        return Val(
            [*val.old, *parts], [*val.new, *parts], val.ups, val.tail + len(parts)
        )

    def chain_val(
        self, count: int, base: ast.AST, seen: frozenset[tuple[int, str]]
    ) -> Val | None:
        val = self.value(base, seen)
        if val is None:
            return None
        fix = int(self.chain_needs_fix(base, val, count))
        if count > len(val.old) or count + fix > len(val.new):
            return None
        popped = min(count, val.tail)
        ups = None if val.ups is None else val.ups + count - popped
        old = val.old[: len(val.old) - count]
        new = val.new[: len(val.new) - count - fix]
        return Val(old, new, ups, val.tail - popped)

    def name_val(self, node: ast.Name, seen: frozenset[tuple[int, str]]) -> Val | None:
        name = node.id
        if name in self.known:
            old, new = self.known[name]
            return Val(list(old), list(new), None, 0)
        scope = self.scope_of.get(id(node), self.module)
        while (scope, name) not in self.assigned:
            parent = self.scope_parent.get(scope)
            if self.stores[scope, name] or parent is None:
                return None
            scope = parent
        key = (scope, name)
        if key in seen:
            return None
        return self.value(self.assigned[key], seen | {key})

    def handle_joins(self) -> None:
        for node in self.nodes:
            if is_div(node):
                if id(node) not in self.joined:
                    self.join_chain(node)
            elif isinstance(node, ast.Call):
                split = split_join(node)
                if split is not None:
                    self.check_join(node, split[0], split[1], True)

    def join_chain(self, node: ast.expr) -> None:
        consts: list[ast.expr] = []
        cur: ast.expr = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            if not str_const(cur.right):
                break
            self.joined.add(id(cur))
            consts.append(cur.right)
            cur = cur.left
        if consts:
            consts.reverse()
            self.check_join(node, cur, consts)

    def check_join(
        self,
        expr: ast.expr,
        base: ast.expr,
        consts: list[ast.expr],
        call: bool = False,
    ) -> None:
        dirs = self.eval_dir(base, frozenset())
        if dirs is None:
            return
        self.covered.update(id(c) for c in consts)
        parts: list[str] = []
        for const in consts:
            assert isinstance(const, ast.Constant)
            if const.value.startswith("/"):
                return
            for piece in const.value.split("/"):
                if piece == "..":
                    if not parts:
                        return
                    parts.pop()
                elif piece not in ("", "."):
                    parts.append(piece)
        old = "/".join([*dirs[0], *parts])
        now = "/".join([*dirs[1], *parts])
        moved = self.ctx.moved_to(old)
        if moved is not None and moved != now:
            if self.fix_join(consts, dirs[1], moved, call):
                return
            text = f"{self.seg(expr)}: old target {old}, moved to {moved}"
            self.report(expr.lineno, "join-mismatch", f"{text}, now yields {now}")

    def fix_join(
        self, consts: list[ast.expr], new_dir: list[str], moved: str, call: bool
    ) -> bool:
        """Rewrite the join literals to reach the moved target from the new directory."""
        target = moved.split("/")
        cut = len(new_dir)
        if len(target) <= cut or target[:cut] != new_dir:
            return False
        rest = target[cut:]
        first, last = consts[0], consts[-1]
        start, _ = self.span(first)
        _, end = self.span(last)
        quote = self.src.data[start : start + 1].decode("utf-8", "replace")
        if quote not in ("'", '"') or any(quote in p or "\\" in p for p in rest):
            return False
        sep = r"\s*,\s*" if call else r"\s*/\s*"
        for a, b in pairwise(consts):
            gap = self.src.data[self.span(a)[1] : self.span(b)[0]]
            if not re.fullmatch(sep, gap.decode("utf-8", "replace")):
                return False
        if self.inside_removed(start, end):
            return False
        assert isinstance(first, ast.Constant)
        # One literal stays one literal only when it already holds a path with "/".
        one = len(consts) == 1 and "/" in first.value
        pieces = ["/".join(rest)] if one else rest
        text = (", " if call else " / ").join(f"{quote}{p}{quote}" for p in pieces)
        self.src.add(start, end, text, "join", 1)
        return True

    # string constants

    def handle_literals(self) -> None:
        for node in self.nodes:
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            self.path_literal(node, value)
            if id(node) not in self.handled:
                self.dotted_literal(node, value)
                if id(node) not in self.covered and value in self.ctx.basenames:
                    self.report(node.lineno, "basename-literal", value)
            if value in ("tools", "tools/"):
                self.report(node.lineno, "tools-literal", value)

    def path_literal(self, node: ast.Constant, value: str) -> None:
        if "tools/" not in value:
            return
        hits = [p for p in self.ctx.old_py_tools if p in value]
        if hits:
            names = ", ".join(hits)
            self.report(node.lineno, "py-path-literal", f"{names}: {value}")

    def dotted_literal(self, node: ast.Constant, value: str) -> None:
        if not DOTTED_RE.fullmatch(value):
            return
        comps = value.split(".")
        if comps[0] not in self.ctx.oldnames or comps[-1] in EXTENSIONS:
            return
        core = value.startswith("sharc_core.")
        core = core and any(fnmatch.fnmatchcase(self.src.old, g) for g in CORE_FILES)
        self.report(node.lineno, "core-logical" if core else "dotted-literal", value)


def note(ctx: Ctx, src: Source, offset: int, category: str, text: str) -> None:
    ctx.reports.append((src.new, src.line_of(offset), category, clip(text)))


def text_rewrite(ctx: Ctx, src: Source) -> None:
    """T1 command, T2 path and T3 dotted rewrites on the original bytes."""
    is_py = src.old.endswith(".py")
    spans: list[tuple[int, int]] = []
    for m in T1_RE.finditer(src.data):
        text = m[0].decode()
        if m["rel"]:
            note(ctx, src, m.start(), "cmd-relative", text)
            continue
        new_path = ctx.py_moves.get("tools/" + m["path"].decode())
        if new_path is None:
            continue
        mod = mod_parts(new_path)
        if not all(p.isidentifier() for p in mod):
            continue
        py, flags = m["py"].decode(), m["flags"].decode()
        src.add(m.start(), m.end(), f"{py}{flags} -m {'.'.join(mod)}", "T1", 2)
        spans.append((m.start(), m.end()))
        if is_py:
            note(ctx, src, m.start(), "cmd-in-py", text)
    for m in T2_RE.finditer(src.data):
        if any(s <= m.start() < e for s, e in spans):
            continue
        rest = m["rest"].decode()
        core = rest.rstrip("./")
        cut = len(core)
        if not core:
            continue
        new, category = ctx.rewrite_path(core)
        if new:
            text = m["pre"].decode() + new + rest[cut:]
            src.add(m.start(), m.end(), text, "T2", 2)
        elif category == "stale-ref" and src.new.startswith("docs/"):
            ctx.stale_docs += 1
        elif category:
            note(ctx, src, m.start(), category, m[0].decode())
    if is_py:
        return
    for m in T3_RE.finditer(src.data):
        name = m["name"].decode()
        if name in ctx.toplevel and src.data[m.end() : m.end() + 3] != b".py":
            src.add(m.start(), m.end(), ctx.modmap[f"tools.{name}"], "T3", 2)
    for m in BARE_RE.finditer(src.data):
        note(ctx, src, m.start(), "bare-tools", m[0].decode())


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def overlaps(a: Edit, b: Edit) -> bool:
    return a.start < b.end and b.start < a.end


def settle(ctx: Ctx, src: Source, problems: list[str]) -> None:
    """Drop edits that lose to a higher-priority edit; fail on real overlaps."""
    kept: list[Edit] = []
    for prio in (0, 1, 2):
        for edit in sorted(src.edits, key=lambda e: (e.start, e.end)):
            if edit.prio != prio:
                continue
            if prio == 0 or not any(overlaps(edit, k) for k in kept):
                kept.append(edit)
            else:
                text = f"{edit.kind}: {edit.old}"
                ctx.reports.append((src.new, edit.line, "edit-dropped", clip(text)))
    kept.sort(key=lambda e: (e.start, e.end))
    for a, b in pairwise(kept):
        if a.end > b.start or a.start == b.start:
            problems.append(f"{src.old}: overlapping edits at lines {a.line}, {b.line}")
    src.edits = kept
    src.out = src.apply()


def analyse(
    ctx: Ctx, src: Source, exclude: tuple[str, ...], problems: list[str]
) -> None:
    is_py = src.old.endswith(".py")
    if is_py:
        try:
            tree = ast.parse(src.data)
        except (SyntaxError, ValueError, RecursionError) as exc:
            note(ctx, src, 0, "parse-error", f"{type(exc).__name__}: {exc}")
        else:
            PyFile(ctx, src, tree).run()
    skipped = src.old.startswith(exclude) or src.new.startswith(exclude)
    if is_text(src.data) and not skipped:
        text_rewrite(ctx, src)
    settle(ctx, src, problems)
    if is_py and src.edits:
        try:
            ast.parse(src.out)
        except (SyntaxError, ValueError, RecursionError) as exc:
            problems.append(f"{src.new}: edited file does not parse: {exc}")


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout


def check_moves(root: Path, ctx: Ctx, apply: bool) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for old, new in sorted(ctx.moves.items()):
        if old not in ctx.tracked:
            problems.append(f"source is not a tracked file: {old}")
        if new in seen:
            problems.append(f"duplicate destination {new}: {seen[new]} and {old}")
        seen[new] = old
        if new in ctx.tracked or (root / new).exists():
            problems.append(f"destination exists: {new}")
        if len(new.split("/")) != len(old.split("/")) + 1:
            problems.append(f"not exactly one directory deeper: {old} -> {new}")
    if apply and git(root, "status", "--porcelain", "--untracked-files=no").strip():
        problems.append("working tree has uncommitted changes to tracked files")
    return problems


def pending_packages(
    root: Path, ctx: Ctx, packages: dict[str, str]
) -> list[tuple[str, str]]:
    dests = set(ctx.moves.values())
    out: list[tuple[str, str]] = []
    for directory, text in sorted(packages.items()):
        init = f"{directory}/__init__.py"
        if init in dests or init in ctx.tracked or (root / init).exists():
            continue
        out.append((init, f'"""{text}"""\n'))
    return out


def read_sources(root: Path, ctx: Ctx, skip: set[str]) -> dict[str, Source]:
    srcs: dict[str, Source] = {}
    for rel in sorted(ctx.tracked - skip):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        srcs[rel] = Source(rel, ctx.moves.get(rel, rel), path.read_bytes())
    return srcs


def apply_plan(
    root: Path, ctx: Ctx, srcs: dict[str, Source], pkgs: list[tuple[str, str]]
) -> None:
    for old, new in sorted(ctx.moves.items()):
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", old, new)
    for src in srcs.values():
        if src.edits:
            (root / src.new).write_bytes(src.out)
    for init, text in pkgs:
        path = root / init
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def build_report(ctx: Ctx, srcs: dict[str, Source]) -> dict[str, Any]:
    edits = [
        {"file": s.new, "line": e.line, "kind": e.kind, "old": e.old, "new": e.new}
        for s in srcs.values()
        for e in s.edits
    ]
    edits.sort(key=lambda e: (e["file"], e["line"], e["kind"], e["old"], e["new"]))
    reports = [
        {"file": f, "line": n, "category": c, "text": t}
        for f, n, c, t in sorted(set(ctx.reports))
    ]
    reports.sort(key=lambda r: (r["file"], r["line"], r["category"], r["text"]))
    moves = [[old, new] for old, new in sorted(ctx.moves.items())]
    return {"moves": moves, "edits": edits, "reports": reports}


def summary(report: dict[str, Any], packages: int, stale_docs: int) -> str:
    lines = [f"moves: {len(report['moves'])}", f"packages created: {packages}"]
    lines.append("edits:")
    kinds = Counter(e["kind"] for e in report["edits"])
    for kind, count in sorted(kinds.items()):
        lines.append(f"  {kind}: {count}")
    lines.append("reports:")
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in report["reports"]:
        by_category.setdefault(item["category"], []).append(item)
    for category, items in sorted(by_category.items()):
        lines.append(f"  {category}: {len(items)}")
    lines.append(f"  stale-ref in docs (not listed): {stale_docs}")
    for category, items in sorted(by_category.items()):
        lines.append(f"first {min(len(items), SUMMARY_LIMIT)} of {category}:")
        for item in items[:SUMMARY_LIMIT]:
            lines.append(f"  {item['file']}:{item['line']}: {item['text']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a file move map.")
    parser.add_argument("map", type=Path, help="JSON map file")
    parser.add_argument("--apply", action="store_true", help="move and edit files")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--root", type=Path, help="repository root")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.map.read_text(encoding="utf-8"))
        moves = {str(k): str(v) for k, v in spec["moves"].items()}
        packages = {str(k): str(v) for k, v in spec.get("packages", {}).items()}
        exclude = tuple(str(p) for p in spec.get("exclude_text", []))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"relocate: cannot read the map: {exc}", file=sys.stderr)
        return 2
    try:
        top = args.root or git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        root = Path(top).resolve()
        tracked = {f for f in git(root, "ls-files", "-z").split("\0") if f}
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"relocate: not a git repository: {exc}", file=sys.stderr)
        return 2
    try:
        map_rel = args.map.resolve().relative_to(root).as_posix()
    except ValueError:
        map_rel = ""
    ctx = Ctx(moves, tracked)
    problems = check_moves(root, ctx, args.apply)
    srcs = read_sources(root, ctx, {SELF, map_rel})
    for src in srcs.values():
        analyse(ctx, src, exclude, problems)
    if problems:
        for problem in sorted(problems):
            print(f"relocate: {problem}", file=sys.stderr)
        return 1
    pkgs = pending_packages(root, ctx, packages)
    if args.apply:
        try:
            apply_plan(root, ctx, srcs, pkgs)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"relocate: apply failed: {exc}", file=sys.stderr)
            return 1
    report = build_report(ctx, srcs)
    if args.report:
        text = json.dumps(report, indent=2, ensure_ascii=False)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(summary(report, len(pkgs), ctx.stale_docs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
