"""Repository tools, one subpackage per owner (docs/LAYOUT.md).

The repository root is the import root: run a tool as
`uv run python -m tools.<owner>.<module>` from the repository root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def python_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of env (default os.environ) with REPO_ROOT first on PYTHONPATH, for
    a child Python process that imports tools."""
    out = dict(os.environ if env is None else env)
    root = str(REPO_ROOT)
    rest = [p for p in out.get("PYTHONPATH", "").split(os.pathsep) if p and p != root]
    out["PYTHONPATH"] = os.pathsep.join([root, *rest])
    return out
