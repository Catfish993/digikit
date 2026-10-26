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
        [--config FILE] [--root DIR]

The config (default tools/devtools/pathcheck.json) has:
  retired_roots  extra top-level names to look for (directories that moved away)
  exclude        files not scanned: a path prefix, or an fnmatch pattern
  generated      paths that are valid although untracked (prefix or fnmatch
                 pattern, matched on every leading part of the token)
  moved_local    {old prefix: new prefix} for ignored local directories that
                 moved (sections/ -> out/sections/dt2-1.16/). The old prefix
                 is a path root; a token under it is historical in history_docs
                 files and elsewhere stale
  history_docs   files (path prefix or fnmatch pattern) that may cite paths that
                 once existed: there a stale token that was ever tracked in the
                 history of HEAD is "historical", not stale
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
from pathlib import Path
from typing import Any

CONFIG = "tools/devtools/pathcheck.json"
PLACEHOLDER = re.compile(r"[*<{$]|NAME|NNNN|\.\.\.")
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
        self.sorted_files = sorted(files)
        for f in files:
            parts = f.split("/")
            self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))

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

    def is_moved(self, path: str) -> bool:
        """Is the path under a moved local root?"""
        return any(path.startswith(old) for old in self.moved)

    def is_history_doc(self, rel: str) -> bool:
        return any(matches(rel, e) for e in self.history_docs)


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


class Scan:
    def __init__(self, root: Path, cfg: Config) -> None:
        self.root = root
        self.cfg = cfg
        self.git = Git(root)
        files = [f for f in git(root, "ls-files", "-z").split("\0") if f]
        self.files = files
        self.index = Index(files)
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
        and not cfg.is_moved(c["path"])
    ]
    ignored = scan.git.ignored(unresolved + [p + "/" for p in unresolved])
    stale: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for rel, c in cands:
        row = {"file": rel, **c}
        if c["ref"]:
            if present[f"{c['ref']}:{c['path'].rstrip('/')}"]:
                continue
            if cfg.is_allowed(rel, c["token"]):
                continue
            stale.append(row)
            continue
        if tracked_resolves(scan.index, c["path"]) or cfg.is_allowed(rel, c["path"]):
            continue
        if cfg.is_moved(c["path"]):
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
        if cfg.is_history_doc(rel) and scan.git.was_tracked(c["path"]):
            historical.append(row)
        else:
            stale.append(row)
    return stale, historical, scan


def render(rows: list[dict[str, Any]], historical: int) -> str:
    lines: list[str] = []
    current = ""
    for r in rows:
        if r["file"] != current:
            current = r["file"]
            lines.append(current)
        lines.append(f"  {r['line']}: {r['token']}")
    files = len({r["file"] for r in rows})
    lines.append(f"stale: {len(rows)} in {files} files; historical: {historical}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find stale repository paths.")
    parser.add_argument("--json", type=Path, help="write the JSON report here")
    parser.add_argument("--paths", nargs="+", help="scan only files under these paths")
    parser.add_argument("--config", type=Path, help=f"config file (default {CONFIG})")
    parser.add_argument("--root", type=Path, help="repository root")
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
    rows, historical, _ = find_stale(root, cfg, args.paths)
    unused = [] if args.paths else cfg.unused_allows()
    if args.json:
        report: dict[str, Any] = {
            "stale": rows,
            "historical": historical,
            "summary": {"stale": len(rows), "historical": len(historical)},
            "unused_allow": unused,
        }
        args.json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(render(rows, len(historical)))
    for item in unused:
        print(f"unused allow: {json.dumps(item, ensure_ascii=False)}")
    return 1 if rows or unused else 0


if __name__ == "__main__":
    raise SystemExit(main())
