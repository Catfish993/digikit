"""The repository has no stale path references (tools/devtools/pathcheck.py)."""

from __future__ import annotations

import subprocess
import sys

from support.paths import REPO_ROOT


def test_repository_paths_resolve() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "tools.devtools.pathcheck"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
