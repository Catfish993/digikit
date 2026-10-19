"""tools/coldfire/regions.py static analysis on the extracted sections (skipped without them)."""

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.coldfire import regions  # noqa: E402


def _need(image):
    if not (ROOT / "out" / "sections" / image / "section_3_MAIN_OS.bin").exists():
        pytest.skip(f"needs out/sections/{image}")


@pytest.mark.parametrize(
    "image, clear, copies_end, cave",
    [
        ("dt2-1.17", (0x40312000, 0x47E28470), 0x4031FF60, (0x403117C4, 2108)),
        ("dn2-1.12", (0x402FD000, 0x466B84D0), 0x4030C980, (0x402FC7A8, 2136)),
    ],
)
def test_reset_facts_and_flash_cave(image, clear, copies_end, cave):
    _need(image)
    r = regions.analyse(image)
    f = r["reset"]
    assert (f["clear_start"], f["clear_end"]) == clear
    assert f["sp0"] == 0x48000000
    assert f["copies"][-1]["end"] == copies_end == f["image_end"]
    assert f["calls"] == {"copy": 0x4000053E, "clear": 0x40000542}
    assert f["cache"] == {"cacr": 0xA50CE100, "acr0": 0x4007E020}
    first = r["flash_caves"][0]
    assert (first["start"], first["size"]) == cave and first["ends_at_clear_start"]
    assert r["heap"]["arena_end"] - r["heap"]["arena"] == 0x2000000


def test_expansion_candidates_are_above_the_dma_arena():
    for image in regions.IMAGES:
        _need(image)
        r = regions.analyse(image)
        e, d, f = r["expansion"], r["dma_arena"], r["reset"]
        assert e["start"] > d["last"] > f["clear_end"]
        assert e["end"] < f["sp0"]
        assert e["size"] >= 0x100000
