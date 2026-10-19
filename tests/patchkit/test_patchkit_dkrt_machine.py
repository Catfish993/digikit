"""Run-time machines with DSP code (core/dyndsp, the `.dkm` DSP section, the runtime's DSP loader; finding 32).

Static checks: the committed objects are current, the DSP section is deterministic, its relocation agrees in three places
(the Rust reference loader behind `patchkit dkm-dsp-image`, this file's own, and the loader's checks), and the two sides of the
DSP area's size agree. The emulator proofs are the boot-time equivalence scenarios (tests/digikit, equiv-dyn-pluck and equiv-dn2-set: a selected
module against the patched-in one, PCM sample for sample); the hot-load proof that was here is archived."""

import re
import tomllib

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.patchkit import (  # noqa: E402
    pk_build,
    pk_dkrt,
    pk_dkrt_machine,
    pk_toolchains,
)

MODS = ROOT / "modules"
needs_selache = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None, reason="SHARC+ assembler not installed"
)


@needs_selache
@pytest.mark.parametrize(
    "mod",
    ["core/dsp-link", "core/dyndsp", "test/dyn-pluck", "test/dyn-sine-dn2"],
)
def test_committed_objects_are_current(mod):
    pk_build.build_module(MODS / mod, check=True)


def test_the_area_size_is_the_same_on_both_sides():
    # One source: `[digikit] dsp_area_bytes` of the device file, generated into both sides by `patchkit gen`.
    files = {"dt2": "digitakt-ii.toml", "dn2": "digitone-ii.toml"}
    for dev, name in files.items():
        want = tomllib.loads((ROOT / "devices" / name).read_text())["digikit"][
            "dsp_area_bytes"
        ]
        inc = (MODS / f"core/dsp-link/build/gen/device-{dev}.inc").read_text()
        words = int(re.search(r"#define DL_AREA_WORDS (\d+)", inc).group(1))
        nbytes = int(re.search(r"#define DL_AREA_BYTES (\d+)", inc).group(1))
        assert nbytes == want and words * 4 == nbytes, dev
        link = (MODS / f"core/dsp-link/src/dsp/{dev}.s").read_text()
        assert re.search(r"\.PKBSS _dl_area, DL_AREA_BYTES;", link), dev
        cf = (MODS / f"core/dyndsp/build/gen/device-{dev}.inc").read_text()
        assert (
            int(re.search(r"\.equ DL_AREA_BYTES, (0x[0-9a-f]+)", cf).group(1), 16)
            == want
        ), dev
        info = (MODS / f"core/dyndsp/src/cf/dyndsp-{dev}.s").read_text()
        assert re.search(r"\.long\s+DL_AREA_BYTES\b", info), dev
        assert nbytes <= 4 << 17  # the 17-bit offset of a chunk reaches 512 KiB


def test_dsp_section_is_deterministic_and_relocates_like_the_reference(tmp_path):
    tools = pk_dkrt.Tools()
    a, b = tmp_path / "a.dkm", tmp_path / "b.dkm"
    for out in (a, b):
        tools.patchkit_run("pack", "test/dyn-pluck", "--device", "dt2", "-o", out)
    assert a.read_bytes() == b.read_bytes()
    info = tools.patchkit_run("dkm-info", a)
    assert (
        "voice type 18 entry +0x0" in info and "imports _dv_exp2, _dv_frame_hw" in info
    )
    # the guard's ranges: DECAY, PICK, BRIGHT, COLOR (knobs 1, 2, 4, 5) declare 0 .. 0x7f00; TUNE and LEV are stock rows (undeclared)
    assert "ranges [0, 7f000000, 7f000000, 0, 7f000000, 7f000000, 0, 0]" in info
    assert (
        "runtime   >= 5" in info
    )  # DSP sections of version 2 (voices with knob ranges) need runtime 5
    sec = pk_dkrt_machine.parse_dsp_section(a.read_bytes())
    imports = {"_dv_exp2": 0xB9D7E0, "_dv_frame_hw": 0xB9D80E}
    for base in (0x2000_0000, 0x2001_AE80, 0x2003_F000):
        want = pk_dkrt_machine.relocate(sec, base, imports)
        out = tools.patchkit_run(
            "dkm-dsp-image", a, f"{base:x}", *[f"{k}={v:x}" for k, v in imports.items()]
        )
        assert bytes.fromhex(out.strip()) == want, hex(base)
    # a placement that is not 8-aligned is refused by the reference loader
    r = tools.run(tools.patchkit, "--root", ROOT, "dkm-dsp-image", a, "2000004")
    assert r.returncode != 0


def test_a_dsp_only_module_packs_for_the_digitone_ii(tmp_path):
    tools = pk_dkrt.Tools()
    out = tmp_path / "s.dkm"
    tools.patchkit_run("pack", "test/dyn-sine-dn2", "--device", "dn2", "-o", out)
    info = tools.patchkit_run("dkm-info", out)
    assert (
        "device 2 image" in info
        and "voice type 18" in info
        and "register  0 bytes" in info
    )
    # the other device has no DSP object of this module: nothing to pack
    r = tools.run(
        tools.patchkit,
        "--root",
        ROOT,
        "pack",
        "test/dyn-sine-dn2",
        "--device",
        "dt2",
        "-o",
        tmp_path / "x",
    )
    assert r.returncode != 0
