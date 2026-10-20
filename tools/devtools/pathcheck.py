#!/usr/bin/env python3
"""Report repository paths in tracked text files that name nothing.

A token is path-like when it starts at one of the repository's top-level
directories (taken from `git ls-files`, plus the retired roots named in the
config) and is not preceded by a word character, ".", "/" or "-". A token is
stale when it is no tracked file or directory, no generated path and not
allowed. It uses only the standard library and imports nothing from the
repository.

Usage:
    uv run python -m tools.devtools.pathcheck [--json OUT] [--paths P ...]
        [--config FILE] [--root DIR] [--fix] [--fix-deleted REF] [--apply]

The config (default tools/devtools/pathcheck.json) has:
  retired_roots  extra top-level names to look for (directories that moved away)
  exclude        files not scanned: a path prefix, or an fnmatch pattern
  generated      paths that are valid although untracked (prefix or fnmatch
                 pattern, matched on every leading part of the token)
  moved_local    {old prefix: new prefix} for ignored local directories that
                 moved (sections/ -> out/sections/dt2-1.16/). The old prefix
                 is a path root; a token under it is historical in history_docs
                 files and elsewhere stale with the new path as suggestion
  history_docs   files (path prefix or fnmatch pattern) that may cite paths that
                 once existed: there a stale token that was ever tracked in the
                 history of HEAD and has no suggestion is "historical", not stale
  allow          {"token": T, "file": F, "pattern": RE, "reason": WHY}: accept
                 token T or every token that matches the regular expression RE
                 (in file F only, when given); the reason is required

Also valid: a path that a .gitignore rule matches (`git check-ignore`), a
module citation without extension (T.py is tracked), and a path relative to the
directory of the file that cites it (a tracked file or a tracked file name
prefix ending at a dot).

Skipped: tokens with glob or placeholder characters (* < { $ NAME NNNN ...).
Stripped: :LINE, #anchor and trailing punctuation. A dotted module citation
such as tools/pkg/mod.func resolves when tools/pkg/mod.py is tracked.
A token REF:path (REF a tag, branch or commit-ish) is valid when
`git cat-file -e REF:path` succeeds.

Suggestions come from the "moves" tables of tools/devtools/{relocate,move}-*.json
(a null target means deleted: no suggestion), for dotted citations from the
module part of the token, and from a unique tracked file name. `--fix` rewrites
each token that has a suggestion (dry run unless `--apply`). `--fix-deleted REF`
rewrites the other stale tokens outside the history docs to REF:path when the
path exists at REF, else to <deleting-commit>^:path.

Exit status: 0 when clean, 1 when stale references or (in a full scan) unused
allow entries exist, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import bisect
import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONFIG = "tools/devtools/pathcheck.json"
MAP_GLOBS = ("tools/devtools/relocate-*.json", "tools/devtools/move-*.json")
PLACEHOLDER = re.compile(r"[*<{$]|NAME|NNNN|\.\.\.")
BASENAME_LIMIT = 3
HISTORY_DOCS = [
    "docs/findings/",
    "docs/plans/",
    "docs/design/",
    "docs/archive/",
    "docs/refs/",
    "docs/contracts/",
    "docs/FINDINGS.md",
]
# A dotted citation tools/pkg/mod.func never ends in a file extension.
EXTENSIONS = {
    "py", "rs", "md", "json", "toml", "txt", "sh", "js", "ts", "mjs", "cjs", "tsx",
    "yaml", "yml", "lock", "syx", "bin", "wav", "c", "h", "html", "css", "mdx",
    "astro", "sql", "csv", "png", "svg", "log", "cfg", "ini", "wasm", "jsonl",
}  # fmt: skip
IDENT = re.compile(r"[A-Za-z_]\w*$")
REF_CHARS = r"[A-Za-z0-9][A-Za-z0-9_.\-/^~]*"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout


def covers(base: str, path: str) -> bool:
    return path == base or path.startswith(base + "/")


def matches(path: str, entry: str) -> bool:
    return path.startswith(entry) or fnmatch.fnmatchcase(path, entry)


class Index:
    """Tracked files and directories of the repository."""

    def __init__(self, files: list[str]) -> None:
        self.files = set(files)
        self.dirs: set[str] = set()
        self.by_name: dict[str, list[str]] = {}
        self.sorted_files = sorted(files)
        for f in files:
            parts = f.split("/")
            self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
            self.by_name.setdefault(parts[-1], []).append(f)

    def has(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def prefixed(self, prefix: str) -> bool:
        """Is some tracked file named PREFIX followed by anything?"""
        i = bisect.bisect_left(self.sorted_files, prefix)
        return i < len(self.sorted_files) and self.sorted_files[i].startswith(prefix)

    def roots(self) -> set[str]:
        return {d for d in self.dirs if "/" not in d}


class Config:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.retired = [str(r).strip("/") for r in spec.get("retired_roots", [])]
        self.exclude = [str(p) for p in spec.get("exclude", [])]
        self.generated = [str(p).rstrip("/") for p in spec.get("generated", [])]
        self.moved = {
            str(k).rstrip("/") + "/": str(v).rstrip("/") + "/"
            for k, v in spec.get("moved_local", {}).items()
        }
        self.history_docs = [str(p) for p in spec.get("history_docs", HISTORY_DOCS)]
        self.allow: list[dict[str, Any]] = []
        self.used: set[int] = set()
        for item in spec.get("allow", []):
            if not str(item.get("reason", "")).strip():
                raise ValueError(f"allow entry without a reason: {item}")
            if "pattern" not in item and "token" not in item:
                raise ValueError(f"allow entry needs token or pattern: {item}")
            self.allow.append(
                {
                    "item": item,
                    "file": str(item.get("file", "")),
                    "pattern": re.compile(item["pattern"])
                    if "pattern" in item
                    else None,
                    "token": item.get("token"),
                }
            )

    def is_generated(self, token: str) -> bool:
        parts = token.split("/")
        heads = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        return any(
            fnmatch.fnmatchcase(h, g) or h == g for g in self.generated for h in heads
        )

    def is_allowed(self, rel: str, token: str) -> bool:
        """True when an allow entry accepts the token; every match counts as used."""
        found = False
        for i, e in enumerate(self.allow):
            if e["file"] not in ("", rel):
                continue
            pat = e["pattern"]
            if pat.fullmatch(token) if pat else e["token"] == token:
                self.used.add(i)
                found = True
        return found

    def unused_allows(self) -> list[dict[str, Any]]:
        return [e["item"] for i, e in enumerate(self.allow) if i not in self.used]

    def moved_target(self, path: str) -> str:
        """The new place of a path under a moved local root ("" if none)."""
        for old, new in self.moved.items():
            if path.startswith(old):
                return new + path[len(old) :]
        return ""

    def is_history_doc(self, rel: str) -> bool:
        return any(matches(rel, e) for e in self.history_docs)


def load_moves(root: Path, tracked: set[str]) -> dict[str, str | None]:
    """Old path -> new path; None when the map records a deletion."""
    moves: dict[str, str | None] = {}
    for rel in sorted(tracked):
        if not any(fnmatch.fnmatchcase(rel, g) for g in MAP_GLOBS):
            continue
        try:
            table = json.loads((root / rel).read_text(encoding="utf-8"))["moves"]
            moves.update(
                {
                    str(k).rstrip("/"): None if v is None else str(v).rstrip("/")
                    for k, v in table.items()
                }
            )
        except (OSError, ValueError, KeyError, AttributeError):
            continue
    # file entries that keep their name also move their directory
    dirs: dict[str, set[str]] = {}
    for old, new in moves.items():
        if new and old.rpartition("/")[2] == new.rpartition("/")[2] and "/" in old:
            dirs.setdefault(old.rpartition("/")[0], set()).add(new.rpartition("/")[0])
    for old_dir, targets in dirs.items():
        if len(targets) == 1 and old_dir not in moves:
            moves[old_dir] = next(iter(targets))
    return moves


def tracked_resolves(index: Index, token: str) -> bool:
    """A tracked path, or a dotted module citation of one."""
    token = token.rstrip("/")
    if index.has(token) or f"{token}.py" in index.files:
        return True
    head = token
    while "." in head.rpartition("/")[2]:
        head = head.rpartition(".")[0]
        if f"{head}.py" in index.files or head in index.dirs:
            return True
    return False


def resolves(index: Index, cfg: Config, token: str) -> bool:
    return tracked_resolves(index, token) or cfg.is_generated(token.rstrip("/"))


def splits(path: str) -> list[tuple[str, str]]:
    """(module path, symbol suffix) readings of a path: the whole path first."""
    out = [(path, "")]
    prefix, slash, name = path.rpartition("/")
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        tail = parts[i:]
        if (
            all(parts[:i])
            and all(IDENT.match(t) for t in tail)
            and tail[0] not in EXTENSIONS
        ):
            out.append((prefix + slash + ".".join(parts[:i]), "." + ".".join(tail)))
    return out


def map_path(index: Index, moves: dict[str, str | None], path: str) -> str | None:
    """Follow the move maps (longest prefix first) to a tracked path."""
    keys = sorted(moves, key=lambda o: (-len(o), o))
    for _ in range(4):
        for old in keys:
            if covers(old, path):
                target = moves[old]
                if target is None:
                    return None
                path = target + path[len(old) :]
                break
        else:
            return None
        if index.has(path):
            return path
    return None


def settle(index: Index, head: str, sym: str) -> str | None:
    """head + sym as a citation that resolves; module parts that name files in a
    directory head become path parts (pkg.mod.func -> pkg/mod.func)."""
    if not sym:
        return head if index.has(head) else None
    parts = sym[1:].split(".")
    if head.endswith(".py") and head in index.files:
        head = head[:-3]
    while parts and head in index.dirs:
        if f"{head}/{parts[0]}.py" in index.files:
            head, parts = f"{head}/{parts[0]}", parts[1:]
            if not parts:
                return head + ".py"
        elif f"{head}/{parts[0]}" in index.dirs:
            head, parts = f"{head}/{parts[0]}", parts[1:]
        else:
            break
    final = head + ("." + ".".join(parts) if parts else "")
    return final if tracked_resolves(index, final) else None


def suggest(
    index: Index,
    moves: dict[str, str | None],
    token: str,
    name_ok: Callable[[str, str], bool] | None = None,
) -> tuple[str, str]:
    """(display text, exact replacement token or "" when not safely fixable)."""
    path = token.rstrip("/")
    slash = token[len(path) :]
    if any(
        seg.rpartition(".")[2] in EXTENSIONS
        for seg in path.split("/")[:-1]
        if "." in seg
    ):
        return "", ""  # a list such as a.py/b.py/, not a path
    readings = splits(path)
    for head, sym in readings:
        bare = "." not in head.rpartition("/")[2]
        for cand in (head, head + ".py") if sym or bare else (head,):
            new = map_path(index, moves, cand)
            if new is None:
                continue
            final = settle(index, new, sym)
            if final:
                return f"{final + slash} (map)", final + slash
    for head, sym in readings:
        name = head.rpartition("/")[2] + (".py" if sym else "")
        found = index.by_name.get(name, [])
        if len(found) == 1:
            old = head + (".py" if sym else "")
            if name_ok is not None and not name_ok(old, found[0]):
                continue
            final = settle(index, found[0], sym)
            if final:
                return f"{final + slash} (name)", final + slash
        elif not sym and 1 < len(found) <= BASENAME_LIMIT:
            return " | ".join(sorted(found)) + " (name)", ""
    return "", ""


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def token_regex(roots: set[str]) -> re.Pattern[str]:
    """Groups: 1 optional ref, 2 path (a path alone is the same without 1)."""
    alt = "|".join(re.escape(r) for r in sorted(roots, key=lambda r: (-len(r), r)))
    path = rf"((?:{alt})/[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-/])"
    return re.compile(rf"(?<![\w./-])(?:({REF_CHARS}):)?{path}")


class Git:
    """Cached questions to git: object existence in one batch process, history."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.known: dict[str, bool] = {}
        self._history: set[str] | None = None
        self._deleted: dict[str, str] = {}
        self._added: dict[str, set[str]] = {}
        self._ignored: dict[str, bool] = {}

    def exist(self, names: list[str]) -> dict[str, bool]:
        """Which object names (REF:path, REF^{commit}) exist: one cat-file run."""
        todo = [n for n in dict.fromkeys(names) if n not in self.known]
        if todo:
            done = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "--batch-check"],
                input="".join(n + "\n" for n in todo),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            lines = done.stdout.split("\n")
            for i, n in enumerate(todo):
                line = lines[i] if i < len(lines) else "missing"
                self.known[n] = not (line.endswith(" missing") or "ambiguous" in line)
        return {n: self.known[n] for n in names}

    def ref_ok(self, ref: str) -> bool:
        return self.exist([f"{ref}^{{commit}}"])[f"{ref}^{{commit}}"]

    @property
    def history(self) -> set[str]:
        """Every path (and directory) ever tracked in the history of HEAD."""
        if self._history is None:
            out = git(self.root, "log", "--format=", "--name-only", "-z", "HEAD")
            names = {n for n in out.split("\0") if n.strip()}
            full = set(names)
            for n in names:
                parts = n.split("/")
                full.update("/".join(parts[:i]) for i in range(1, len(parts)))
            self._history = full
        return self._history

    def was_tracked(self, path: str) -> bool:
        for head, sym in splits(path.rstrip("/")):
            if head in self.history or (sym and head + ".py" in self.history):
                return True
        return False

    def ignored(self, paths: list[str]) -> set[str]:
        """Which of the paths a .gitignore rule matches (one check-ignore run).

        A path below a symlinked directory (a lane's `out/` links the main tree's) is asked as that directory: git
        refuses a path "beyond a symbolic link" and drops the rest of the batch."""
        todo = sorted(set(paths) - set(self._ignored))
        if todo:
            probe = {p: self._below_symlink(p) or p for p in todo}
            asked = sorted(set(probe.values()))
            done = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "--no-index"]
                + ["--stdin", "-z"],
                input="".join(p + "\0" for p in asked),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            hit = {p for p in done.stdout.split("\0") if p}
            for p in todo:
                self._ignored[p] = probe[p] in hit
        return {p for p in paths if self._ignored[p]}

    def _below_symlink(self, path: str) -> str:
        """The nearest symlinked ancestor directory of PATH ("" if none); `dir/` counts its own symlink too."""
        parts = path.rstrip("/").split("/")
        for i in range(1, len(parts) + path.endswith("/")):
            head = "/".join(parts[:i])
            if (self.root / head).is_symlink():
                return head
        return ""

    def deleted_by(self, path: str) -> str:
        """Full hash of the commit that last deleted the path ("" if none)."""
        key = "H:" + path
        if key not in self._deleted:
            self._deleted[key] = git(
                self.root,
                *("log", "-1", "--format=%H", "--diff-filter=D", "--no-renames"),
                "--",
                path,
            ).strip()
        return self._deleted[key]

    def history_of(self, path: str) -> set[str]:
        """Commits that added, renamed or copied the path (followed)."""
        if path not in self._added:
            out = git(
                self.root,
                *("log", "--follow", "--diff-filter=AR", "--format=%H", "--", path),
            )
            self._added[path] = set(out.split())
        return self._added[path]

    def same_file(self, old: str, new: str) -> bool:
        """Was `new` plausibly where `old` went: old never existed, or the
        commit that deleted old also created new."""
        if old not in self.history:
            return True
        gone = self.deleted_by(old)
        return bool(gone) and gone in self.history_of(new)

    def deleting_commit(self, path: str) -> str:
        if path not in self._deleted:
            self._deleted[path] = git(
                self.root, "log", "-1", "--format=%h", "--diff-filter=D", "--", path
            ).strip()
        return self._deleted[path]


