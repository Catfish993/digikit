"""Fail-closed audit of the inputs of a coupled-run render.

Native code linked into coupled-run reads environment variables and files. Each
environment variable must be decided: in the render cache key
(tools/devtools/render_cache.py semantic_env), in DENYLIST / DENY_PREFIXES, or in
IGNORED_ENV. Each file read is counted per source file and compared with
tests/devtools/render_inputs.json, so a new read gets reviewed.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.devtools import render_cache  # noqa: E402

NATIVE = ROOT / "packages" / "emulator"
COUPLED = NATIVE / "coupled"
COUPLED_RUN = COUPLED / "src" / "bin" / "coupled_run.rs"
COUNTS = Path(__file__).with_name("render_inputs.json")

ENV_RE = re.compile(
    r'env::var(?:_os)?\(\s*(?:"([A-Za-z0-9_]+)"|([A-Z_][A-Z0-9_]*)\s*\))'
)
CALL_RE = re.compile(r"env::var(?:_os)?\(")
READ_RE = re.compile(
    r"\b(fs::read|fs::read_to_string|File::open|read_to_end|fs::read_dir"
    r"|include_bytes!|include_str!)"
)
TEST_DIRS = frozenset({"tests", "benches", "examples"})


def _dep_tables(manifest: dict) -> list[dict]:
    tables = [manifest.get("dependencies", {})]
    for target in manifest.get("target", {}).values():
        tables.append(target.get("dependencies", {}))
    return tables


def crates() -> set[Path]:
    seen: set[Path] = set()
    todo = [COUPLED]
    while todo:
        crate = todo.pop().resolve()
        if crate in seen:
            continue
        seen.add(crate)
        manifest = tomllib.loads((crate / "Cargo.toml").read_text())
        for table in _dep_tables(manifest):
            for spec in table.values():
                if isinstance(spec, dict) and "path" in spec:
                    todo.append(crate / spec["path"])
    return seen


def _is_test_file(path: Path) -> bool:
    name = path.name
    return name == "tests.rs" or name.startswith("tests_") or name.endswith("_tests.rs")


def sources(crate: Path) -> list[Path]:
    src = crate / "src"
    out: list[Path] = []
    for path in sorted(src.rglob("*.rs")):
        rel = path.relative_to(src)
        if TEST_DIRS & set(rel.parts[:-1]) or _is_test_file(path):
            continue
        if rel.parts[0] == "bin" and path != COUPLED_RUN:
            continue
        if rel.parts == ("main.rs",):
            continue
        out.append(path)
    return out


def all_sources() -> list[Path]:
    return [p for crate in sorted(crates()) for p in sources(crate)]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def env_reads(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ((where, name) of resolvable reads, where of computed-name reads)."""
    text = path.read_text()
    named: list[tuple[str, str]] = []
    dynamic: list[str] = []
    for call in CALL_RE.finditer(text):
        where = f"{_rel(path)}:{text.count(chr(10), 0, call.start()) + 1}"
        m = ENV_RE.match(text, call.start())
        name = m.group(1) if m else None
        if m and name is None:
            const = re.search(
                rf'\bconst\s+{m.group(2)}\s*:\s*&\S*\s*str\s*=\s*"([^"]*)"', text
            )
            name = const.group(1) if const else None
        if name is None:
            dynamic.append(where)
        else:
            named.append((where, name))
    return named, dynamic


def scan() -> dict[str, dict[str, int]]:
    """Current per-file counts of file reads and computed-name env reads."""
    file_reads: dict[str, int] = {}
    dynamic: dict[str, int] = {}
    for path in all_sources():
        n = len(READ_RE.findall(path.read_text()))
        if n:
            file_reads[_rel(path)] = n
        d = len(env_reads(path)[1])
        if d:
            dynamic[_rel(path)] = d
    return {"file_reads": file_reads, "dynamic_env_reads": dynamic}


def _classified(name: str) -> bool:
    return (
        name in render_cache.semantic_env({name: "1"})
        or name in render_cache.DENYLIST
        or name.startswith(render_cache.DENY_PREFIXES)
        or name in render_cache.IGNORED_ENV
    )


class RenderInputsTest(unittest.TestCase):
    def test_crate_walk(self) -> None:
        found = crates()
        for name in ("coupled", "sharc", "boot"):
            self.assertIn((NATIVE / name).resolve(), found)

    def test_every_env_read_is_classified(self) -> None:
        unclassified: dict[str, list[str]] = {}
        for path in all_sources():
            for where, name in env_reads(path)[0]:
                if not _classified(name):
                    unclassified.setdefault(name, []).append(where)
        problems: list[str] = []
        for name, wheres in sorted(unclassified.items()):
            problems.append(f"{name}: {', '.join(wheres)}")
        if problems:
            self.fail(
                "\n".join(problems)
                + "\nclassify it: if it can change a render, make sure the key"
                " includes it (tools/devtools/render_cache.py semantic_env); if not, add"
                " it to DENYLIST or IGNORED_ENV with a reason"
            )

    def test_dynamic_env_reads_are_reviewed(self) -> None:
        actual: dict[str, int] = {}
        for path in all_sources():
            n = len(env_reads(path)[1])
            if n:
                actual[_rel(path)] = n
        reviewed: dict[str, dict[str, object]] = json.loads(COUNTS.read_text())[
            "dynamic_env_reads"
        ]
        expected = {p: int(str(v["count"])) for p, v in reviewed.items()}
        diffs = [
            f"{p}: expected {expected.get(p, 0)}, found {actual.get(p, 0)}"
            for p in sorted(set(actual) | set(expected))
            if actual.get(p, 0) != expected.get(p, 0)
        ]
        if diffs:
            self.fail(
                "\n".join(diffs)
                + "\nan env read with a computed name was added in code linked"
                " into coupled-run: make sure every name it can take is"
                " classified (tools/devtools/render_cache.py), then update"
                " dynamic_env_reads"
            )

    def test_runtime_file_reads_are_reviewed(self) -> None:
        actual = scan()["file_reads"]
        expected: dict[str, int] = json.loads(COUNTS.read_text())["file_reads"]
        diffs = [
            f"{p}: expected {expected.get(p, 0)}, found {actual.get(p, 0)}"
            for p in sorted(set(actual) | set(expected))
            if actual.get(p, 0) != expected.get(p, 0)
        ]
        if diffs:
            self.fail(
                "\n".join(diffs)
                + "\na file read changed in code linked into coupled-run: if it"
                " reads a render input that is not a coupled-run argument, add it"
                " to the render cache key (tools/devtools/render_cache.py); then update"
                " tests/devtools/render_inputs.json"
            )


if __name__ == "__main__":
    if "--scan" in sys.argv[1:]:
        print(json.dumps(scan(), indent=2, sort_keys=True))
    else:
        unittest.main()
