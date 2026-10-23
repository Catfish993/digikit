"""The pure parts of tools/device/dkrun.py: the runtime check and the restart detection from the device's log (no emulator)."""

import struct

import pytest

from tools.device import dklog as L
from tools.device import dkproto
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


def _dkm(dsp_area=0, size=600):
    """A packed module with the needs in its header (docs/design/digikit-dkm.md) and nothing else of it."""
    h = bytearray(size)
    h[:4] = b"DKMF"
    struct.pack_into(">I", h, 8, size)
    for name, value in {
        "ARENA": 64,
        "DSP_AREA": dsp_area,
        "DSP_ALIGN": 8 if dsp_area else 0,
    }.items():
        struct.pack_into(">I", h, dkproto.DKM_NEEDS[name], value)
    return bytes(h)


CAPS = {
    "MODULES": {"limit": 8, "used": 5, "largest": 0},
    "SELECTION": {"limit": 8, "used": 5, "largest": 0},
    "ARENA": {"limit": 524288, "used": 400000, "largest": 100000},
    "STAGE": {"limit": 65536, "used": 0, "largest": 65536},
    "DSP_AREA": {"limit": 524288, "used": 400000, "largest": 100000},
}


class FitUp:
    """The reads `check_fit` and the start of `run` make."""

    def __init__(self, caps=CAPS, with_cap=True):
        self.caps = caps
        self.info_ = {
            "runtime": 0x000300,
            "card_read_only": False,
            "port": "dt2",
            "free_bytes": 1,
            "safe": False,
        }
        self.info_["caps"] = (1 << dkproto.CAP["CAPACITY"]) if with_cap else 0
        self.sent = []

    def info(self):
        return self.info_

    def capacity(self):
        return self.caps

    def select(self, paths):
        self.sent.append(("select", paths))


def test_a_module_that_will_not_fit_the_empty_device_is_named():
    up = FitUp()
    # the next boot is empty: what is loaded now does not count, so 400000 bytes of DSP area used is no reason
    assert R.check_fit(up, up.info(), _dkm(dsp_area=300_000)) is None
    why = R.check_fit(up, up.info(), _dkm(dsp_area=600_000))
    assert why == "DSP area 586 KiB needed, 511 KiB free"
    # no CAPACITY on the device: no verdict
    assert (
        R.check_fit(
            FitUp(with_cap=False), FitUp(with_cap=False).info(), _dkm(dsp_area=600_000)
        )
        is None
    )


def test_run_refuses_a_module_that_will_not_fit_unless_forced(
    tmp_path, monkeypatch, capsys
):
    f = tmp_path / "big.dkm"
    f.write_bytes(_dkm(dsp_area=600_000))
    up = FitUp()
    monkeypatch.setattr(
        R,
        "upload_if_changed",
        lambda up, path, data: up.sent.append(("put", path)) or True,
    )
    with pytest.raises(
        R.RunError,
        match=r"will not fit this device: DSP area 586 KiB needed, 511 KiB free \(--force uploads it anyway\)",
    ):
        R.run(up, "test/big", dkm=f, restart=False)
    assert up.sent == []  # nothing was uploaded, nothing selected
    R.run(up, "test/big", dkm=f, restart=False, force=True)
    assert up.sent == [("put", "modules/big.dkm"), ("select", ["modules/big.dkm"])]
    assert "WARNING test/big will not fit this device" in capsys.readouterr().out