class Scan:
    def __init__(self, root: Path, cfg: Config) -> None:
        self.root = root
        self.cfg = cfg
        self.git = Git(root)
        files = [f for f in git(root, "ls-files", "-z").split("\0") if f]
        self.files = files
        self.index = Index(files)
        self.moves = load_moves(root, self.index.files)
        roots = self.index.roots() | set(cfg.retired)
        roots |= {o.rstrip("/") for o in cfg.moved}
        self.tok = token_regex(roots)
        self.plain = re.compile(
            self.tok.pattern.replace(rf"(?:({REF_CHARS}):)?", "(?:())?", 1)
        )


def candidates(scan: Scan, rel: str, text: str) -> list[dict[str, Any]]:
    """Path-like tokens of a text: line, col, token, ref ("" for a bare path), path."""
    out: list[dict[str, Any]] = []
    for n, line in enumerate(text.split("\n"), 1):
        pos = 0
        while True:
            m = scan.tok.search(line, pos)
            if not m:
                break
            ref = m[1] or ""
            if ref and not scan.git.ref_ok(ref):
                # not a ref: read the same text again as bare paths
                m = scan.plain.search(line, m.start())
                if not m:
                    break
                ref = ""
            pos = m.end()
            path = m[2]
            follow = line[m.end() : m.end() + 1]
            if PLACEHOLDER.search(path) or (follow and follow in "*<{$"):
                continue
            token = f"{ref}:{path}" if ref else path
            start = m.start() if ref else m.start(2)
            out.append(
                {"line": n, "col": start, "token": token, "ref": ref, "path": path}
            )
    return out


