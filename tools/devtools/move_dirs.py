#!/usr/bin/env python3
"""Move directories and files with git mv and rewrite the paths that name them.

Reads a JSON map of moves (old to new path, each a tracked directory or file)
and rewrites path prefixes in tracked text files. The tool is language-neutral:
it knows nothing about imports. What it cannot change safely it reports
instead of editing. It uses only the standard library and imports nothing
from the repository.

Usage:
    uv run python tools/devtools/move_dirs.py MAP.json [--apply]
        [--report REPORT.json] [--root DIR] [--rewrite-only] [--skip-missing]
        [--exclude PREFIX ...]

MAP.json has "moves" (old to new path) and "exclude_text" (path prefixes whose
files are not rewritten; occurrences in them are only counted). Without
--apply nothing changes: every edit and report is computed in memory, written
to the report file and summarised. With --apply, and only when every check
passes, the paths are moved with git mv, the rewritten contents are written to
the new paths and the changes are staged.

A path OLD is rewritten when it is not preceded by a word character, ".", "/"
or "-" and not followed by a word character, "." or "-" (so out/OLD and
OLDER are left alone). Longest paths go first and each file is scanned once,
so a replacement is never scanned again. Reports (no edits): relative-ref
(OLD after ../ or ./), glob-ref (PARENT/*), bare-parent (PARENT/ as a token,
or the quoted name of a parent path component in a source file) and the
counts per excluded file.

--rewrite-only replays the rewrites of moves that are already done: nothing is
moved. Every entry needs its old path gone (not tracked, not on disk) and its
new path tracked, else a check fails (--skip-missing instead drops entries whose new path is not
tracked, for example a module that became a package, and lists them). The map may carry other keys (for
example "packages"); they are ignored. The map files tools/devtools/
{relocate,move}-*.json are never rewritten in this mode. --exclude PREFIX adds
a path prefix to "exclude_text" for this run (repeatable).

Exit status: 0 on success, 1 when a check fails (nothing is changed), 2 on
usage errors.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SELF = "tools/devtools/move_dirs.py"
SUMMARY_LIMIT = 10
CODE_SUFFIXES = {".py", ".rs", ".mjs", ".ts", ".js", ".json", ".toml"}
CLIP = 300
MAP_FILES = re.compile(r"tools/devtools/(?:relocate|move)-[^/]*\.json")


class Patterns:
    """Regular expressions derived from the move map."""

    def __init__(self, moves: dict[str, str]) -> None:
        olds = sorted(moves, key=lambda o: (-len(o), o))
        alt = "|".join(re.escape(o) for o in olds)
        self.rewrite = re.compile(rf"(?<![\w./-])({alt})(?![\w.-])")
        self.relative = re.compile(rf"(?<![\w-])(?:\.{{1,2}}/)+({alt})(?![\w.-])")
        parents = {o.rpartition("/")[0] for o in moves} - {""}
        names = {part for p in parents for part in p.split("/")}
        by_len = sorted(parents, key=lambda p: (-len(p), p))
        palt = "|".join(re.escape(p) for p in by_len)
        nalt = "|".join(re.escape(n) for n in sorted(names))
        self.glob = re.compile(rf"(?<![\w./-])(?:{palt})/\*") if parents else None
        self.parent = (
            re.compile(rf"(?<![\w./-])(?:{palt})/(?![\w.*/-])") if parents else None
        )
        self.quoted = re.compile(rf"([\"'])(?:{nalt})\1") if names else None


def clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= CLIP else text[:CLIP] + "..."


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


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


def new_path(moves: dict[str, str], moves_by_len: list[str], path: str) -> str:
    for old in moves_by_len:
        if covers(old, path):
            return moves[old] + path[len(old) :]
    return path


def check_done_moves(root: Path, moves: dict[str, str], tracked: set[str]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for old, new in sorted(moves.items()):
        # An untracked or ignored leftover directory (build output) is not a source.
        left = root / old
        if any(covers(old, f) for f in tracked) or left.is_file() or left.is_symlink():
            problems.append(f"source still exists: {old}")
        if not any(covers(new, f) for f in tracked):
            problems.append(f"destination is not tracked: {new}")
        if new in seen:
            problems.append(f"duplicate destination {new}: {seen[new]} and {old}")
        seen[new] = old
    return problems


def check_moves(
    root: Path, moves: dict[str, str], tracked: set[str], apply: bool
) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for old, new in sorted(moves.items()):
        if not any(covers(old, f) for f in tracked):
            problems.append(f"source is not tracked: {old}")
        if new in seen:
            problems.append(f"duplicate destination {new}: {seen[new]} and {old}")
        seen[new] = old
        if (root / new).exists() or any(covers(new, f) for f in tracked):
            problems.append(f"destination exists: {new}")
        problems.extend(
            f"source {old} is inside source {other}"
            for other in moves
            if other != old and covers(other, old)
        )
    if apply and git(root, "status", "--porcelain", "--untracked-files=no").strip():
        problems.append("working tree has uncommitted changes to tracked files")
    return problems


class Scan:
    """Edits and reports for one file, computed on its old content."""

    def __init__(self, old: str, new: str, text: str) -> None:
        self.old = old
        self.new = new
        self.text = text
        self.starts = [0, *(m.end() for m in re.finditer("\n", text))]
        self.edits: list[tuple[int, str, str]] = []
        self.reports: list[tuple[int, str, str]] = []
        self.out = text

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def report(self, m: re.Match[str], category: str) -> None:
        line = self.line_of(m.start())
        self.reports.append((line, category, clip(m[0])))


def rewrite_file(scan: Scan, pats: Patterns, moves: dict[str, str]) -> None:
    def sub(m: re.Match[str]) -> str:
        new = moves[m[1]]
        scan.edits.append((scan.line_of(m.start()), m[1], new))
        return new

    scan.out = pats.rewrite.sub(sub, scan.text)
    for m in pats.relative.finditer(scan.text):
        scan.report(m, "relative-ref")
    if pats.glob is not None:
        for m in pats.glob.finditer(scan.text):
            scan.report(m, "glob-ref")
    if pats.parent is not None:
        for m in pats.parent.finditer(scan.text):
            scan.report(m, "bare-parent")
    if pats.quoted is not None and Path(scan.old).suffix in CODE_SUFFIXES:
        for m in pats.quoted.finditer(scan.text):
            scan.report(m, "bare-parent")


def apply_plan(
    root: Path, moves: dict[str, str], scans: dict[str, Scan], rewrite_only: bool
) -> None:
    for old, new in sorted(moves.items()):
        if rewrite_only:
            break
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", old, new)
    # git mv staged the renames. Stage only the rewritten files (all tracked), so
    # untracked or ignored files that moved with a directory stay unstaged.
    rewritten = set()
    for scan in scans.values():
        if scan.out != scan.text:
            (root / scan.new).write_bytes(scan.out.encode("utf-8"))
            rewritten.add(scan.new)
    if rewritten:
        git(root, "add", "--", *sorted(rewritten))


def build_report(
    moves: dict[str, str], scans: dict[str, Scan], excluded: dict[str, int]
) -> dict[str, Any]:
    edits = [
        {"file": s.new, "line": line, "old": old, "new": new}
        for s in scans.values()
        for line, old, new in s.edits
    ]
    edits.sort(key=lambda e: (e["file"], e["line"], e["old"], e["new"]))
    reports = [
        {"file": s.new, "line": line, "category": category, "text": text}
        for s in scans.values()
        for line, category, text in set(s.reports)
    ]
    reports.sort(key=lambda r: (r["file"], r["line"], r["category"], r["text"]))
    return {
        "moves": [[old, new] for old, new in sorted(moves.items())],
        "edits": edits,
        "reports": reports,
        "excluded": dict(sorted(excluded.items())),
    }


def summary(report: dict[str, Any]) -> str:
    edited = {e["file"] for e in report["edits"]}
    lines = [
        f"moves: {len(report['moves'])}",
        f"edited files: {len(edited)}",
        f"edits: {len(report['edits'])}",
        "reports:",
    ]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in report["reports"]:
        by_category.setdefault(item["category"], []).append(item)
    for category, items in sorted(by_category.items()):
        lines.append(f"  {category}: {len(items)}")
    total = sum(report["excluded"].values())
    lines.append(f"  excluded-ref: {total} in {len(report['excluded'])} files")
    for category, items in sorted(by_category.items()):
        lines.append(f"first {min(len(items), SUMMARY_LIMIT)} of {category}:")
        for item in items[:SUMMARY_LIMIT]:
            lines.append(f"  {item['file']}:{item['line']}: {item['text']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move directories, rewrite paths.")
    parser.add_argument("map", type=Path, help="JSON map file")
    parser.add_argument("--apply", action="store_true", help="move and edit files")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument(
        "--rewrite-only",
        action="store_true",
        help="rewrite references for moves that are already done; move nothing",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="with --rewrite-only: drop entries whose new path is not tracked",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PREFIX",
        help="also leave files under this path prefix alone (repeatable)",
    )
    parser.add_argument("--root", type=Path, help="repository root")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.map.read_text(encoding="utf-8"))
        moves = {
            str(k).rstrip("/"): str(v).rstrip("/") for k, v in spec["moves"].items()
        }
        exclude = tuple(str(p) for p in spec.get("exclude_text", [])) + tuple(
            args.exclude
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"move_dirs: cannot read the map: {exc}", file=sys.stderr)
        return 2
    if not moves:
        print("move_dirs: the map has no moves", file=sys.stderr)
        return 2
    try:
        top = args.root or git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        root = Path(top).resolve()
        tracked = {f for f in git(root, "ls-files", "-z").split("\0") if f}
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"move_dirs: not a git repository: {exc}", file=sys.stderr)
        return 2
    if args.rewrite_only and args.skip_missing:
        for old, new in sorted(moves.items()):
            if not any(covers(new, f) for f in tracked):
                print(f"move_dirs: skipped, new path not tracked: {old} -> {new}")
                del moves[old]
        if not moves:
            print("move_dirs: nothing left to rewrite", file=sys.stderr)
            return 2
    if args.rewrite_only:
        problems = check_done_moves(root, moves, tracked)
        if (
            args.apply
            and git(root, "status", "--porcelain", "--untracked-files=no").strip()
        ):
            problems.append("working tree has uncommitted changes to tracked files")
    else:
        problems = check_moves(root, moves, tracked, args.apply)
    if problems:
        for problem in sorted(problems):
            print(f"move_dirs: {problem}", file=sys.stderr)
        return 1
    try:
        map_rel = args.map.resolve().relative_to(root).as_posix()
    except ValueError:
        map_rel = ""
    pats = Patterns(moves)
    by_len = sorted(moves, key=lambda o: (-len(o), o))
    scans: dict[str, Scan] = {}
    excluded: dict[str, int] = {}
    skip = {SELF, map_rel}
    if args.rewrite_only:
        skip |= {f for f in tracked if MAP_FILES.fullmatch(f)}
    for rel in sorted(tracked - skip):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if not is_text(data):
            continue
        text = data.decode("utf-8")
        if rel.startswith(exclude):
            count = len(pats.rewrite.findall(text))
            if count:
                excluded[rel] = count
            continue
        here = rel if args.rewrite_only else new_path(moves, by_len, rel)
        scan = Scan(rel, here, text)
        rewrite_file(scan, pats, moves)
        scans[rel] = scan
    if args.apply:
        try:
            apply_plan(root, moves, scans, args.rewrite_only)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"move_dirs: apply failed: {exc}", file=sys.stderr)
            return 1
    report = build_report(moves, scans, excluded)
    if args.report:
        text = json.dumps(report, indent=2, ensure_ascii=False)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
