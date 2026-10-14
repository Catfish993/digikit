"""Prove that a refactor or file move kept every pytest test's outcome.

Compares two pytest JUnit XML files (pytest --junitxml) test by test.

Usage:
    uv run python tools/test_outcomes.py BEFORE.xml AFTER.xml
    uv run python tools/test_outcomes.py BEFORE.xml AFTER.xml --renames-from HEAD
    uv run python tools/test_outcomes.py BEFORE.xml AFTER.xml \
        --rename tests/test_a.py=tests/sharc/test_a.py [--allow-new]

Exit status: 0 when the outcomes are the same, 1 when they differ, 2 on usage
errors or unreadable XML.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIST_CAP = 50
SKIP_PREFIX = re.compile(r"^\S+?:\d+: ")

Key = tuple[str, str]
Result = tuple[str, str]  # (outcome, skip message)


def split_classname(classname: str, name: str) -> Key:
    """Map a JUnit classname and test name to (file path, rest)."""
    parts = classname.split(".") if classname else []
    for i, part in enumerate(parts):
        if part.startswith("test_"):
            path = "/".join(parts[: i + 1]) + ".py"
            rest = ".".join(parts[i + 1 :])
            return path, f"{rest}::{name}"
    return "/".join(parts), f"::{name}"


def read_outcomes(path: Path) -> dict[Key, Result]:
    """Read a JUnit XML file into {key: (outcome, skip message)}."""
    out: dict[Key, Result] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        key = split_classname(case.get("classname", ""), case.get("name", ""))
        tags = {child.tag for child in case}
        message = ""
        if "error" in tags:
            outcome = "error"
        elif "failure" in tags:
            outcome = "failed"
        elif "skipped" in tags:
            outcome = "skipped"
            skipped = case.find("skipped")
            assert skipped is not None
            message = (skipped.get("message") or "")[:200]
        else:
            outcome = "passed"
        out[key] = (outcome, message)
    return out


def norm_reason(message: str) -> str:
    """Strip a leading 'path:line: ' prefix from a skip message."""
    return SKIP_PREFIX.sub("", message, count=1)


def git_renames(ref: str) -> dict[str, str]:
    """File renames under tests/ since REF, from git diff."""
    proc = subprocess.run(
        ["git", "diff", "-M", "--name-status", ref, "--", "tests"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    renames: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0].startswith("R"):
            renames[fields[1]] = fields[2]
    return renames


def show(title: str, lines: list[str]) -> None:
    """Print a titled list, capped at LIST_CAP lines."""
    print(f"{title}: {len(lines)}")
    for line in lines[:LIST_CAP]:
        print(f"  {line}")
    if len(lines) > LIST_CAP:
        print(f"  ... and {len(lines) - LIST_CAP} more")


def fmt(key: Key) -> str:
    return f"{key[0]}::{key[1].removeprefix('::')}"


def describe(result: Result) -> str:
    outcome, message = result
    return f"{outcome} ({message})" if outcome == "skipped" and message else outcome


def counts(results: dict[Key, Result]) -> str:
    names = ("passed", "skipped", "failed", "error")
    return ", ".join(
        f"{n}={sum(1 for r in results.values() if r[0] == n)}" for n in names
    )


def compare(
    before: dict[Key, Result],
    after: dict[Key, Result],
    renames: dict[str, str],
    allow_new: bool,
) -> int:
    """Print the report and return the exit status."""
    moved = {(renames.get(p, p), r): v for (p, r), v in before.items()}
    print(f"before: {counts(moved)}")
    print(f"after:  {counts(after)}")
    missing = sorted(set(moved) - set(after))
    new = sorted(set(after) - set(moved))
    show("only in BEFORE (missing)", [fmt(k) for k in missing])
    show("only in AFTER (new)", [fmt(k) for k in new])
    changed: list[str] = []
    reasons: list[str] = []
    for key in sorted(set(moved) & set(after)):
        b, a = moved[key], after[key]
        if b[0] != a[0]:
            changed.append(f"{fmt(key)}: {describe(b)} -> {describe(a)}")
        elif b[0] == "skipped" and norm_reason(b[1]) != norm_reason(a[1]):
            reasons.append(f"{fmt(key)}: {b[1]!r} -> {a[1]!r}")
    show("outcome changed", changed)
    show("skipped in both, reason changed", reasons)
    n_new = 0 if allow_new else len(new)
    if missing or n_new or changed or reasons:
        print(
            f"test_outcomes: DIFFERENT ({len(missing)} missing, {len(new)} new, "
            f"{len(changed)} changed, {len(reasons)} reason-changed)"
        )
        return 1
    print("test_outcomes: SAME")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--renames-from", metavar="REF")
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--allow-new", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    renames: dict[str, str] = {}
    try:
        if args.renames_from:
            renames.update(git_renames(args.renames_from))
        for item in args.rename:
            old, sep, new = item.partition("=")
            if not sep or not old or not new:
                print(f"bad --rename {item!r}, want OLD=NEW", file=sys.stderr)
                return 2
            renames[old] = new
        before = read_outcomes(args.before)
        after = read_outcomes(args.after)
    except (OSError, ET.ParseError, subprocess.CalledProcessError) as exc:
        print(f"test_outcomes: {exc}", file=sys.stderr)
        return 2
    return compare(before, after, renames, args.allow_new)


if __name__ == "__main__":
    raise SystemExit(main())
