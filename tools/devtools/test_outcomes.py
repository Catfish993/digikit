"""Prove that a refactor or file move kept every pytest test's outcome.

Compares two pytest JUnit XML files (pytest --junitxml) test by test.

Usage:
    uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml
    uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml --renames-from HEAD
    uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml \
        --rename tests/test_a.py=tests/sharc/test_a.py [--allow-new]
    uv run python -m tools.devtools.test_outcomes BEFORE.xml AFTER.xml \
        --split tests/test_a.py=tests/test_a1.py,tests/test_a2.py
(--split: tests of OLD may now be in any NEW file and class; matched by bare name,
by class and name when the bare name is not unique in OLD.)

Run every test file in its own pytest process and record a baseline:
    uv run python -m tools.devtools.test_outcomes run --out DIR [--root PATH] [--jobs N] \
        [--timeout SECONDS] [--pytest-arg=ARG ...] [--check] PATHS...
writes DIR/logs, DIR/xml, DIR/summary.tsv and DIR/all.xml (a hung or crashed file
becomes one error testcase named __file__) and exits 0; with --check it exits 1
when any file failed.

Exit status of the compare mode: 0 when the outcomes are the same, 1 when they differ, 2 on usage
errors or unreadable XML.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
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


def bare_of(rest: str) -> str:
    """Test name without its class: the part after the last '::'."""
    return rest.rsplit("::", 1)[-1]


def class_of(rest: str) -> str:
    return rest.rsplit("::", 1)[0]


def apply_split(
    moved: dict[Key, Result],
    after: dict[Key, Result],
    old: str,
    news: list[str],
) -> str:
    """Re-key AFTER tests in NEWS to the BEFORE key they match in OLD.

    Matched pairs end up under the same key in both dicts. Returns the report
    line."""
    bkeys = sorted(k for k in moved if k[0] == old)
    akeys = sorted(k for k in after if k[0] in news)
    by_bare_b: dict[str, list[Key]] = {}
    by_bare_a: dict[str, list[Key]] = {}
    for k in bkeys:
        by_bare_b.setdefault(bare_of(k[1]), []).append(k)
    for k in akeys:
        by_bare_a.setdefault(bare_of(k[1]), []).append(k)
    pairs: list[tuple[Key, Key]] = []
    for bare, bl in by_bare_b.items():
        al = list(by_bare_a.get(bare, []))
        unique = len(bl) == 1
        if unique and len(al) == 1:
            pairs.append((bl[0], al[0]))
            continue
        for bk in bl:
            same = [a for a in al if class_of(a[1]) == class_of(bk[1])]
            if same:
                al.remove(same[0])
                pairs.append((bk, same[0]))
    matched_a = {a for _, a in pairs}
    new_count = len([k for k in akeys if k not in matched_a])
    results = {a: after[a] for _, a in pairs}
    for a in matched_a:
        del after[a]
    for b, a in pairs:
        after[b] = results[a]
    missing = len(bkeys) - len(pairs)
    return (
        f"split {old} -> {len(news)} files: {len(pairs)} matched, "
        f"{missing} missing, {new_count} new"
    )


def compare(
    before: dict[Key, Result],
    after: dict[Key, Result],
    renames: dict[str, str],
    allow_new: bool,
    splits: dict[str, list[str]] | None = None,
) -> int:
    """Print the report and return the exit status."""
    moved = {(renames.get(p, p), r): v for (p, r), v in before.items()}
    after = dict(after)
    split_lines = [
        apply_split(moved, after, old, news) for old, news in (splits or {}).items()
    ]
    print(f"before: {counts(moved)}")
    print(f"after:  {counts(after)}")
    for line in split_lines:
        print(line)
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


def slug_of(path: str) -> str:
    """File path -> log/XML base name: '/' becomes '__', no '.py'."""
    return path.removesuffix(".py").replace("/", "__")


def dotted_of(path: str) -> str:
    """File path -> module dotted path."""
    return path.removesuffix(".py").replace("/", ".")


def expand_paths(root: Path, paths: list[str]) -> list[str]:
    """Expand files and directories to sorted, unique test_*.py paths below ROOT."""
    found: set[str] = set()
    for item in paths:
        full = root / item
        files = sorted(full.rglob("test_*.py")) if full.is_dir() else [full]
        for f in files:
            rel = f.relative_to(root)
            if "support" not in rel.parts[:-1]:
                found.add(rel.as_posix())
    return sorted(found)


def xml_counts(path: Path) -> tuple[int, int, int, int] | None:
    """(passed, failed, skipped, errors) of a JUnit XML file, None if unreadable."""
    try:
        cases = list(ET.parse(path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return None
    failed = skipped = errors = 0
    for c in cases:
        tags = {child.tag for child in c}
        if "error" in tags:
            errors += 1
        elif "failure" in tags:
            failed += 1
        elif "skipped" in tags:
            skipped += 1
    return len(cases) - failed - skipped - errors, failed, skipped, errors


def run_one(
    root: Path,
    out: Path,
    rel: str,
    pytest_args: list[str],
    timeout: float,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run pytest on one file in its own process group (ENV: its environment, default ours); return
    its record."""
    slug = slug_of(rel)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "xml").mkdir(parents=True, exist_ok=True)
    xml = out / "xml" / f"{slug}.xml"
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        f"--junitxml={xml}", *pytest_args, rel,
    ]  # fmt: skip
    start = time.monotonic()
    timed_out = False
    with open(out / "logs" / f"{slug}.log", "wb") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            except ProcessLookupError:
                pass
            rc = proc.returncode
    seconds = time.monotonic() - start
    cnt = None if timed_out else xml_counts(xml)
    if timed_out:
        status = "TIMEOUT"
    elif cnt is None:
        status = "NO-XML"
    else:
        status = "OK"
    return {
        "file": rel,
        "status": status,
        "rc": rc,
        "seconds": seconds,
        "counts": cnt,
        "xml": xml,
    }


