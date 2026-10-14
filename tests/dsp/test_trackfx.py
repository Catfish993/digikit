"""Track FX sweep tooling: descriptor table by signature, scenario builders,
read-back decoding. Images are skipped when absent."""

from __future__ import annotations

import struct

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.coldfire import paramtable  # noqa: E402
from tools.emulator import fxcheck, gen_trackfx  # noqa: E402
from tools.emulator import scenario_dsl as d  # noqa: E402

DN2 = ROOT / "out" / "sections" / "dn2-1.11" / "section_3_MAIN_OS.bin"
DT2 = ROOT / "out" / "sections" / "dt2-1.16" / "section_3_MAIN_OS.bin"


def test_find_table_synthetic() -> None:
    sig = bytes.fromhex(
        "202f00040c800000014155c141f9401f7f947301c0812200e589ed889081203008044e75"
    )
    assert paramtable.find_table(b"\0" * 7 + sig + b"\0" * 9) == (0x401F7F94, 0x141)
    with pytest.raises(SystemExit):
        paramtable.find_table(sig + sig)


@pytest.mark.skipif(not DN2.exists(), reason="DN2 1.11 image absent")
def test_descriptors_dn2() -> None:
    desc = paramtable.descriptors(DN2)
    assert len(desc) == 0x141
    by = {e.code: e for e in desc}
    bit = by[0x12D]
    assert (bit.long_name, bit.short_name, bit.owner_name) == (
        "Bit Reduction",
        "BR",
        "FX",
    )
    assert (bit.mirror_index, bit.min, bit.max, bit.default) == (95, 0, 0x7F00, 0)
    assert by[0x71].default == 0x1700 and by[0x71].mirror_index == 32
    snd, fx = fxcheck.name_maps(DN2)
    assert snd[95] == "FX Bit Reduction" and snd[86] == "FX Delay Send"
    assert fx[32] == "Delay Delay Time" and fx[68] == "Master Master Overdrive"


@pytest.mark.skipif(not DT2.exists(), reason="DT2 1.16 image absent")
def test_descriptors_dt2() -> None:
    desc = paramtable.descriptors(DT2)
    assert len(desc) == paramtable.COUNT
    assert desc[0xFF].long_name == "Bit Reduction" and desc[0xFF].mirror_index == 64
    assert desc[0x44].long_name == "Delay Send" and desc[0x44].mirror_index == 55


def test_turn_sweep_fixed_clock() -> None:
    a, b = d.Timeline(), d.Timeline()
    d.turn_sweep(a, 3, 0, 20)
    d.turn_sweep(b, 3, 7, 20)
    ta = [e["at_instr"] for e in a.as_list()]
    assert ta == [e["at_instr"] for e in b.as_list()]
    assert len(ta) == 20
    assert sum(e["detents"] for e in b.as_list()) == 28
    assert all(e["detents"] == 0 for e in a.as_list())


def test_generated_scenarios_share_timing() -> None:
    sc = gen_trackfx.scenarios()
    names = [s["name"] for s in sc]
    assert len(names) == len(set(names))
    sc = [s for s in sc if "flt" not in s["name"]]  # the filter prelude shifts them
    for kind in ("hold", "tap"):
        group = [s for s in sc if (s["seconds"] < 4.5) == (kind == "hold")]
        assert len(group) > 5
        times = [e["at_instr"] for e in group[0]["events"]]
        assert times[0] == 1_000_000
        # Same number of events and same times, except the routing window.
        for s in group:
            assert len(s["events"]) == len(group[0]["events"])
            assert [e["at_instr"] for e in s["events"]] == times


def test_decode_and_diff() -> None:
    snd = [0] * 101
    snd[95] = 0x2C00
    names: tuple[dict[int, str], dict[int, str]] = ({95: "FX Bit Reduction"}, {})
    pk = {"snd": struct.pack(">101h", *snd).hex()}
    ref = fxcheck.decode_arrays(
        {"snd": struct.pack(">101h", *([0] * 101)).hex()}, names
    )
    got = fxcheck.decode_arrays(pk, names)
    assert fxcheck.readback_diff(got, ref) == {
        "snd[95] FX Bit Reduction": "0x0000 -> 0x2c00 (UI 44)"
    }
    assert fxcheck.snd_ui(got, 95) == 44
