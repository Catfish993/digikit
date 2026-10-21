"""Free-running (desktop-like) daemon scenarios, Digitone II (tests/digikit/scenarios/transfer-live.toml, runner `run_live` of tools/patchkit/pk_digikit_scen.py): one test per scenario and image, tier DIGIKIT_TIER."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("live",), "dn2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
