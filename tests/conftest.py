"""Pytest options for this suite.

Tests marked ``slow`` (long firmware integration runs) are skipped unless
``--slow`` is given.
"""

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = str(Path(__file__).resolve().parents[1] / "tools")
# tests/ itself, so tests import tests/support/ from any subdirectory
_TESTS_DIR = str(Path(__file__).resolve().parent)


def _ensure_paths():
    """Some Ghidra tools drop tools/ from sys.path when imported (so PyGhidra's
    ghidra package is not shadowed); restore the paths this conftest promises
    before each collection and test."""
    present = {os.path.abspath(p) for p in sys.path if p}
    for path in (_TESTS_DIR, _TOOLS_DIR):
        if os.path.abspath(path) not in present:
            sys.path.insert(0, path)


_ensure_paths()
from tools.sharc_dev.sharc_transpile_run import NativeLibraryUnusable  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", help="also run tests marked slow")


def pytest_collectstart(collector):
    _ensure_paths()


def pytest_runtest_setup(item):
    _ensure_paths()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long firmware integration test")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """A test that needs the native SHARC library skips, with the reason, when
    the library is missing symbols or stale (tools/sharc_dev/sharc_transpile_run.py)."""
    try:
        return (yield)
    except NativeLibraryUnusable as exc:
        pytest.skip(str(exc))


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow"):
        return
    skip = pytest.mark.skip(reason="slow; run with --slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
