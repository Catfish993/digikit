"""Tests for tools/devtools/pathcheck.py (tiny git repositories, no firmware)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support.git_repo import make_repo

from tools.devtools import pathcheck

FILES = {
    "tools/own/mod.py": "x = 1\n",
    "tools/own/other.py": "y = 1\n",
    "packages/emulator/boot/lib.rs": "// code\n",
    "docs/notes.md": (
        "ok tools/own/mod.py:12 and tools/own/mod.py#anchor, (tools/own/).\n"
        "dotted tools/own/mod.func and packages/emulator/boot.\n"
        "stale tools/mod.py and tools/own/gone.py and native/boot/lib.rs.\n"
        "skip tools/own/*.py tools/<name>/x out/tools/nothing tools/own/NAME.py\n"
        "generated out/runs/x and apps/web/.local/y and docs/findings/07\n"
    ),
    "docs/handovers/h.md": "tools/old/thing.py\n",
    "docs/fixture.md": "tools/fake.py and tools/fake2.py\n",
    "tools/devtools/move-r1.json": json.dumps(
        {"moves": {"native/boot": "packages/emulator/boot"}}
    ),
    "apps/web/package.json": "{}\n",
    "pnpm-lock.yaml": "tools/lock/entry.py\n",
    "blob.bin": "tools/nope.py\0",
}
CONFIG: dict[str, Any] = {
    "retired_roots": ["native"],
    "exclude": [
        "docs/handovers/",
        "pnpm-lock.yaml",
        "docs/fixture.md",
        "tools/devtools/move-*.json",
    ],
    "generated": ["out/", "apps/web/.local"],
    "allow": [
        {"token": "tools/own/gone.py", "file": "elsewhere.md", "reason": "test"},
        {"pattern": "docs/findings/\\d\\d", "reason": "NN shorthand"},
    ],
}


def run(
    root: Path, config: dict[str, Any] | None = None, *extra: str
) -> tuple[int, list[dict[str, Any]]]:
    cfg = root.parent / "cfg.json"
    cfg.write_text(json.dumps(CONFIG if config is None else config))
    out = root.parent / "out.json"
    out.unlink(missing_ok=True)
    args = ["--root", str(root), "--config", str(cfg), "--json", str(out), *extra]
    code = pathcheck.main(args)
    return code, json.loads(out.read_text())["stale"] if out.exists() else []


def tokens(rows: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    return [(r["file"], r["line"], r["token"]) for r in rows]


def test_reports_only_stale_tokens(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, rows = run(root)
    assert code == 1
    assert tokens(rows) == [
        ("docs/notes.md", 3, "tools/mod.py"),
        ("docs/notes.md", 3, "tools/own/gone.py"),
        ("docs/notes.md", 3, "native/boot/lib.rs"),
    ]


def test_suggestions_use_move_maps_and_unique_names(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    _, rows = run(root)
    by_token = {r["token"]: r["suggestion"] for r in rows}
    assert by_token["native/boot/lib.rs"] == "packages/emulator/boot/lib.rs (map)"
    assert by_token["tools/mod.py"] == "tools/own/mod.py (name)"
    assert by_token["tools/own/gone.py"] == ""


def test_clean_repo_exits_zero(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"tools/a.py": "see tools/a.py\n", "docs/b.md": "x\n"})
    code, rows = run(root, {})
    assert (code, rows) == (0, [])


def test_allow_entry_by_file_and_token(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    cfg = {
        **CONFIG,
        "allow": [{"token": "tools/mod.py", "file": "docs/notes.md", "reason": "r"}],
    }
    _, rows = run(root, cfg)
    assert "tools/mod.py" not in {r["token"] for r in rows}
    cfg = {
        **CONFIG,
        "allow": [{"token": "tools/mod.py", "file": "x.md", "reason": "r"}],
    }
    _, rows = run(root, cfg)
    assert "tools/mod.py" in {r["token"] for r in rows}


def test_allow_needs_a_reason(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, _ = run(root, {"allow": [{"token": "tools/mod.py"}]})
    assert code == 2


def test_paths_restricts_the_scan(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {**FILES, "tools/own/doc.md": "tools/zzz.py\n"})
    _, rows = run(root, None, "--paths", "tools/own")
    assert tokens(rows) == [("tools/own/doc.md", 1, "tools/zzz.py")]


def test_without_exclude_the_fixture_is_scanned(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    _, rows = run(root, {**CONFIG, "exclude": []})
    files = {r["file"] for r in rows}
    assert {"docs/handovers/h.md", "docs/fixture.md", "pnpm-lock.yaml"} <= files
    assert "blob.bin" not in files
