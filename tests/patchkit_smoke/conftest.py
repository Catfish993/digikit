"""Fixtures of the patchkit smoke suite: real daemons need the built `digi-emud`, the firmware and fresh ready inputs
(see tools/pk_harness.py); the tests that need them skip, with the reason, when a setup problem is reported."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import pk_harness  # noqa: E402


@pytest.fixture(scope="module")
def stock_session(request):
    """A daemon on the stock firmware of a device (parametrise indirectly with the device, or "device:version")."""
    device, _, version = getattr(request, "param", "dt2").partition(":")
    version = version or {"dt2": "1.17", "dn2": "1.12"}[device]
    cell = pk_harness.Cell(device, version, [])
    session = None
    try:
        session = pk_harness.Session(
            cell, pk_harness.Goldens(), run_name=f"pytest-{device}-{version}"
        )
        session.open()
    except pk_harness.HarnessError as e:
        pytest.skip(str(e))
    yield session
    session.close()
