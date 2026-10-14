"""Tests for tools/devtools/move_dirs.py (tiny git repositories, no firmware)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support.git_repo import git, make_repo

from tools.devtools import move_dirs

MOVES = {
    "native/boot": "packages/emulator/boot",
    "native/README.md": "packages/emulator/README.md",
    "packages/web": "apps/web",
}
FILES = {
    "native/boot/a.txt": "native/boot/b.txt and packages/web\n",
    "native/boot/b.txt": "plain\n",
    "native/README.md": "see native/README.md\n",
    "native/other.txt": "keep\n",
    "packages/web/index.html": "native/boot\n",
    "docs/notes.md": (
        "native/boot is here; native/bootstrap; out/native/boot; ../native/boot;"
        " native/*\n"
    ),
    "docs/handovers/h.md": "native/boot and native/boot\n",
    "src/m.py": 'ROOT / "native" / "boot"\nPATH = "packages/web/x"\n',
    "blob.bin": "native/boot\0",
}
EXCLUDE = ["docs/handovers/"]


def run(
    root: Path,
    moves: dict[str, str] = MOVES,
    *,
    apply: bool = False,
    exclude: list[str] = EXCLUDE,
) -> tuple[int, dict[str, Any]]:
    map_path = root.parent / "map.json"
    map_path.write_text(json.dumps({"moves": moves, "exclude_text": exclude}))
    report = root.parent / "report.json"
    report.unlink(missing_ok=True)
    args = [str(map_path), "--root", str(root), "--report", str(report)]
    if apply:
        args.append("--apply")
    code = move_dirs.main(args)
    return code, json.loads(report.read_text()) if report.exists() else {}


def found(data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [r for r in data["reports"] if r["category"] == category]


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, data = run(root)
    assert code == 0
    assert git(root, "status", "--porcelain").strip() == ""
    assert (root / "native/boot/a.txt").exists()
    assert not (root / "apps").exists()
    assert data["moves"] == [[o, n] for o, n in sorted(MOVES.items())]
    assert data["excluded"] == {"docs/handovers/h.md": 2}
    assert data["edits"] == sorted(
        data["edits"], key=lambda e: (e["file"], e["line"], e["old"], e["new"])
    )
    edited = {e["file"] for e in data["edits"]}
    assert edited == {
        "packages/emulator/boot/a.txt",
        "packages/emulator/README.md",
        "apps/web/index.html",
        "docs/notes.md",
        "src/m.py",
    }


def test_apply_moves_and_rewrites(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, _ = run(root, apply=True)
    assert code == 0

    def read(rel: str) -> str:
        return (root / rel).read_text()

    assert read("packages/emulator/boot/a.txt") == (
        "packages/emulator/boot/b.txt and apps/web\n"
    )
    assert read("packages/emulator/boot/b.txt") == "plain\n"
    assert read("packages/emulator/README.md") == "see packages/emulator/README.md\n"
    assert read("apps/web/index.html") == "packages/emulator/boot\n"
    assert read("native/other.txt") == "keep\n"
    assert read("src/m.py").splitlines()[1] == 'PATH = "apps/web/x"'
    assert not (root / "native/boot").exists()
    assert not (root / "native/README.md").exists()
    assert not (root / "packages/web").exists()
    assert git(root, "status", "--porcelain", "--untracked-files=no").strip()
    assert not git(root, "diff").strip()  # everything is staged


def test_boundaries_and_relative_refs(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, data = run(root, apply=True)
    assert code == 0
    notes = (root / "docs/notes.md").read_text()
    assert notes == (
        "packages/emulator/boot is here; native/bootstrap; out/native/boot;"
        " ../native/boot; native/*\n"
    )
    rel = found(data, "relative-ref")
    assert [(r["file"], r["line"], r["text"]) for r in rel] == [
        ("docs/notes.md", 1, "../native/boot")
    ]


def test_glob_and_bare_parent_reports(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    _, data = run(root)
    globs = found(data, "glob-ref")
    assert [(r["file"], r["text"]) for r in globs] == [("docs/notes.md", "native/*")]
    bare = found(data, "bare-parent")
    assert [(r["file"], r["line"], r["text"]) for r in bare] == [
        ("src/m.py", 1, '"native"')
    ]


def test_exclude_text_respected_and_counted(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, data = run(root, apply=True)
    assert code == 0
    assert (root / "docs/handovers/h.md").read_text() == FILES["docs/handovers/h.md"]
    assert data["excluded"] == {"docs/handovers/h.md": 2}
    assert not [e for e in data["edits"] if e["file"].startswith("docs/handovers")]


def test_binary_file_untouched(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, data = run(root, apply=True)
    assert code == 0
    assert (root / "blob.bin").read_bytes() == b"native/boot\0"
    assert "blob.bin" not in {e["file"] for e in data["edits"]}


def test_destination_exists_fails(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    moves = {**MOVES, "native/other.txt": "docs/notes.md"}
    code, _ = run(root, moves, apply=True)
    assert code == 1
    assert (root / "native/other.txt").exists()
    assert (root / "native/boot/a.txt").exists()


def test_duplicate_destination_and_nested_source_fail(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    dup = {"native/boot": "x/y", "native/other.txt": "x/y"}
    assert run(root, dup)[0] == 1
    nested = {"native": "n2", "native/boot": "x/boot"}
    assert run(root, nested)[0] == 1
    missing = {"nothing/here": "x/y"}
    assert run(root, missing)[0] == 1


def test_dirty_tree_fails_only_on_apply(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    (root / "native/other.txt").write_text("changed\n")
    code, _ = run(root, apply=True)
    assert code == 1
    assert (root / "native/boot/a.txt").exists()
    assert not (root / "apps").exists()
    code, _ = run(root)
    assert code == 0
