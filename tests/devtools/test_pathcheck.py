"""Tests for tools/devtools/pathcheck.py (tiny git repositories, no firmware)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support.git_repo import git, make_repo

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


# --- refs, history -------------------------------------------------

COMMIT = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=0"]


def history_repo(tmp_path: Path) -> Path:
    """tools/gone.py and tools/keep.py existed; gone.py deleted (tagged before)."""
    root = make_repo(
        tmp_path,
        {
            "tools/gone.py": "g = 1\n",
            "tools/old/never.py": "n = 1\n",
            "tools/keep.py": "k = 1\n",
        },
    )
    git(root, "tag", "before")
    git(root, "rm", "-q", "tools/gone.py", "tools/old/never.py")
    git(root, *COMMIT, "commit", "-qm", "delete")
    return root


def write_commit(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, *COMMIT, "commit", "-qm", "docs")


def test_ref_paths_are_checked_at_the_ref(tmp_path: Path) -> None:
    root = history_repo(tmp_path)
    write_commit(
        root,
        {
            "n.md": "ok before:tools/gone.py and before:tools/keep.py:3.\n"
            "bad before:tools/missing.py and HEAD:tools/gone.py\n"
            "notref nothing:tools/keep.py\n"
        },
    )
    _, rows = run(root, {})
    assert tokens(rows) == [
        ("n.md", 2, "before:tools/missing.py"),
        ("n.md", 2, "HEAD:tools/gone.py"),
    ]


def test_history_docs_tolerate_deleted_paths(tmp_path: Path) -> None:
    root = history_repo(tmp_path)
    doc = "tools/gone.py tools/never_existed.py\n"
    write_commit(root, {"docs/findings/f.md": doc, "docs/other.md": doc})
    code, rows = run(root, {})
    assert code == 1
    got = tokens(rows)
    # the findings file keeps only what never existed
    assert ("docs/findings/f.md", 1, "tools/gone.py") not in got
    assert ("docs/findings/f.md", 1, "tools/never_existed.py") in got
    assert ("docs/other.md", 1, "tools/gone.py") in got
    report = json.loads((root.parent / "out.json").read_text())
    assert [r["token"] for r in report["historical"]] == ["tools/gone.py"]
    assert report["summary"] == {"stale": len(rows), "historical": 1}


def test_ignored_module_and_relative_paths_are_valid(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        {
            ".gitignore": "samples/\ntools/gen/*.bin\n",
            "tools/own/mod.py": "x\n",
            "tools/own/pkg/__init__.py": "x\n",
            "tests/reg/dt2/rec-play.test.ts": "t\n",
            "tests/reg/dt2/pages/a.ts": "t\n",
            "tests/reg/README.md": (
                "samples/a.wav tools/gen/x.bin tools/own/mod tools/own/pkg\n"
                "dt2/rec-play dt2/pages dt2/rec dt2/missing tools/gen/x.txt\n"
            ),
        },
    )
    cfg: dict[str, Any] = {"retired_roots": ["samples", "dt2"]}
    _, rows = run(root, cfg)
    assert [r["token"] for r in rows] == ["dt2/rec", "dt2/missing", "tools/gen/x.txt"]


def test_ignored_paths_below_a_symlinked_directory(tmp_path: Path) -> None:
    # A lane links build directories from the main tree: git refuses a path "beyond a symbolic link" (here `a/dist/`)
    # and used to drop the rest of the check-ignore batch, so every later ignored path counted as stale.
    target = tmp_path / "main-dist"
    target.mkdir()
    root = make_repo(
        tmp_path,
        {
            ".gitignore": "a/dist\nfirmware/*\n!firmware/README.md\n",
            "a/x.ts": "x\n",
            "firmware/README.md": "x\n",
            "tools/a.py": "x\n",
            "README.md": "a/dist/ firmware/X.syx tools/missing.py\n",
        },
    )
    (root / "a" / "dist").symlink_to(target)
    _, rows = run(root, {})
    assert [r["token"] for r in rows] == ["tools/missing.py"]


def test_unused_allow_entries_fail_a_full_scan(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"tools/a.py": "see tools/a.py and tools/b.py\n"})
    cfg: dict[str, Any] = {
        "allow": [
            {"token": "tools/b.py", "reason": "used"},
            {"token": "tools/zzz.py", "reason": "unused"},
        ]
    }
    code, rows = run(root, cfg)
    assert (code, rows) == (1, [])
    report = json.loads((root.parent / "out.json").read_text())
    assert [e["token"] for e in report["unused_allow"]] == ["tools/zzz.py"]
    # a partial scan cannot tell
    assert run(root, cfg, "--paths", "tools")[0] == 0


def test_moved_local_roots(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        {
            "tools/a.py": "x\n",
            "docs/findings/f.md": "old sections/section_3.bin and .coldtrace/x\n",
            "docs/n.md": (
                "live sections/.source-sha256 and .coldtrace/t.log, "
                "snapshots/a/b.snap.\n"
                "not out/sections/x or apps/x/sections/y or the sections word\n"
            ),
        },
    )
    cfg: dict[str, Any] = {
        "generated": ["out/"],
        "moved_local": {
            "sections/": "out/sections/dt2-1.16/",
            ".coldtrace/": "out/coldtrace/",
            "snapshots/": "out/snapshots/",
        },
    }
    code, rows = run(root, cfg)
    assert code == 1
    assert [r["token"] for r in rows] == [
        "sections/.source-sha256",
        ".coldtrace/t.log",
        "snapshots/a/b.snap",
    ]
    report = json.loads((root.parent / "out.json").read_text())
    assert [r["token"] for r in report["historical"]] == [
        "sections/section_3.bin",
        ".coldtrace/x",
    ]
