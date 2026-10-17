"""Equivalence of a boot-loaded set with its patched-in twin, Digitone II (tests/digikit/scenarios/*.toml): one test per scenario and image, tier DIGIKIT_TIER. One file per group, so that verify runs them in parallel."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("equiv",), "dn2"))
def test_scenario(scen, image, tmp_path):
    run(scen, image, tmp_path)
