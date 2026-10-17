"""Module upload scenarios, Digitakt II (tests/digikit/scenarios/upload-*.toml, runner tools/patchkit/pk_digikit_upload.py): one test per scenario and image, tier DIGIKIT_TIER."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("upload",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
