#!/usr/bin/env python3
"""Split one Python module into a package of modules.

Moves the top-level statements of one module verbatim into new modules named in
a plan, writes the import statements each new module needs, a facade
__init__.py that keeps the old import path working, and __main__.py when the
module had a main block. What it cannot do safely it reports instead. It uses
only the standard library and imports nothing from the repository (repository
scans read files).

Usage:
    uv run python tools/devtools/split_module.py PLAN.json [--apply]
        [--report REPORT.json] [--root DIR]

PLAN.json has "source" (the module path), "modules" (new module name to
{"doc", "take"}), "facade_extra" (names the facade exports besides the ones
used from outside) and "main_module". A "take" entry is a top-level name, an
fnmatch pattern over top-level names or @L<a>-L<b> (every statement whose
first line lies in the range). Without --apply nothing changes: everything is
computed in memory, written to the report file and summarised. With --apply,
and only when every check passes, the source is removed with git rm and the
package files are written and added.

Exit status: 0 on success, 1 when a check fails (nothing is changed), 2 on
usage errors.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import fnmatch
import io
import json
import keyword
import re
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

SUMMARY_LIMIT = 10
LINE_LIMIT = 88
GLOB_CHARS = set("*?[")

FATAL = {
    "ambiguous-name",
    "destination-exists",
    "dirty-tree",
    "double-assigned",
    "facade-extra-unknown",
    "generated-parse-error",
    "global-cross",
    "import-cycle",
    "main-dedent",
    "main-inline",
    "no-main-block",
    "shared-line",
    "source-unreadable",
    "source-untracked",
    "star-import",
    "take-empty",
    "take-unmatched",
    "unassigned",
    "undefined-name",
}
BUILTIN_EXTRAS = {
    "__file__",
    "__name__",
    "__doc__",
    "__spec__",
    "__package__",
    "__builtins__",
    "__annotations__",
    "__class__",
}
BUILTINS = set(dir(builtins)) | BUILTIN_EXTRAS
IMPORT_CALLS = {"importlib.import_module", "import_module", "__import__"}
STATUS_ARGS = ("status", "--porcelain", "--untracked-files=no")
Comp = ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
Func = ast.FunctionDef | ast.AsyncFunctionDef
NameBinders = ast.ExceptHandler | ast.MatchAs | ast.MatchStar
Spans = list[tuple[int, int, int]]  # (first source line, last source line, offset)
Hazard = tuple[int, str, str]  # (line, category, statement text)


@dataclass
class Stmt:
    node: ast.stmt
    start: int  # first line, decorators included
    end: int
    lead: int  # first line of the leading block
    bound: set[str]
    kind: str  # doc, future, import, main or code
    owners: list[str] = field(default_factory=list)


@dataclass
class ModInfo:
    name: str
    stmts: list[Stmt]
    bound: set[str] = field(default_factory=set)
    edges: dict[str, dict[str, bool]] = field(default_factory=dict)
    lines: dict[tuple[str, str], int] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    checking: set[str] = field(default_factory=set)


@dataclass
class Use:
    name: str
    line: int
    ann: bool  # needed only by annotations (strings under the future import)
    soft: bool  # found in a string annotation: not an error when unresolved


@dataclass
class Binds:
    bound: set[str] = field(default_factory=set)
    globs: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    global_nodes: list[ast.Global] = field(default_factory=list)
    star: bool = False


@dataclass
class Scope:
    kind: str  # module, function, class or comp
    bound: set[str]
    globs: set[str] = field(default_factory=set)


Scopes = list[Scope]


@dataclass
class Target:
    dotted: str
    stem: str
    parent: str
    directory: str
    unique: bool


@dataclass
class Usage:
    names: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    star: bool = False


def clip(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


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


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout


def bound_name(alias: ast.alias) -> str:
    return (alias.asname or alias.name).split(".")[0]


def alias_text(alias: ast.alias) -> str:
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def format_from(module: str, names: list[str], indent: str = "") -> str:
    line = f"{indent}from {module} import {', '.join(names)}"
    if len(line) <= LINE_LIMIT:
        return line
    body = "".join(f"{indent}    {name},\n" for name in names)
    return f"{indent}from {module} import (\n{body}{indent})"


def render_import(node: ast.Import | ast.ImportFrom, used: set[str]) -> str | None:
    """The import statement filtered to the used names, one package level deeper."""
    kept = [alias for alias in node.names if bound_name(alias) in used]
    if not kept:
        return None
    names = [alias_text(alias) for alias in kept]
    if isinstance(node, ast.Import):
        return "import " + ", ".join(names)
    dots = "." * (node.level + 1) if node.level else ""
    return format_from(dots + (node.module or ""), names)


def docstring(text: str) -> str:
    body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    return f'"""{body}"""'


def is_main_block(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or node.orelse:
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def map_line(spans: Spans, base: int, line: int) -> int:
    """Line in the new file for a source line (base is the first body line)."""
    for first, end, offset in spans:
        if first <= line <= end:
            return base + offset + (line - first)
    return line


def multiline_rows(text: str) -> list[tuple[int, int]] | None:
    """Start and end rows of string tokens, or None when tokenizing fails."""
    rows: list[tuple[int, int]] = []
    opened: list[int] = []
    fstart = getattr(tokenize, "FSTRING_START", -1)
    fend = getattr(tokenize, "FSTRING_END", -1)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == fstart:
                opened.append(tok.start[0])
            elif tok.type == fend and opened:
                rows.append((opened.pop(), tok.end[0]))
            elif tok.type == tokenize.STRING:
                rows.append((tok.start[0], tok.end[0]))
    except (tokenize.TokenError, SyntaxError):
        return None
    return rows


# scopes and free names


def collect(nodes: Sequence[ast.AST]) -> Binds:
    """Names bound directly in a scope made of these nodes."""
    binds = Binds()
    for node in nodes:
        collect_into(node, binds)
    return binds


def collect_into(node: ast.AST, binds: Binds) -> None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        binds.bound.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, Comp):
        for inner in ast.walk(node):
            if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
                binds.bound.add(inner.target.id)
        return
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        binds.bound.add(node.id)
    elif isinstance(node, ast.Import | ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                binds.star = True
            else:
                binds.bound.add(bound_name(alias))
    elif isinstance(node, NameBinders) and node.name:
        binds.bound.add(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
        binds.bound.add(node.rest)
    elif isinstance(node, ast.Global):
        binds.globs.update(node.names)
        binds.global_nodes.append(node)
    elif isinstance(node, ast.Nonlocal):
        binds.nonlocals.update(node.names)
    for child in ast.iter_child_nodes(node):
        collect_into(child, binds)


def all_args(args: ast.arguments) -> list[ast.arg]:
    out = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        out.append(args.vararg)
    if args.kwarg:
        out.append(args.kwarg)
    return out


def default_exprs(args: ast.arguments) -> list[ast.expr]:
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None)]


def is_module_level(name: str, scopes: Scopes) -> bool:
    """True when the name is looked up in the module (or is unknown)."""
    last = len(scopes) - 1
    for index in range(last, 0, -1):
        scope = scopes[index]
        if scope.kind == "class" and index != last:
            continue
        if name in scope.globs:
            return True
        if name in scope.bound:
            return False
    return True


class Analyzer:
    """Module-level references of a list of statements, with correct scoping."""

    def __init__(self, future: bool) -> None:
        self.future = future
        self.uses: list[Use] = []
        self.global_decls: list[tuple[str, int]] = []
        self.global_stores: set[str] = set()
        self.depth = 0  # inside an annotation
        self.soft = 0  # inside a parsed string annotation

    def run(self, stmts: list[ast.stmt]) -> None:
        scopes = [Scope("module", set())]
        for stmt in stmts:
            self.visit(stmt, scopes)

    def visit(self, node: ast.AST, scopes: Scopes) -> None:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            self.children(node, scopes)
        else:
            method(node, scopes)

    def children(self, node: ast.AST, scopes: Scopes) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child, scopes)

    def use(self, name: str, line: int, scopes: Scopes) -> None:
        if is_module_level(name, scopes):
            ann = self.depth > 0 and (self.future or self.soft > 0)
            self.uses.append(Use(name, line, ann, self.soft > 0))

    def note_globals(self, binds: Binds) -> None:
        for node in binds.global_nodes:
            self.global_decls.extend((name, node.lineno) for name in node.names)
        self.global_stores |= binds.globs & binds.bound

    def annotation(self, node: ast.expr, scopes: Scopes) -> None:
        self.depth += 1
        self.visit(node, scopes)
        self.depth -= 1

    def with_type_params(self, node: ast.AST, scopes: Scopes) -> Scopes:
        params = getattr(node, "type_params", None)
        if not params:
            return scopes
        for param in params:
            bound = getattr(param, "bound", None)
            if bound is not None:
                self.visit(bound, scopes)
        return [*scopes, Scope("function", {param.name for param in params})]

    def visit_Name(self, node: ast.Name, scopes: Scopes) -> None:
        if not isinstance(node.ctx, ast.Store):
            self.use(node.id, node.lineno, scopes)

    def visit_AugAssign(self, node: ast.AugAssign, scopes: Scopes) -> None:
        if isinstance(node.target, ast.Name):
            self.use(node.target.id, node.lineno, scopes)
        self.children(node, scopes)

    def visit_Constant(self, node: ast.Constant, scopes: Scopes) -> None:
        if self.depth and isinstance(node.value, str):
            self.string_annotation(node, node.value, scopes)

    def string_annotation(self, node: ast.Constant, text: str, scopes: Scopes) -> None:
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError):
            return
        ast.increment_lineno(tree, node.lineno - 1)
        self.soft += 1
        self.visit(tree.body, scopes)
        self.soft -= 1

    def visit_AnnAssign(self, node: ast.AnnAssign, scopes: Scopes) -> None:
        self.annotation(node.annotation, scopes)
        self.visit(node.target, scopes)
        if node.value is not None:
            self.visit(node.value, scopes)

    def visit_TypeAlias(self, node: ast.AST, scopes: Scopes) -> None:
        inner = self.with_type_params(node, scopes)
        value = getattr(node, "value", None)
        if value is not None:
            self.visit(value, inner)

    def visit_FunctionDef(self, node: Func, scopes: Scopes) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator, scopes)
        outer = self.with_type_params(node, scopes)
        for default in default_exprs(node.args):
            self.visit(default, outer)
        args = all_args(node.args)
        for arg in args:
            if arg.annotation is not None:
                self.annotation(arg.annotation, outer)
        if node.returns is not None:
            self.annotation(node.returns, outer)
        binds = collect(list(node.body))
        self.note_globals(binds)
        params = {arg.arg for arg in args}
        bound = (binds.bound | params) - binds.globs - binds.nonlocals
        inner = [*outer, Scope("function", bound, binds.globs)]
        for stmt in node.body:
            self.visit(stmt, inner)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda, scopes: Scopes) -> None:
        for default in default_exprs(node.args):
            self.visit(default, scopes)
        params = {arg.arg for arg in all_args(node.args)}
        self.visit(node.body, [*scopes, Scope("function", params)])

    def visit_ClassDef(self, node: ast.ClassDef, scopes: Scopes) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator, scopes)
        outer = self.with_type_params(node, scopes)
        for base in node.bases:
            self.visit(base, outer)
        for keyword_ in node.keywords:
            self.visit(keyword_.value, outer)
        binds = collect(list(node.body))
        self.note_globals(binds)
        implicit = {"__module__", "__qualname__"}
        bound = (binds.bound | implicit) - binds.globs - binds.nonlocals
        inner = [*outer, Scope("class", bound, binds.globs)]
        for stmt in node.body:
            self.visit(stmt, inner)

    def comprehension(self, node: Comp, scopes: Scopes, elts: list[ast.expr]) -> None:
        gens = node.generators
        self.visit(gens[0].iter, scopes)
        targets: list[ast.AST] = [gen.target for gen in gens]
        inner = [*scopes, Scope("comp", collect(targets).bound)]
        for index, gen in enumerate(gens):
            if index:
                self.visit(gen.iter, inner)
            self.visit(gen.target, inner)
            for cond in gen.ifs:
                self.visit(cond, inner)
        for elt in elts:
            self.visit(elt, inner)

    def visit_ListComp(self, node: ast.ListComp, scopes: Scopes) -> None:
        self.comprehension(node, scopes, [node.elt])

    def visit_SetComp(self, node: ast.SetComp, scopes: Scopes) -> None:
        self.comprehension(node, scopes, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp, scopes: Scopes) -> None:
        self.comprehension(node, scopes, [node.elt])

    def visit_DictComp(self, node: ast.DictComp, scopes: Scopes) -> None:
        self.comprehension(node, scopes, [node.key, node.value])


# hazards in moved code


def hazard_of(node: ast.AST, modname: str) -> str:
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return "file-anchor"
        return "module-name" if node.id == "__name__" else ""
    if isinstance(node, ast.Constant):
        value = node.value
        named = isinstance(value, str) and (
            value == modname or value.startswith(modname + ".")
        )
        return "module-name" if named else ""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        plain = node.func.id == "globals" or (node.func.id == "vars" and not node.args)
        return "dynamic" if plain else ""
    if isinstance(node, ast.Subscript) and dotted(node.value) == "sys.modules":
        by_name = isinstance(node.slice, ast.Name) and node.slice.id == "__name__"
        return "dynamic" if by_name else ""
    return ""


def statement_text(stmt: ast.stmt, text: str) -> str:
    seg = ast.get_source_segment(text, stmt) or ""
    if hasattr(stmt, "body") or hasattr(stmt, "cases"):
        seg = seg.split("\n")[0]
    return clip(seg)


def scan_hazards(root: ast.stmt, text: str, modname: str) -> list[Hazard]:
    """(line, category, statement text) for file anchors and module-name uses."""
    found: set[Hazard] = set()
    stack: list[tuple[ast.AST, ast.stmt]] = [(root, root)]
    while stack:
        node, current = stack.pop()
        if isinstance(node, ast.stmt):
            current = node
        category = hazard_of(node, modname)
        if category:
            line = getattr(node, "lineno", current.lineno)
            found.add((line, category, statement_text(current, text)))
        stack.extend((child, current) for child in ast.iter_child_nodes(node))
    return sorted(found)


# import graph


def components(graph: dict[str, set[str]]) -> dict[str, int]:
    """Strongly connected components: node to component id."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    comp: dict[str, int] = {}

    def strong(node: str) -> None:
        index[node] = len(index)
        low[node] = index[node]
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                strong(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt not in comp:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            while True:
                top = stack.pop()
                comp[top] = index[node]
                if top == node:
                    break

    for node in sorted(graph):
        if node not in index:
            strong(node)
    return comp


def find_cycle(graph: dict[str, set[str]], members: list[str]) -> list[str]:
    """A shortest cycle through the first member, as a path that returns to it."""
    start = members[0]
    inside = set(members)
    parent: dict[str, str] = {}
    queue = [start]
    seen = {start}
    at = 0
    while at < len(queue):
        node = queue[at]
        at += 1
        for nxt in sorted(graph[node]):
            if nxt == start:
                path = [node]
                while path[-1] != start:
                    path.append(parent[path[-1]])
                path.reverse()
                return [*path, start]
            if nxt in inside and nxt not in seen:
                seen.add(nxt)
                parent[nxt] = node
                queue.append(nxt)
    return [*members, start]


# scanning the repository for users of the module


def absolute_from(node: ast.ImportFrom, package: list[str]) -> str | None:
    if node.level == 0:
        return node.module
    if node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    extra = node.module.split(".") if node.module else []
    return ".".join([*base, *extra])


def call_kind(fname: str) -> str:
    if fname in ("getattr", "hasattr"):
        return "read"
    if fname in ("setattr", "delattr"):
        return "mutate"
    tail = ".".join(fname.split(".")[-2:])
    if tail in ("monkeypatch.setattr", "monkeypatch.delattr", "patch.object"):
        return "mutate"
    if fname == "patch" or fname.endswith(".patch"):
        return "mutate"
    return ""


class FileScanner:
    """Uses of the split module in one file: names, mutations, unresolved uses."""

    def __init__(self, rel: str, tree: ast.Module, target: Target) -> None:
        self.tree = tree
        self.target = target
        self.package = rel.split("/")[:-1]
        flat = target.unique or "/".join(self.package) == target.directory
        self.mods = {target.dotted} | ({target.stem} if flat else set())
        self.aliases = set(self.mods)  # dotted expressions that denote the module
        self.simple: set[str] = set()  # plain names bound to the module
        self.consumed: set[int] = set()
        self.names: dict[str, set[int]] = {}
        self.mutations: list[tuple[int, str]] = []
        self.unresolved: list[tuple[int, str]] = []
        self.star: list[int] = []

    def run(self) -> None:
        self.collect_aliases()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute) and self.is_module(node.value):
                self.consumed.add(id(node.value))
                self.use_name(node.attr, node.lineno)
                if not isinstance(node.ctx, ast.Load):
                    self.mutations.append((node.lineno, node.attr))
            elif isinstance(node, ast.Call):
                self.call(node)
        for node in ast.walk(self.tree):
            if id(node) in self.consumed:
                continue
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in self.simple
            ):
                text = f"module object used as a value: {node.id}"
                self.unresolved.append((node.lineno, text))
            elif self.module_ref(node):
                text = "module object obtained dynamically"
                self.unresolved.append((getattr(node, "lineno", 0), text))

    def bind(self, name: str) -> None:
        self.aliases.add(name)
        self.simple.add(name)

    def use_name(self, name: str, line: int) -> None:
        self.names.setdefault(name, set()).add(line)

    def module_ref(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and node.args:
            if dotted(node.func) not in IMPORT_CALLS:
                return False
            arg = node.args[0]
            return isinstance(arg, ast.Constant) and arg.value in self.mods
        if isinstance(node, ast.Subscript) and dotted(node.value) == "sys.modules":
            key = node.slice
            return isinstance(key, ast.Constant) and key.value in self.mods
        return False

    def is_module(self, node: ast.AST) -> bool:
        return dotted(node) in self.aliases or self.module_ref(node)

    def collect_aliases(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in self.mods:
                        continue
                    if alias.asname:
                        self.bind(alias.asname)
                    elif "." not in alias.name:
                        self.bind(alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.from_import(node)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and self.module_ref(node.value)
            ):
                self.bind(node.targets[0].id)
                self.consumed.add(id(node.value))

    def from_import(self, node: ast.ImportFrom) -> None:
        module = absolute_from(node, self.package)
        if module is None:
            return
        if module in self.mods:
            for alias in node.names:
                if alias.name == "*":
                    self.star.append(node.lineno)
                else:
                    self.use_name(alias.name, node.lineno)
        elif module and module == self.target.parent:
            for alias in node.names:
                if alias.name == self.target.stem:
                    self.bind(alias.asname or alias.name)

    def call(self, node: ast.Call) -> None:
        fname = dotted(node.func)
        kind = call_kind(fname)
        if not kind or not node.args:
            return
        first = node.args[0]
        if self.is_module(first):
            self.consumed.add(id(first))
            second = node.args[1] if len(node.args) > 1 else None
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                self.use_name(second.value, node.lineno)
                if kind == "mutate":
                    self.mutations.append((node.lineno, second.value))
            else:
                text = f"{fname} with a name that is not a literal"
                self.unresolved.append((node.lineno, text))
        elif kind == "mutate" and isinstance(first, ast.Constant):
            value = first.value
            for mod in sorted(self.mods):
                if isinstance(value, str) and value.startswith(mod + "."):
                    name = value[len(mod) + 1 :].split(".")[0]
                    if name:
                        self.use_name(name, node.lineno)
                        self.mutations.append((node.lineno, name))
                    break


# the splitter


class Splitter:
    """Plan, checks and generated files, all in memory."""

    def __init__(self, root: Path, plan: dict[str, Any], tracked: set[str]) -> None:
        self.root = root
        self.plan = plan
        self.tracked = tracked
        self.source: str = plan["source"]
        self.pkg = self.source.removesuffix(".py")
        self.dotted = self.pkg.replace("/", ".")
        self.modules: dict[str, dict[str, Any]] = plan["modules"]
        self.reports: list[tuple[str, int, str, str]] = []
        self.files: dict[str, str] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.exports: list[str] = []
        self.text = ""
        self.lines: list[str] = []
        self.stmts: list[Stmt] = []
        self.doc_stmt: Stmt | None = None
        self.main_stmt: Stmt | None = None
        self.future_names: list[str] = []
        self.future_line = ""
        self.has_ann = False
        self.defs: dict[str, set[str]] = {}
        self.top_defs: dict[str, set[str]] = {}
        self.creators: dict[str, set[str]] = {}
        self.import_names: set[str] = set()
        self.infos: dict[str, ModInfo] = {}
        self.main_edges: dict[str, dict[str, bool]] = {}
        self.main_used: set[str] = set()
        self.kept: set[int] = set()

    def report(self, file: str, line: int, category: str, text: str) -> None:
        self.reports.append((file, line, category, clip(text)))

    def failed(self) -> bool:
        return any(r[2] in FATAL for r in self.reports)

    def line_text(self, line: int) -> str:
        return self.lines[line - 1] if 0 < line <= len(self.lines) else ""

    def run(self, apply: bool) -> None:
        if not self.precheck(apply) or not self.load():
            return
        self.assign()
        if self.failed():
            return
        self.resolve()
        if self.failed():
            return
        for mod in sorted(self.infos):
            self.build_module(self.infos[mod])
        self.build_main()
        usage = self.scan_repo()
        self.build_facade(usage)
        self.finish()

    # checks and loading

    def precheck(self, apply: bool) -> bool:
        if self.source not in self.tracked:
            self.report(self.source, 0, "source-untracked", "not a tracked file")
        prefix = self.pkg + "/"
        if (self.root / self.pkg).exists() or any(
            f.startswith(prefix) for f in self.tracked
        ):
            self.report(self.pkg, 0, "destination-exists", "package path exists")
        if apply and git(self.root, *STATUS_ARGS).strip():
            text = "working tree has uncommitted changes to tracked files"
            self.report(self.source, 0, "dirty-tree", text)
        return not self.failed()

    def load(self) -> bool:
        try:
            self.text = (self.root / self.source).read_text(encoding="utf-8")
            tree = ast.parse(self.text)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            text = f"{type(exc).__name__}: {exc}"
            self.report(self.source, 0, "source-unreadable", text)
            return False
        self.lines = self.text.split("\n")
        self.classify(tree)
        return True

    def kind_of(self, index: int, node: ast.stmt) -> str:
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return "doc"
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "__future__"
        ):
            return "future"
        if isinstance(node, ast.Import | ast.ImportFrom):
            return "import"
        return "main" if is_main_block(node) else "code"

    def classify(self, tree: ast.Module) -> None:
        prev_end = 0
        for index, node in enumerate(tree.body):
            decorators = getattr(node, "decorator_list", [])
            start = min([node.lineno, *(d.lineno for d in decorators)])
            end = node.end_lineno or node.lineno
            binds = collect([node])
            kind = self.kind_of(index, node)
            if kind == "main" and self.main_stmt is not None:
                kind = "code"
            stmt = Stmt(node, start, end, prev_end + 1, binds.bound, kind)
            if start <= prev_end:
                self.report(self.source, start, "shared-line", self.line_text(start))
            if binds.star:
                self.report(self.source, start, "star-import", self.line_text(start))
            if kind == "future" and isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name not in self.future_names:
                        self.future_names.append(alias.name)
            elif kind == "doc":
                self.doc_stmt = stmt
            elif kind == "main":
                self.main_stmt = stmt
            self.stmts.append(stmt)
            prev_end = end
        if self.future_names:
            self.future_line = "from __future__ import " + ", ".join(self.future_names)
        self.has_ann = "annotations" in self.future_names

    # assignment of statements to modules

    @staticmethod
    def match(entry: str, candidates: list[Stmt]) -> list[Stmt]:
        span = re.fullmatch(r"@L(\d+)-L(\d+)", entry)
        if span:
            low, high = int(span.group(1)), int(span.group(2))
            return [s for s in candidates if low <= s.start <= high]
        if GLOB_CHARS & set(entry):
            return [
                s
                for s in candidates
                if any(fnmatch.fnmatchcase(name, entry) for name in s.bound)
            ]
        return [s for s in candidates if entry in s.bound]

    def assign(self) -> None:
        candidates = [s for s in self.stmts if s.kind == "code"]
        for mod in sorted(self.modules):
            for entry in self.modules[mod]["take"]:
                hits = self.match(entry, candidates)
                if not hits:
                    self.report(self.source, 0, "take-unmatched", f"{mod}: {entry}")
                for stmt in hits:
                    if mod not in stmt.owners:
                        stmt.owners.append(mod)
            if not any(mod in s.owners for s in candidates):
                self.report(self.source, 0, "take-empty", mod)
        for stmt in candidates:
            first = self.line_text(stmt.start)
            if not stmt.owners:
                self.report(self.source, stmt.start, "unassigned", first)
            elif len(stmt.owners) > 1:
                owners = ", ".join(sorted(stmt.owners))
                self.report(self.source, stmt.start, "double-assigned", owners)
        if self.plan.get("main_module") and self.main_stmt is None:
            text = "main_module is set but the source has no main block"
            self.report(self.source, 0, "no-main-block", text)

    # names, imports and the module graph

    def import_aliases(self, stmt: Stmt) -> list[ast.alias]:
        node = stmt.node
        assert isinstance(node, ast.Import | ast.ImportFrom)
        return node.names

    def resolve(self) -> None:
        for stmt in self.stmts:
            if stmt.kind == "import":
                aliases = self.import_aliases(stmt)
                self.import_names.update(bound_name(a) for a in aliases)
        analysers: dict[str, Analyzer] = {}
        for mod in sorted(self.modules):
            stmts = [s for s in self.stmts if mod in s.owners]
            analyser = Analyzer(self.has_ann)
            analyser.run([s.node for s in stmts])
            analysers[mod] = analyser
            info = ModInfo(mod, stmts)
            for stmt in stmts:
                info.bound |= stmt.bound
                for name in stmt.bound:
                    self.top_defs.setdefault(name, set()).add(mod)
            info.bound |= analyser.global_stores
            for name in analyser.global_stores:
                self.creators.setdefault(name, set()).add(mod)
            self.infos[mod] = info
        self.defs = {n: set(m) for n, m in self.top_defs.items()}
        for name, mods in self.creators.items():
            if name not in self.top_defs:
                self.defs[name] = set(mods)
        for mod, info in self.infos.items():
            analyser = analysers[mod]
            info.edges, info.lines, info.used = self.bind_uses(
                mod, analyser.uses, info.bound
            )
            for name, line in analyser.global_decls:
                owners = self.top_defs.get(name) or self.creators.get(name, set())
                owners = owners if name in self.top_defs else owners - {mod}
                if owners and mod not in owners:
                    where = ", ".join(sorted(owners))
                    text = f"{mod}: global {name} is defined in {where}"
                    self.report(self.source, line, "global-cross", text)
        self.break_cycles()
        self.resolve_main()

    def resolve_main(self) -> None:
        stmt = self.main_stmt
        if stmt is None:
            return
        node = stmt.node
        assert isinstance(node, ast.If)
        analyser = Analyzer(self.has_ann)
        analyser.run(list(node.body))
        binds = collect(list(node.body))
        bound = binds.bound | analyser.global_stores
        self.main_edges, _, self.main_used = self.bind_uses(
            "__main__", analyser.uses, bound
        )

    def bind_uses(
        self, label: str, uses: list[Use], bound: set[str]
    ) -> tuple[dict[str, dict[str, bool]], dict[tuple[str, str], int], set[str]]:
        edges: dict[str, dict[str, bool]] = {}
        lines: dict[tuple[str, str], int] = {}
        used: set[str] = set()
        for use in sorted(uses, key=lambda u: (u.line, u.name)):
            name = use.name
            if name in bound or name in BUILTINS:
                continue
            owners = self.defs.get(name)
            if owners and len(owners) > 1:
                where = ", ".join(sorted(owners))
                text = f"{label}: {name} is defined in {where}"
                self.report(self.source, use.line, "ambiguous-name", text)
            elif owners:
                (dep,) = owners
                if name not in self.top_defs:
                    text = f"{label}: {name} is created by a global statement in {dep}"
                    self.report(self.source, use.line, "global-cross", text)
                names = edges.setdefault(dep, {})
                names[name] = names.get(name, True) and use.ann
                lines.setdefault((dep, name), use.line)
            elif name in self.import_names:
                used.add(name)
            elif not use.soft:
                self.report(self.source, use.line, "undefined-name", f"{label}: {name}")
        return edges, lines, used

    def break_cycles(self) -> None:
        graph = {m: set(i.edges) for m, i in self.infos.items()}
        comp = components(graph)
        for mod, info in self.infos.items():
            for dep, names in info.edges.items():
                if comp[mod] == comp[dep] and all(names.values()):
                    info.checking.add(dep)
        runtime = {m: set(i.edges) - i.checking for m, i in self.infos.items()}
        comp = components(runtime)
        groups: dict[int, list[str]] = {}
        for mod in sorted(runtime):
            groups.setdefault(comp[mod], []).append(mod)
        for members in groups.values():
            if len(members) < 2:
                continue
            path = find_cycle(runtime, members)
            parts = []
            for a, b in pairwise(path):
                names = ", ".join(sorted(self.infos[a].edges[b]))
                parts.append(f"{a} -> {b} ({names})")
            first = self.infos[path[0]]
            line = first.lines.get((path[1], min(first.edges[path[1]])), 0)
            self.report(self.source, line, "import-cycle", "; ".join(parts))

    # generated files

    def kept_imports(self, used: set[str]) -> list[str]:
        out: list[str] = []
        for stmt in self.stmts:
            if stmt.kind != "import":
                continue
            node = stmt.node
            assert isinstance(node, ast.Import | ast.ImportFrom)
            text = render_import(node, used)
            if text is not None:
                out.append(text)
                self.kept.add(stmt.start)
        return out

    def body_of(self, stmts: list[Stmt]) -> tuple[list[str], Spans]:
        lines: list[str] = []
        spans: Spans = []
        for index, stmt in enumerate(stmts):
            first = stmt.lead
            if index == 0:
                while first < stmt.start and not self.lines[first - 1].strip():
                    first += 1
            spans.append((first, stmt.end, len(lines)))
            lines.extend(self.lines[first - 1 : stmt.end])
        return lines, spans

    def build_module(self, info: ModInfo) -> None:
        path = f"{self.pkg}/{info.name}.py"
        ext = self.kept_imports(info.used)
        siblings: list[str] = []
        checking: list[str] = []
        for dep in sorted(info.edges):
            names = sorted(info.edges[dep])
            if dep in info.checking:
                checking.append(format_from(f".{dep}", names, "    "))
            else:
                siblings.append(format_from(f".{dep}", names))
        if checking and "TYPE_CHECKING" not in info.used:
            ext.append("from typing import TYPE_CHECKING")
        blocks = [docstring(self.modules[info.name]["doc"])]
        if self.future_line:
            blocks.append(self.future_line)
        for group in (ext, siblings):
            if group:
                blocks.append("\n".join(group))
        if checking:
            blocks.append("if TYPE_CHECKING:\n" + "\n".join(checking))
        body, spans = self.body_of(info.stmts)
        head = "\n\n".join(blocks)
        gap = 2 if isinstance(info.stmts[0].node, Func | ast.ClassDef) else 1
        base = head.count("\n") + 1 + gap + 1
        text = head + "\n" * (gap + 1) + "\n".join(body) + "\n"
        self.files[path] = text
        shown = [*ext, *siblings, *(c.strip() for c in checking)]
        self.stats[info.name] = {
            "statements": len(info.stmts),
            "lines": text.count("\n"),
            "imports": shown,
        }
        for stmt in info.stmts:
            for line, category, shown_text in scan_hazards(
                stmt.node, self.text, self.dotted
            ):
                new = map_line(spans, base, line)
                self.report(path, new, category, shown_text)

    def build_main(self) -> None:
        stmt = self.main_stmt
        if stmt is None:
            return
        node = stmt.node
        assert isinstance(node, ast.If)
        first_body = node.body[0]
        if first_body.lineno == node.lineno:
            text = "the main block body starts on the if line"
            self.report(self.source, node.lineno, "main-inline", text)
            return
        indent = first_body.col_offset
        low, high = node.lineno + 1, stmt.end
        while low < high and not self.lines[low - 1].strip():
            low += 1
        rows = multiline_rows(self.text)
        if rows is None:
            self.report(self.source, low, "main-dedent", "cannot tokenize the source")
            return
        interior = {n for a, b in rows for n in range(a + 1, b + 1)}
        out: list[str] = []
        for number in range(low, high + 1):
            line = self.lines[number - 1]
            if not line.strip():
                out.append("")
            elif line[:indent].strip() or number in interior:
                text = "line is less indented or inside a multi-line string"
                self.report(self.source, number, "main-dedent", text)
                out.append(line)
            else:
                out.append(line[indent:])
        path = f"{self.pkg}/__main__.py"
        ext = self.kept_imports(self.main_used)
        siblings = [
            format_from(f".{dep}", sorted(names))
            for dep, names in sorted(self.main_edges.items())
        ]
        blocks = ["\n".join(group) for group in (ext, siblings) if group]
        prefix = "\n\n".join(blocks)
        body_text = "\n".join(out)
        if blocks:
            self.files[path] = prefix + "\n\n" + body_text + "\n"
            base = prefix.count("\n") + 3
        else:
            self.files[path] = body_text + "\n"
            base = 1
        spans: Spans = [(low, high, 0)]
        for inner in node.body:
            for line, category, shown_text in scan_hazards(
                inner, self.text, self.dotted
            ):
                new = map_line(spans, base, line)
                self.report(path, new, category, shown_text)

    def scan_repo(self) -> Usage:
        stem = self.dotted.rsplit(".", 1)[-1]
        parent = self.dotted.rpartition(".")[0]
        basenames = Counter(Path(f).name for f in self.tracked if f.endswith(".py"))
        target = Target(
            self.dotted,
            stem,
            parent,
            self.source.rpartition("/")[0],
            basenames[stem + ".py"] == 1,
        )
        usage = Usage()
        for rel in sorted(self.tracked):
            if not rel.endswith(".py") or rel == self.source:
                continue
            path = self.root / rel
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if stem not in text:
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError) as exc:
                self.report(rel, 0, "parse-error", f"{type(exc).__name__}: {exc}")
                continue
            scanner = FileScanner(rel, tree, target)
            scanner.run()
            self.merge(rel, scanner, usage)
        return usage

    def merge(self, rel: str, scanner: FileScanner, usage: Usage) -> None:
        for name, lines in scanner.names.items():
            for line in sorted(lines):
                usage.names.setdefault(name, []).append((rel, line))
        for line, name in scanner.mutations:
            owners = sorted(self.defs.get(name, set()))
            where = f"defined in {', '.join(owners)}" if owners else "not defined"
            self.report(rel, line, "outside-mutation", f"{name} ({where})")
        for line, text in scanner.unresolved:
            self.report(rel, line, "unresolved-use", text)
        for line in scanner.star:
            self.report(rel, line, "unresolved-use", "star import of the module")
            usage.star = True

    def build_facade(self, usage: Usage) -> None:
        names = set(usage.names)
        for name in self.plan.get("facade_extra", []):
            if name in self.defs or name in self.import_names:
                names.add(name)
            else:
                self.report(self.source, 0, "facade-extra-unknown", name)
        if usage.star:
            names |= {n for n in self.defs if not n.startswith("_")}
        by_mod: dict[str, list[str]] = {}
        reexport: set[str] = set()
        for name in sorted(names):
            owners = self.defs.get(name)
            if owners and len(owners) == 1:
                by_mod.setdefault(next(iter(owners)), []).append(name)
            elif owners:
                where = ", ".join(sorted(owners))
                text = f"facade: {name} is defined in {where}"
                self.report(self.source, 0, "ambiguous-name", text)
                continue
            elif name in self.import_names:
                reexport.add(name)
            else:
                for file, line in usage.names.get(name, [(self.source, 0)]):
                    self.report(file, line, "export-unknown", name)
                continue
            self.exports.append(name)
            if name.startswith("_"):
                for file, line in usage.names.get(name, []):
                    self.report(file, line, "private-export", name)
        blocks: list[str] = []
        if self.doc_stmt is not None:
            doc = self.doc_stmt
            blocks.append("\n".join(self.lines[doc.start - 1 : doc.end]))
        if self.future_line:
            blocks.append(self.future_line)
        reexported = self.kept_imports(reexport)
        if reexported:
            blocks.append("\n".join(reexported))
        lines = [format_from(f".{mod}", ns) for mod, ns in sorted(by_mod.items())]
        if lines:
            blocks.append("\n".join(lines))
        self.exports.sort()
        if self.exports:
            items = "".join(f'    "{name}",\n' for name in self.exports)
            blocks.append(f"__all__ = [\n{items}]")
        path = f"{self.pkg}/__init__.py"
        self.files[path] = "\n\n".join(blocks) + "\n" if blocks else ""

    def finish(self) -> None:
        for path, text in sorted(self.files.items()):
            try:
                ast.parse(text)
            except (SyntaxError, ValueError) as exc:
                self.report(path, 0, "generated-parse-error", str(exc))
        for stmt in self.stmts:
            if stmt.kind == "import" and stmt.start not in self.kept:
                text = "\n".join(self.lines[stmt.start - 1 : stmt.end])
                self.report(self.source, stmt.start, "import-dropped", text)


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    source = plan["source"]
    modules = plan["modules"]
    if not isinstance(source, str) or not source.endswith(".py"):
        raise ValueError("source must be a .py path")
    if not isinstance(modules, dict) or not modules:
        raise ValueError("modules must be a non-empty object")
    for name, spec in modules.items():
        reserved = name in ("__init__", "__main__")
        if not name.isidentifier() or keyword.iskeyword(name) or reserved:
            raise ValueError(f"bad module name: {name}")
        take = spec["take"]
        if not isinstance(spec["doc"], str) or not isinstance(take, list):
            raise ValueError(f"module {name}: doc must be text and take a list")
        if not all(isinstance(entry, str) for entry in take):
            raise ValueError(f"module {name}: take entries must be text")
    extra = plan.get("facade_extra", [])
    if not isinstance(extra, list) or not all(isinstance(n, str) for n in extra):
        raise ValueError("facade_extra must be a list of names")
    main_module = plan.get("main_module")
    if main_module is not None and main_module not in modules:
        raise ValueError(f"main_module {main_module} is not a module of the plan")
    return plan