def relative_ok(index: Index, rel: str, path: str) -> bool:
    """A path relative to the directory of the citing file."""
    here = rel.rpartition("/")[0]
    if not here:
        return False
    full = f"{here}/{path.rstrip('/')}"
    return index.has(full) or index.prefixed(full + ".")


def find_stale(
    root: Path, cfg: Config, only: list[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Scan]:
    """(stale rows, historical rows, scan state)."""
    scan = Scan(root, cfg)
    cands: list[tuple[str, dict[str, Any]]] = []
    for rel in sorted(scan.files):
        if any(matches(rel, e) for e in cfg.exclude):
            continue
        if only and not any(covers(p.rstrip("/"), rel) for p in only):
            continue
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if not is_text(data):
            continue
        cands.extend((rel, c) for c in candidates(scan, rel, data.decode("utf-8")))
    refs = [f"{c['ref']}:{c['path'].rstrip('/')}" for _, c in cands if c["ref"]]
    present = scan.git.exist(refs)
    unresolved = [
        c["path"].rstrip("/")
        for rel, c in cands
        if not c["ref"]
        and not resolves(scan.index, cfg, c["path"])
        and not cfg.moved_target(c["path"])
    ]
    ignored = scan.git.ignored(unresolved + [p + "/" for p in unresolved])
    stale: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for rel, c in cands:
        row = {"file": rel, **c, "suggestion": "", "fix": ""}
        if c["ref"]:
            if present[f"{c['ref']}:{c['path'].rstrip('/')}"]:
                continue
            if cfg.is_allowed(rel, c["token"]):
                continue
            stale.append(row)
            continue
        if tracked_resolves(scan.index, c["path"]) or cfg.is_allowed(rel, c["path"]):
            continue
        moved = cfg.moved_target(c["path"])
        if moved:
            row["suggestion"], row["fix"] = f"{moved} (moved)", moved
            (historical if cfg.is_history_doc(rel) else stale).append(row)
            continue
        if (
            resolves(scan.index, cfg, c["path"])
            or cfg.is_allowed(rel, c["path"])
            or relative_ok(scan.index, rel, c["path"])
            or c["path"].rstrip("/") in ignored
            or c["path"].rstrip("/") + "/" in ignored
        ):
            continue
        row["suggestion"], row["fix"] = suggest(
            scan.index, scan.moves, c["path"], scan.git.same_file
        )
        if (
            not row["fix"]
            and cfg.is_history_doc(rel)
            and scan.git.was_tracked(c["path"])
        ):
            historical.append(row)
        else:
            stale.append(row)
    return stale, historical, scan


def plan_fix_deleted(
    scan: Scan, rows: list[dict[str, Any]], ref: str
) -> list[dict[str, Any]]:
    """Rewrites for stale tokens without a fix, outside the history docs."""
    todo = [
        r
        for r in rows
        if not r["fix"] and not r["ref"] and not scan.cfg.is_history_doc(r["file"])
    ]
    paths = sorted({r["path"].rstrip("/") for r in todo})
    at_ref = scan.git.exist([f"{ref}:{p}" for p in paths])
    plan: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    for r in todo:
        p = r["path"].rstrip("/")
        if at_ref[f"{ref}:{p}"]:
            plan.append({**r, "new": f"{ref}:{p}", "kind": "ref"})
        elif len(splits(p)) == 1 and scan.git.was_tracked(p):
            later.append(r)  # a dotted citation cannot be quoted at a commit
    commits = {
        p: scan.git.deleting_commit(p)
        for p in sorted({r["path"].rstrip("/") for r in later})
    }
    there = scan.git.exist([f"{h}^:{p}" for p, h in commits.items() if h])
    for r in later:
        p = r["path"].rstrip("/")
        h = commits[p]
        if h and there[f"{h}^:{p}"]:
            plan.append({**r, "new": f"{h}^:{p}", "kind": "deleting-commit"})
    return plan


def plan_fix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "new": r["fix"], "kind": "suggestion"} for r in rows if r["fix"]]


