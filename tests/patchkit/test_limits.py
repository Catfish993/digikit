"""The capacity limits come from the device files (docs/plans/capacity-isolation.md): the Python side reads `[limits]` and the contract
maxima of sdk/sdk.toml, and the tools keep no copy of a number."""

import pytest

from tools.patchkit import pk_conform, pk_device, pk_dkrt, pk_dkrt_boot

DEVICES = pk_device.digikit_devices()


@pytest.mark.parametrize("dev", DEVICES)
def test_the_limits_are_the_device_files_and_within_the_contract_maxima(dev):
    lim = pk_device.limits(dev)
    d = pk_device.load(dev)
    assert lim.modules == d["limits"]["modules"]
    assert lim.dsp_area_bytes == d["digikit"]["dsp_area_bytes"]
    mx = pk_device.contract_max()
    for key in ("modules", "fx_slots", "param_rows", "page_rows", "dsp_area_bytes"):
        assert getattr(lim, key) <= mx[key], (dev, key)
    assert lim.modules <= mx["sel_max"]
    # a Digitone II has page rows; the Digitakt II none
    assert (lim.page_rows > 0) == (dev == "dn2")


def test_shared_limits_agree_between_devices():
    lim = pk_device.shared_limits()
    for dev in DEVICES:
        o = pk_device.limits(dev)
        assert (
            o.modules,
            o.machine_rows,
            o.arena_bytes,
            o.stage_bytes,
            o.param_rows,
        ) == (
            lim.modules,
            lim.machine_rows,
            lim.arena_bytes,
            lim.stage_bytes,
            lim.param_rows,
        )


def test_the_boot_tools_use_the_profile_not_a_copy():
    lim = pk_device.shared_limits()
    assert lim.modules == pk_dkrt.MAX_MODULES
    assert lim.arena_bytes == pk_dkrt.ARENA_BYTES
    assert (
        pk_dkrt.SEL_MAX == pk_dkrt_boot.SEL_MAX == pk_device.contract_max()["sel_max"]
    )
    assert max(pk_device.limits(d).fx_slots for d in DEVICES) == pk_conform.FX_MAX