def apply_plan(root: Path, source: str, files: dict[str, str]) -> None:
    git(root, "rm", "-q", "--", source)
    for rel, text in sorted(files.items()):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "--", *sorted(files))


def build_report(splitter: Splitter) -> dict[str, Any]:
    reports = [
        {"file": f, "line": n, "category": c, "text": t}
        for f, n, c, t in sorted(set(splitter.reports))
    ]
    return {
        "plan": splitter.plan,
        "modules": {name: splitter.stats[name] for name in sorted(splitter.stats)},
        "exports": splitter.exports,
        "reports": reports,
    }


def summary(report: dict[str, Any]) -> str:
    lines = ["modules:"]
    for name, info in report["modules"].items():
        count = f"{info['lines']} lines, {info['statements']} statements"
        lines.append(f"  {name}: {count}")
    lines.append(f"exports: {len(report['exports'])}")
    lines.append("reports:")
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in report["reports"]:
        by_category.setdefault(item["category"], []).append(item)
    for category, items in sorted(by_category.items()):
        lines.append(f"  {category}: {len(items)}")
    for category, items in sorted(by_category.items()):
        lines.append(f"first {min(len(items), SUMMARY_LIMIT)} of {category}:")
        for item in items[:SUMMARY_LIMIT]:
            lines.append(f"  {item['file']}:{item['line']}: {item['text']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split a module into a package.")
    parser.add_argument("plan", type=Path, help="JSON plan file")
    parser.add_argument("--apply", action="store_true", help="write the package")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--root", type=Path, help="repository root")
    args = parser.parse_args(argv)
    sys.setrecursionlimit(10000)
    try:
        plan = load_plan(args.plan)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"split_module: cannot read the plan: {exc}", file=sys.stderr)
        return 2
    try:
        top = args.root or git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        root = Path(top).resolve()
        tracked = {f for f in git(root, "ls-files", "-z").split("\0") if f}
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"split_module: not a git repository: {exc}", file=sys.stderr)
        return 2
    splitter = Splitter(root, plan, tracked)
    splitter.run(args.apply)
    report = build_report(splitter)
    if args.report:
        text = json.dumps(report, indent=2, ensure_ascii=False)
        args.report.write_text(text + "\n", encoding="utf-8")
    if splitter.failed():
        for file, line, category, text in sorted(set(splitter.reports)):
            if category in FATAL:
                where = f"{file}:{line}: {category}: {text}"
                print(f"split_module: {where}", file=sys.stderr)
        return 1
    if args.apply:
        try:
            apply_plan(root, splitter.source, splitter.files)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"split_module: apply failed: {exc}", file=sys.stderr)
            return 1
    print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
