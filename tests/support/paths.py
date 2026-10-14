"""Repository paths for tests, independent of how deep a test file sits under tests/."""

from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS.parent
TOOLS = REPO_ROOT / "tools"
