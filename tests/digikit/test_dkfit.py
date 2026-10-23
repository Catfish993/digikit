"""The fit rule of tools/device/dkfit.py on the shared vectors (sdk/fit-vectors.json); dkrt_fit and apps/web/src/modules/dkfit.ts read the same file."""

import json
from pathlib import Path

import pytest

from tools.device import dkfit as F

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "sdk" / "fit-vectors.json").read_text()
)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_fits_now_on_the_vectors(case):
    caps = F.vector_caps(VECTORS["caps_base"], case.get("caps"))
    got = F.fits_now(case["needs"], caps)
    assert (list(got) if got else None) == case["want"]


@pytest.mark.parametrize("case", VECTORS["sets"], ids=lambda c: c["name"])
def test_fits_set_on_the_vectors(case):
    caps = F.vector_caps(VECTORS["caps_base"], case.get("caps"))
    needs = [
        {k: v for k, v in n.items() if k != "times"}
        for n in case["needs"]
        for _ in range(n.get("times", 1))
    ]
    got = F.fits_set(needs, caps)
    assert (list(got) if got else None) == case["want"]


def test_take_leaves_the_capacity_it_says():
    caps = F.vector_caps(VECTORS["caps_base"], None)
    left = F.take(
        {
            "ARENA": 32,
            "ARENA_XFER": 1392,
            "DSP_AREA": 265600,
            "DSP_ALIGN": 64,
            "VOICE_ROWS": 1,
        },
        caps,
    )
    assert left["ARENA"]["used"] == 1424 and left["ARENA"]["largest"] == 524272 - 1424
    assert left["DSP_AREA"]["used"] == 265656  # the alignment room is taken too
    assert left["MODULES"]["used"] == 1 and left["VOICE_ROWS"]["used"] == 1
    assert caps["MODULES"]["used"] == 0  # the input is not changed


def test_a_short_resource_reads_as_a_line():
    assert (
        F.describe(("DSP_AREA", 409600, 260096))
        == "DSP memory 400 KiB needed, 254 KiB free"
    )
    assert F.describe(("ARENA", 1025, 1535)) == "CPU memory 2 KiB needed, 1 KiB free"
    assert F.describe(("PARAM_ROWS", 3, 2)) == "parameter rows 3 needed, 2 free"
    assert F.describe(("STAGE", 70000, 65536)) == "file 69 KiB needed, 64 KiB at most"


def test_empty_caps_on_the_vector():
    got = F.empty_caps(F.vector_caps(VECTORS["empty"]["from"], None))
    assert got == F.vector_caps(VECTORS["empty"]["want"], None)


def test_the_menu_lines_are_the_ones_the_page_shows():
    # modules/core/digikit-menu `short_text`: the popup form and the RESULT row's compact form
    assert F.menu_line(("DSP_AREA", 409600, 260096)) == "DSP 400K > 254K FREE"
    assert F.menu_line(("DSP_AREA", 409601, 260096), compact=True) == "DSP 401K>254K"
    assert F.menu_line(("ARENA", 600000, 100)) == "CPU 586K > 0K FREE"
    assert F.menu_line(("PARAM_ROWS", 3, 2)) == "PARAMS 3 > 2 FREE"
    assert F.menu_line(("STAGE", 70000, 65536)) == "FILE 69K > 64K MAX"


@pytest.mark.parametrize("case", VECTORS["load_lines"], ids=lambda c: c["name"][:50])
def test_the_load_line_on_the_vectors(case):
    caps = {
        k: {"limit": v[0], "used": v[1], "largest": v[2]}
        for k, v in case["caps"].items()
    }
    assert F.load_line(caps) == case["want"]
    assert (
        len(F.load_line(caps)) <= 17
    )  # `LINE_MAX`: what the list shows of a row is about 18 characters


def test_the_load_line_of_a_selection_takes_the_ticked_modules_out_of_the_limits():
    caps = F.vector_caps(VECTORS["caps_base"], None)
    left = F.take(
        {
            "ARENA": 32,
            "ARENA_XFER": 1392,
            "DSP_AREA": 265600,
            "DSP_ALIGN": 64,
            "MACHINE_ROWS": 1,
        },
        F.empty_caps(caps),
    )
    # 265656 of 524288 is 50.67 %, up to 51; the arena (1424 of 524288) and the module record (1 of 8, 12.5 %) follow
    assert F.load_line(left) == "DSP 51% MOD 13%"
