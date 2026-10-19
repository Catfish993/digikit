"""The pure parts of tools/device/dkrun.py: the runtime check and the restart detection from the device's log (no emulator)."""

import pytest

from tools.device import dklog as L
from tools.device import dkrun as R
from tools.device import dkupload as D

UP = L.K["UPLOAD"]


def ev(kind, a0=0, a1=0, flags=0, seq=1):
    return (seq, 0, kind, flags, (a0, a1, 0, 0, 0))


def test_an_old_runtime_is_refused_with_the_way_out():
    info = {"runtime": 3, "card_read_only": False}
    with pytest.raises(R.RunError, match="flash a runtime-4 image"):
        R.require_runtime(info)
    R.require_runtime({"runtime": 4, "card_read_only": False})
    with pytest.raises(R.RunError, match="writes nothing"):
        R.require_runtime({"runtime": 4, "card_read_only": True})


def test_restart_detection_reads_the_log():
    boot = ev(L.K["BOOT_START"], 4)
    asked = ev(UP, D.CMD_RESTART, 0)
    assert (
        R.restarted([boot, ev(UP, D.CMD_SELECT), asked]) is False
    )  # the old boot is still running
    assert (
        R.restarted([boot, ev(UP, D.CMD_SELECT)]) is True
    )  # a new boot never saw the request
    assert R.restarted([]) is False  # nothing booted yet
    refused = ev(UP, D.CMD_RESTART, 0xFFFFFFD5, flags=1)
    with pytest.raises(R.RunError, match="refused"):
        R.restarted([boot, asked, refused])


def test_wait_new_boot_retries_through_a_vanished_port():
    calls = {"n": 0}

    class Up:
        def log_page(self, first):
            return 0, 0, []

    def connect():
        calls["n"] += 1
        if calls["n"] < 3:
            raise SystemExit("no MIDI port")
        return Up()

    t = {"v": 0.0}
    boots = iter([[ev(UP, D.CMD_RESTART)], [ev(L.K["BOOT_START"], 4)]])

    orig = L.read_all
    L.read_all = lambda up, first=0: (1, 1, next(boots))
    try:
        up = R.wait_new_boot(
            connect, lambda s: t.__setitem__("v", t["v"] + s), 90.0, lambda: t["v"]
        )
    finally:
        L.read_all = orig
    assert isinstance(up, Up) and calls["n"] == 4
