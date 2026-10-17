"""Shared by the tests of tests/digikit: the (scenario, image) cases of a kind at the tier DIGIKIT_TIER."""

import os

import pytest

from tools.firmware import config
from tools.patchkit import pk_digikit_scen as S
from tools.patchkit.pk_dkrt import DEVICE_SYX

TIER = os.environ.get("DIGIKIT_TIER", "quick")


def cases(kinds: tuple[str, ...], device: str | None = None):
    out = []
    for scen in S.selected(TIER):
        if scen.kind not in kinds:
            continue
        for image in scen.images(TIER):
            if device and image[:3] != device:
                continue
            if image[:3] in scen.devices or scen.data["scenario"].get("images"):
                out.append(pytest.param(scen, image, id=f"{scen.name}-{image}"))
    return out


def run(scen, image, tmp_path):
    if not config.stock(DEVICE_SYX[image]).exists():
        pytest.skip(f"{DEVICE_SYX[image]} missing")
    report = S.run_scenario(scen, image, tmp_path)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert report["pass"], failed
