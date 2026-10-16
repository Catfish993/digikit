"""Ruff lint and format checks for the files that have been cleaned.

The rules live in pyproject.toml. Add a path here once it passes both
`ruff check` and `ruff format --check`; the list should only grow.
"""

import subprocess
import sys

import pytest
from support.paths import REPO_ROOT

ROOT = str(REPO_ROOT)

# Every tracked Python file (ruff skips git-ignored files).
CLEAN = [
    "tests",
    "tools",
]


def ruff(*args):
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args, *CLEAN],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "args",
    [("check", "--no-cache"), ("format", "--check", "--no-cache")],
    ids=["check", "format"],
)
def test_ruff(args):
    result = ruff(*args)
    assert result.returncode == 0, result.stdout + result.stderr
