"""Tiny git repositories for the tools/devtools tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    commit = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=0"]
    git(root, *commit, "commit", "-qm", "x")
    return root
