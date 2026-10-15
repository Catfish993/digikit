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


def test_apply_does_not_stage_untracked_or_ignored(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    (root / ".gitignore").write_text("*.ignored\n")
    (root / "native/boot/loose.txt").write_text("untracked\n")
    (root / "native/boot/cache.ignored").write_text("ignored\n")
    code, _ = run(root, apply=True)
    assert code == 0
    assert (root / "packages/emulator/boot/loose.txt").exists()
    assert (root / "packages/emulator/boot/cache.ignored").exists()
    tracked = git(root, "ls-files").splitlines()
    assert "packages/emulator/boot/loose.txt" not in tracked
    assert "packages/emulator/boot/cache.ignored" not in tracked
    status = git(root, "status", "--porcelain", "--ignored").splitlines()
    assert "?? packages/emulator/boot/loose.txt" in status
    assert "!! packages/emulator/boot/cache.ignored" in status


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


def done_repo(tmp_path: Path) -> Path:
    files = {
        "packages/emulator/boot/b.txt": "plain\n",
        "apps/web/index.html": "see native/boot and packages/web/x\n",
        "docs/notes.md": "native/boot; native/bootstrap; out/native/boot; ../native/boot\n",
        "docs/handovers/h.md": "native/boot\n",
        "docs/LAYOUT.md": "native/boot\n",
        "tools/devtools/move-r9.json": '{"moves": {"native/boot": "x"}}\n',
        "blob.bin": "native/boot\0",
    }
    return make_repo(tmp_path, files)


DONE = {"native/boot": "packages/emulator/boot", "packages/web": "apps/web"}


def run_only(
    root: Path,
    moves: dict[str, Any] = DONE,
    *extra: str,
    extra_keys: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    map_path = root.parent / "map.json"
    spec = {"moves": moves, "exclude_text": EXCLUDE, **(extra_keys or {})}
    map_path.write_text(json.dumps(spec))
    report = root.parent / "report.json"
    report.unlink(missing_ok=True)
    args = [str(map_path), "--root", str(root), "--report", str(report), *extra]
    code = move_dirs.main(["--rewrite-only", *args])
    return code, json.loads(report.read_text()) if report.exists() else {}


def test_rewrite_only_dry_run_and_apply(tmp_path: Path) -> None:
    root = done_repo(tmp_path)
    code, data = run_only(root, extra_keys={"packages": {"x": "y"}})
    assert code == 0
    assert git(root, "status", "--porcelain").strip() == ""
    assert {e["file"] for e in data["edits"]} == {
        "apps/web/index.html",
        "docs/notes.md",
        "docs/LAYOUT.md",
    }
    assert data["excluded"] == {"docs/handovers/h.md": 1}
    code, _ = run_only(root, DONE, "--apply")
    assert code == 0
    read = lambda rel: (root / rel).read_text()  # noqa: E731
    assert read("apps/web/index.html") == "see packages/emulator/boot and apps/web/x\n"
    assert read("docs/notes.md") == (
        "packages/emulator/boot; native/bootstrap; out/native/boot; ../native/boot\n"
    )
    assert read("docs/handovers/h.md") == "native/boot\n"
    assert read("tools/devtools/move-r9.json").startswith('{"moves": {"native/boot"')
    assert (root / "blob.bin").read_bytes() == b"native/boot\0"
    assert not git(root, "diff").strip()  # staged
    assert git(root, "status", "--porcelain").strip()


def test_rewrite_only_exclude_flag(tmp_path: Path) -> None:
    root = done_repo(tmp_path)
    _, data = run_only(root, DONE, "--exclude", "docs/LAYOUT.md")
    assert data["excluded"]["docs/LAYOUT.md"] == 1
    assert "docs/LAYOUT.md" not in {e["file"] for e in data["edits"]}
    _, data = run_only(root)
    assert "docs/LAYOUT.md" in {e["file"] for e in data["edits"]}


def test_rewrite_only_refuses_unfinished_moves(tmp_path: Path) -> None:
    root = done_repo(tmp_path)
    assert run_only(root, {"apps/web": "elsewhere"})[0] == 1  # old still exists
    assert run_only(root, {"native/boot": "packages/missing"})[0] == 1
    dup = {"native/boot": "apps/web", "native/x": "apps/web"}
    assert run_only(root, dup)[0] == 1
    assert git(root, "status", "--porcelain").strip() == ""


def test_rewrite_only_dirty_tree_fails_only_on_apply(tmp_path: Path) -> None:
    root = done_repo(tmp_path)
    (root / "docs/LAYOUT.md").write_text("changed\n")
    assert run_only(root, DONE, "--apply")[0] == 1
    assert run_only(root)[0] == 0


def test_rewrite_only_skip_missing_and_leftover_dir(tmp_path: Path) -> None:
    root = done_repo(tmp_path)
    (root / "native/boot").mkdir(parents=True)  # untracked leftover directory
    (root / "native/boot/cache.bin").write_text("x")
    moves = {**DONE, "native/gone.py": "packages/emulator/gone.py"}
    assert run_only(root, moves)[0] == 1
    code, data = run_only(root, moves, "--skip-missing")
    assert code == 0
    assert ["native/gone.py", "packages/emulator/gone.py"] not in data["moves"]
    assert len(data["moves"]) == 2
