"""The fit rule of tools/device/dkfit.py on the shared vectors (sdk/fit-vectors.json); dkrt_sdk::fit and apps/web/src/modules/dkfit.ts read the same file."""

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
        == "DSP area 400 KiB needed, 254 KiB free"
    )
    assert F.describe(("ARENA", 1025, 1535)) == "memory 2 KiB needed, 1 KiB free"
    assert F.describe(("PARAM_ROWS", 3, 2)) == "parameter rows 3 needed, 2 free"
