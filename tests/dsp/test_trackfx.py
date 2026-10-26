"""Track FX sweep tooling: descriptor table by signature, scenario builders,
read-back decoding. Images are skipped when absent."""

from __future__ import annotations

import struct
from typing import Any

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.coldfire import paramtable  # noqa: E402
from tools.emulator import fxcheck  # noqa: E402
from tools.emulator import scenario_dsl as d  # noqa: E402

DN2 = ROOT / "out" / "sections" / "dn2-1.12" / "section_3_MAIN_OS.bin"
DT2 = ROOT / "out" / "sections" / "dt2-1.17" / "section_3_MAIN_OS.bin"


def test_find_table_synthetic() -> None:
    sig = bytes.fromhex(
        "202f00040c800000014155c141f9401f83a47301c0812200e589ed889081203008044e75"
    )
    assert paramtable.find_table(b"\0" * 7 + sig + b"\0" * 9) == (0x401F83A4, 0x141)
    with pytest.raises(SystemExit):
        paramtable.find_table(sig + sig)


@pytest.mark.skipif(not DN2.exists(), reason="DN2 1.12 image absent")
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


@pytest.mark.skipif(not DT2.exists(), reason="DT2 1.17 image absent")
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


def test_pcm_observations_keep_exact_numpy_semantics() -> None:
    import numpy as np

    x = np.array([[0.0, 0.0], [0.25, 0.25]])
    assert fxcheck.pcm_observations(x, x.copy(), "dry") == {
        "shape": [2, 2],
        "mono_equal": True,
        "reference": "dry",
        "reference_shape": [2, 2],
        "reference_equal": True,
        "reference_max_abs_diff": 0.0,
    }
    assert not fxcheck.pcm_observations(x, x[:1], "dry")["reference_equal"]
    changed = x.copy()
    changed[1, 1] = np.nextafter(changed[1, 1], 1.0)
    got = fxcheck.pcm_observations(changed, x, "dry")
    assert not got["mono_equal"] and not got["reference_equal"]
    assert got["reference_max_abs_diff"] == float(np.abs(changed - x).max())
    assert fxcheck.pcm_observations(x, x[:1], "dry")["reference_max_abs_diff"] is None
    assert fxcheck.pcm_observations(x, None, "dry")["reference_max_abs_diff"] is None
    assert fxcheck.pcm_observations(x, None, "missing")["reference_equal"] is None


def test_json_measurements_normalizes_only_nonfinite_numbers() -> None:
    import json
    import math

    measurements: dict[str, Any] = {
        "tf-chr-0": {
            "tf": {"mod_phase_hz": math.nan, "mod_env_hz": math.inf},
            "levels": [-math.inf, 0.5],
        }
    }
    encoded = json.dumps(fxcheck.json_measurements(measurements), allow_nan=False)
    assert json.loads(encoded) == {
        "tf-chr-0": {
            "tf": {"mod_phase_hz": None, "mod_env_hz": None},
            "levels": [None, 0.5],
        }
    }
    assert math.isnan(measurements["tf-chr-0"]["tf"]["mod_phase_hz"])


def test_routing_observations_keep_stereo_difference() -> None:
    import numpy as np

    x = np.array([[0.25, -0.25], [0.5, -0.5]])
    dry = np.zeros_like(x)
    got = fxcheck.routing_compare(x, dry, 1, 0.0, 0.0)
    assert got["max_abs_diff"] == 0.0  # mono average cancels the stereo difference
    assert got["shape_equal"]
    assert got["full_max_abs_diff"] == 0.5
    got = fxcheck.routing_compare(x, dry[:1], 1, 0.0, 0.0)
    assert not got["shape_equal"]
    assert got["full_max_abs_diff"] is None