def record_line(rec: dict[str, object]) -> str:
    """The one-line summary of a record: rc, seconds, passed/failed/skipped/errors, file."""
    cnt = rec["counts"]
    shown = (
        "/".join(str(n) for n in cnt) if isinstance(cnt, tuple) else str(rec["status"])
    )
    return f"{rec['rc']}  {rec['seconds']:.1f}  {shown}  {rec['file']}"


def run_all(
    root: Path,
    out: Path,
    files: list[str],
    pytest_args: list[str],
    jobs: int,
    timeout: float,
) -> list[str]:
    """Run every file, print one line each, write summary.tsv and all.xml.

    Returns the sorted files that did not pass."""
    records: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(run_one, root, out, f, pytest_args, timeout) for f in files
        ]
        for fut in as_completed(futures):
            rec = fut.result()
            print(record_line(rec), flush=True)
            records.append(rec)
    return write_reports(out, records, timeout)


def write_reports(
    out: Path, records: list[dict[str, object]], timeout: float
) -> list[str]:
    """Write summary.tsv and all.xml for RECORDS (run_one results); the sorted files that did not pass."""
    records = list(records)
    records.sort(key=lambda r: str(r["file"]))
    suites = ET.Element("testsuites")
    lines = ["file\tstatus\trc\tseconds\tpassed\tfailed\tskipped\terrors"]
    for rec in records:
        cnt = rec["counts"]
        nums = cnt if isinstance(cnt, tuple) else ("", "", "", "")
        lines.append(
            "\t".join(
                str(x)
                for x in (
                    rec["file"],
                    rec["status"],
                    rec["rc"],
                    f"{rec['seconds']:.1f}",
                    *nums,
                )
            )
        )
        suite = ET.SubElement(suites, "testsuite", name=str(rec["file"]))
        xml = rec["xml"]
        assert isinstance(xml, Path)
        if rec["status"] == "OK":
            for c in ET.parse(xml).getroot().iter("testcase"):
                suite.append(c)
            continue
        if rec["status"] == "TIMEOUT":
            message = f"TIMEOUT after {timeout:g} s"
        else:
            message = f"NO-XML rc={rec['rc']}"
        c = ET.SubElement(
            suite,
            "testcase",
            classname=dotted_of(str(rec["file"])),
            name="__file__",
        )
        ET.SubElement(c, "error", message=message)
    (out / "summary.tsv").write_text("\n".join(lines) + "\n")
    ET.ElementTree(suites).write(
        out / "all.xml", encoding="utf-8", xml_declaration=True
    )
    bad: list[str] = []
    for rec in records:
        cnt = rec["counts"]
        if rec["status"] != "OK" or (isinstance(cnt, tuple) and (cnt[1] or cnt[3])):
            bad.append(str(rec["file"]))
    return bad


def run_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="test_outcomes.py run",
        description="Run every test file in isolation and record a baseline.",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--pytest-arg", action="append", default=[], metavar="ARG")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("paths", nargs="+", metavar="PATHS")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.jobs < 1 or args.timeout <= 0:
        print("test_outcomes: --jobs and --timeout must be positive", file=sys.stderr)
        return 2
    root = args.root.resolve()
    if not root.is_dir():
        print(f"test_outcomes: no such root {root}", file=sys.stderr)
        return 2
    files = expand_paths(root, args.paths)
    if not files:
        print("test_outcomes: no test files found", file=sys.stderr)
        return 2
    bad = run_all(
        root, args.out.resolve(), files, args.pytest_arg, args.jobs, args.timeout
    )
    if not args.check:
        return 0
    if bad:
        shown = ", ".join(bad[:10]) + (", ..." if len(bad) > 10 else "")
        print(f"test_outcomes: {len(bad)} files failed: {shown}")
        return 1
    print(f"test_outcomes: all {len(files)} files passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "run":
        return run_main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--renames-from", metavar="REF")
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--split", action="append", default=[], metavar="OLD=NEW1,NEW2")
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
        splits: dict[str, list[str]] = {}
        for item in args.split:
            old, sep, rest = item.partition("=")
            news = [n for n in rest.split(",") if n]
            if not sep or not old or not news:
                print(f"bad --split {item!r}, want OLD=NEW1,NEW2", file=sys.stderr)
                return 2
            splits.setdefault(old, []).extend(news)
        before = read_outcomes(args.before)
        after = read_outcomes(args.after)
    except (OSError, ET.ParseError, subprocess.CalledProcessError) as exc:
        print(f"test_outcomes: {exc}", file=sys.stderr)
        return 2
    return compare(before, after, renames, args.allow_new, splits)


if __name__ == "__main__":
    raise SystemExit(main())
