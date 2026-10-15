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
  allow          {"token": T, "file": F, "pattern": RE, "reason": WHY}: accept
                 token T (in file F only, when given) or every token that
                 matches the regular expression RE; the reason is required

Skipped: tokens with glob or placeholder characters (* < { $ NAME NNNN ...).
Stripped: :LINE, #anchor and trailing punctuation. A dotted module citation
such as tools/pkg/mod.func resolves when tools/pkg/mod.py is tracked.
Suggestions come from the "moves" tables of tools/devtools/{relocate,move}-*.json
and from unique file names.

Exit status: 0 when clean, 1 when stale references exist, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CONFIG = "tools/devtools/pathcheck.json"
MAP_GLOBS = ("tools/devtools/relocate-*.json", "tools/devtools/move-*.json")
PLACEHOLDER = re.compile(r"[*<{$]|NAME|NNNN|\.\.\.")
BASENAME_LIMIT = 3


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
        for f in files:
            parts = f.split("/")
            self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
            self.by_name.setdefault(parts[-1], []).append(f)

    def has(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def roots(self) -> set[str]:
        return {d for d in self.dirs if "/" not in d}


class Config:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.retired = [str(r).strip("/") for r in spec.get("retired_roots", [])]
        self.exclude = [str(p) for p in spec.get("exclude", [])]
        self.generated = [str(p).rstrip("/") for p in spec.get("generated", [])]
        self.allow_tokens: list[tuple[str, str]] = []
        self.allow_patterns: list[re.Pattern[str]] = []
        for item in spec.get("allow", []):
            if not str(item.get("reason", "")).strip():
                raise ValueError(f"allow entry without a reason: {item}")
            if "pattern" in item:
                self.allow_patterns.append(re.compile(item["pattern"]))
            elif "token" in item:
                self.allow_tokens.append((str(item.get("file", "")), item["token"]))
            else:
                raise ValueError(f"allow entry needs token or pattern: {item}")

    def is_generated(self, token: str) -> bool:
        parts = token.split("/")
        heads = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        return any(
            fnmatch.fnmatchcase(h, g) or h == g for g in self.generated for h in heads
        )

    def is_allowed(self, rel: str, token: str) -> bool:
        if any(t == token and f in ("", rel) for f, t in self.allow_tokens):
            return True
        return any(p.fullmatch(token) for p in self.allow_patterns)


def load_moves(root: Path, tracked: set[str]) -> dict[str, str]:
    moves: dict[str, str] = {}
    for rel in sorted(tracked):
        if not any(fnmatch.fnmatchcase(rel, g) for g in MAP_GLOBS):
            continue
        try:
            table = json.loads((root / rel).read_text(encoding="utf-8"))["moves"]
            moves.update(
                {str(k).rstrip("/"): str(v).rstrip("/") for k, v in table.items()}
            )
        except (OSError, ValueError, KeyError, AttributeError):
            continue
    return moves


def resolves(index: Index, cfg: Config, token: str) -> bool:
    token = token.rstrip("/")
    if index.has(token) or cfg.is_generated(token):
        return True
    # A dotted module citation: tools/pkg/mod.func -> tools/pkg/mod.py
    head = token
    while "." in head.rpartition("/")[2]:
        head = head.rpartition(".")[0]
        if f"{head}.py" in index.files or head in index.dirs:
            return True
    return False


def suggest(index: Index, moves: dict[str, str], token: str) -> str:
    token = token.rstrip("/")
    for old in sorted(moves, key=lambda o: (-len(o), o)):
        if covers(old, token):
            new = moves[old] + token[len(old) :]
            if index.has(new):
                return f"{new} (map)"
    name = token.rpartition("/")[2]
    found = index.by_name.get(name, [])
    if 0 < len(found) <= BASENAME_LIMIT:
        return " | ".join(sorted(found)) + " (name)"
    return ""


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def token_regex(roots: set[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(r) for r in sorted(roots, key=lambda r: (-len(r), r)))
    return re.compile(rf"(?<![\w./-])((?:{alt})/[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-/])")


def scan_text(
    rel: str, text: str, tok: re.Pattern[str], index: Index, cfg: Config
) -> list[tuple[int, str]]:
    stale: list[tuple[int, str]] = []
    for n, line in enumerate(text.split("\n"), 1):
        for m in tok.finditer(line):
            token = m[1]
            follow = line[m.end() : m.end() + 1]
            if PLACEHOLDER.search(token) or (follow and follow in "*<{$"):
                continue
            if resolves(index, cfg, token) or cfg.is_allowed(rel, token):
                continue
            stale.append((n, token))
    return stale


def find_stale(
    root: Path, cfg: Config, only: list[str] | None = None
) -> tuple[list[dict[str, Any]], Index]:
    files = [f for f in git(root, "ls-files", "-z").split("\0") if f]
    index = Index(files)
    tok = token_regex(index.roots() | set(cfg.retired))
    moves = load_moves(root, index.files)
    rows: list[dict[str, Any]] = []
    for rel in sorted(files):
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
        for line, token in scan_text(rel, data.decode("utf-8"), tok, index, cfg):
            rows.append(
                {
                    "file": rel,
                    "line": line,
                    "token": token,
                    "suggestion": suggest(index, moves, token),
                }
            )
    return rows, index


def render(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    current = ""
    for r in rows:
        if r["file"] != current:
            current = r["file"]
            lines.append(current)
        extra = f"  =>  {r['suggestion']}" if r["suggestion"] else ""
        lines.append(f"  {r['line']}: {r['token']}{extra}")
    files = len({r["file"] for r in rows})
    lines.append(f"stale paths: {len(rows)} in {files} files")
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
    rows, _ = find_stale(root, cfg, args.paths)
    if args.json:
        args.json.write_text(
            json.dumps({"stale": rows}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(render(rows))
    return 1 if rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