def apply_plan(root: Path, plan: list[dict[str, Any]]) -> int:
    """Replace the exact token spans; a span whose text changed is skipped."""
    by_file: dict[str, list[dict[str, Any]]] = {}
    for p in plan:
        by_file.setdefault(p["file"], []).append(p)
    done = 0
    for rel, items in by_file.items():
        path = root / rel
        lines = path.read_bytes().decode("utf-8").split("\n")
        for p in sorted(items, key=lambda p: (p["line"], -p["col"])):
            line = lines[p["line"] - 1]
            col, tok = p["col"], p["token"]
            if line[col : col + len(tok)] != tok:
                continue
            new = p["new"] + (
                "/" if tok.endswith("/") and not p["new"].endswith("/") else ""
            )
            lines[p["line"] - 1] = line[:col] + new + line[col + len(tok) :]
            done += 1
        path.write_bytes("\n".join(lines).encode("utf-8"))
    return done


def sample(plan: list[dict[str, Any]], n: int = 20) -> list[dict[str, Any]]:
    step = max(1, len(plan) // n)
    return plan[::step][:n]


def render(rows: list[dict[str, Any]], historical: int) -> str:
    lines: list[str] = []
    current = ""
    for r in rows:
        if r["file"] != current:
            current = r["file"]
            lines.append(current)
        extra = f"  =>  {r['suggestion']}" if r["suggestion"] else ""
        lines.append(f"  {r['line']}: {r['token']}{extra}")
    files = len({r["file"] for r in rows})
    fixable = sum(1 for r in rows if r["fix"])
    lines.append(
        f"stale: {len(rows)} in {files} files; historical: {historical}; "
        f"fixable: {fixable}"
    )
    return "\n".join(lines)


def brief(p: dict[str, Any]) -> dict[str, Any]:
    keys = ("file", "line", "col", "token", "new", "kind")
    return {k: p[k] for k in keys}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find stale repository paths.")
    parser.add_argument("--json", type=Path, help="write the JSON report here")
    parser.add_argument("--paths", nargs="+", help="scan only files under these paths")
    parser.add_argument("--config", type=Path, help=f"config file (default {CONFIG})")
    parser.add_argument("--root", type=Path, help="repository root")
    parser.add_argument(
        "--fix", action="store_true", help="rewrite tokens that have a suggestion"
    )
    parser.add_argument(
        "--fix-deleted",
        metavar="REF",
        help="rewrite other stale tokens (not in history docs) to REF:path or "
        "<deleting-commit>^:path",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the rewrites (default: dry run)"
    )
    args = parser.parse_args(argv)
    try:
        top = args.root or git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        root = Path(top).resolve()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"pathcheck: not a git repository: {exc}", file=sys.stderr)
        return 2
    config = args.config or root / CONFIG
    try:
        spec = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
        cfg = Config(spec)
    except (OSError, ValueError, TypeError, AttributeError, re.error) as exc:
        print(f"pathcheck: cannot read the config: {exc}", file=sys.stderr)
        return 2
    rows, historical, scan = find_stale(root, cfg, args.paths)
    unused = [] if args.paths else cfg.unused_allows()
    plans: dict[str, list[dict[str, Any]]] = {}
    try:
        if args.fix:
            plans["fix"] = plan_fix(rows)
        if args.fix_deleted:
            plans["fix-deleted"] = plan_fix_deleted(scan, rows, args.fix_deleted)
    except subprocess.CalledProcessError as exc:
        print(f"pathcheck: git failed: {exc}", file=sys.stderr)
        return 2
    planned = {
        id_
        for p in plans.values()
        for id_ in ((q["file"], q["line"], q["col"]) for q in p)
    }
    remaining = [r for r in rows if (r["file"], r["line"], r["col"]) not in planned]
    if args.json:
        report: dict[str, Any] = {
            "stale": rows,
            "historical": historical,
            "summary": {
                "stale": len(rows),
                "historical": len(historical),
                "fixable": sum(1 for r in rows if r["fix"]),
            },
            "unused_allow": unused,
        }
        if plans:
            report["plans"] = {k: [brief(p) for p in v] for k, v in plans.items()}
            report["remaining"] = remaining
        args.json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(render(rows, len(historical)))
    for item in unused:
        print(f"unused allow: {json.dumps(item, ensure_ascii=False)}")
    for mode, plan in plans.items():
        kinds: dict[str, int] = {}
        for p in plan:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        print(f"{mode}: {len(plan)} rewrites {kinds}")
        for p in sample(plan):
            print(f"  {p['file']}:{p['line']}: {p['token']} -> {p['new']}")
    if plans:
        print(f"remaining stale after the rewrites: {len(remaining)}")
        if args.apply:
            for plan in plans.values():
                print(f"applied: {apply_plan(root, plan)}")
        else:
            print("dry run: pass --apply to write")
    return 1 if rows or unused else 0


if __name__ == "__main__":
    raise SystemExit(main())
