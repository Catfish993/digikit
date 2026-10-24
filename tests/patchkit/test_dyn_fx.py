"""Run-time effects, the static side (F4 step 1; docs/design/digikit-dkm.md "DSP section", effect records): the fixture `test/dyn-fx-gain` packs into a `.dkm` whose
header says fx 1.0 and no machine, its DSP section (version 4) holds one effect record that the reference loader and the packer's own numbers agree on, and the needs
the file infers are one effect slot and no row of any other kind. The emulator proof is the scenario `fx-dyn-dt2` (tests/digikit/scenarios)."""

import struct
import tomllib

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.device import dkfit, dkneeds  # noqa: E402
from tools.patchkit import (  # noqa: E402
    modroots,
    pk_build,
    pk_device,
    pk_dkrt,
    pk_dkrt_machine,
    pk_toolchains,
)

needs_selache = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None, reason="SHARC+ assembler not installed"
)
FX = "test/dyn-fx-gain"


@pytest.fixture(scope="module")
def packed(tmp_path_factory):
    tools = pk_dkrt.Tools()
    out = tmp_path_factory.mktemp("dynfx") / "fx.dkm"
    tools.patchkit_run("pack", FX, "--device", "dt2", "-o", out)
    again = out.with_name("fx2.dkm")
    tools.patchkit_run("pack", FX, "--device", "dt2", "-o", again)
    assert out.read_bytes() == again.read_bytes()  # deterministic
    return tools, out


@needs_selache
@pytest.mark.parametrize("mod", ["core/dynfx", FX])
def test_committed_objects_are_current(mod):
    pk_build.build_module(modroots.module_dir(mod), check=True)


def test_the_header_says_fx_and_not_machine(packed):
    _, out = packed
    b = out.read_bytes()
    sdk = tomllib.loads((ROOT / "sdk" / "sdk.toml").read_text())["contract"]
    assert tuple(b[0x38:0x40]) == (*sdk["api"], 0, 0, *sdk["fx"], 0, 0)
    assert sdk["fx"][0] != 0  # the runtime implements the contract


def test_the_effect_record_is_what_the_manifest_and_the_descriptor_say(packed):
    tools, out = packed
    sec = pk_dkrt_machine.parse_dsp_section(out.read_bytes())
    assert sec["voices"] == [] and len(sec["fxs"]) == 1
    e = sec["fxs"][0]
    # one parameter at 0.5f, 4 bytes of state; the process routine is the first routine of the object
    assert (e["n_params"], e["state_bytes"]) == (1, 4)
    assert e["defaults"] == [0x3F00_0000, 0, 0, 0, 0, 0, 0, 0]
    assert e["process"] % 2 == 0 and e["process"] < len(sec["image"])
    # its descriptor in the image: DKFX, contract 1, the same parameters and state
    d = struct.unpack_from("<7I", sec["image"], e["desc"])
    assert d[:4] == (0x444B4658, 1, 1, 4)
    info = tools.patchkit_run("dkm-info", out)
    assert "effect process" in info and "defaults [3f000000]" in info
    # the reference loader and the second implementation of the relocation agree
    for base in (0x2000_0000, 0x2001_AE80, 0x2003_F000):
        want = pk_dkrt_machine.relocate(sec, base, {})
        got = tools.patchkit_run("dkm-dsp-image", out, f"{base:x}")
        assert bytes.fromhex(got.strip()) == want, hex(base)


def test_the_inferred_needs_are_one_effect_slot_and_the_state_in_the_range(packed):
    _, out = packed
    b = out.read_bytes()
    n = dkneeds.needs_of(b)
    assert n["FX_SLOTS"] == 1
    assert (n["MACHINE_ROWS"], n["VOICE_ROWS"], n["PARAM_ROWS"], n["PAGE_ROWS"]) == (
        0,
        0,
        0,
        0,
    )
    sec = pk_dkrt_machine.parse_dsp_section(b)
    # image, bss and the state (4 bytes -> 8) rounded to the 64-byte granule: the packer's `range_len` and the file's needs agree
    want = (len(sec["image"]) + sec["bss"] + 7) & ~7
    want = (want + 8 + 63) & ~63
    assert n["DSP_AREA"] == want


def test_the_fit_rule_counts_the_effect_slots_of_the_device(packed):
    _, out = packed
    n = dkneeds.needs_of(out.read_bytes())
    slots = pk_device.limits("dt2").fx_slots
    assert slots == 3  # the Digitakt II profile: three run-time effects
    caps = {"FX_SLOTS": {"limit": slots, "used": 0, "largest": 0}}
    assert dkfit.fits_now(n, caps) is None
    caps["FX_SLOTS"]["used"] = slots
    short = dkfit.fits_now(n, caps)
    assert short == ("FX_SLOTS", 1, 0)
    assert dkfit.describe(short) == "effect slots 1 needed, 0 free"
    assert dkfit.menu_line(short) == "FX 1 > 0 FREE"
    # (a set of four effects stops at the fourth: the "sets" vector of sdk/fit-vectors.json, read by tests/digikit/test_dkfit.py)


def test_the_effect_is_not_packable_for_the_digitone_yet(packed, tmp_path):
    tools, _ = packed
    r = tools.run(
        tools.patchkit,
        "--root",
        ROOT,
        "pack",
        FX,
        "--device",
        "dn2",
        "-o",
        tmp_path / "fx-dn2.dkm",
    )
    assert r.returncode != 0
