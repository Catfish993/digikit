"""`module:check` prints the declared needs of the packed module and whether it fits an empty device (tools/patchkit/pk_modcheck.py)."""

import struct

from tools.device import dkproto
from tools.patchkit import pk_device, pk_modcheck


def dkm(**needs):
    h = bytearray(600)
    h[:4] = b"DKMF"
    struct.pack_into(">I", h, 8, len(h))
    for k, v in needs.items():
        struct.pack_into(">I", h, dkproto.DKM_NEEDS[k], v)
    return bytes(h)


def test_device_caps_are_the_limits_of_the_device_file():
    for dev in pk_device.digikit_devices():
        lim = pk_device.limits(dev)
        c = pk_modcheck.device_caps(dev)
        assert c["DSP_AREA"] == {"limit": lim.dsp_area_bytes, "used": 0, "largest": 0}
        assert (
            c["ARENA"]["limit"],
            c["MODULES"]["limit"],
            c["PAGE_ROWS"]["limit"],
        ) == (
            lim.arena_bytes,
            lim.modules,
            lim.page_rows,
        )
        assert all(v["used"] == 0 for v in c.values())


def test_a_module_that_fits_and_one_that_does_not():
    ok = pk_modcheck.needs_lines(
        dkm(ARENA=48, DSP_AREA=4096, DSP_ALIGN=8, VOICE_ROWS=1), "dt2"
    )
    assert ok[0] == "dt2: needs    arena 48, DSP area 4096, voices 1"
    assert ok[1] == "dt2: fits     fits an empty device"
    big = pk_device.limits("dt2").dsp_area_bytes + 64
    no = pk_modcheck.needs_lines(
        dkm(ARENA=48, DSP_AREA=big, DSP_ALIGN=8, VOICE_ROWS=1), "dt2"
    )
    assert no[1].startswith(
        "dt2: fits     DOES NOT FIT even an empty device: DSP area "
    )
    # a module with nothing declared says so
    assert (
        pk_modcheck.needs_lines(dkm(), "dt2")[0] == "dt2: needs    nothing but its code"
    )
