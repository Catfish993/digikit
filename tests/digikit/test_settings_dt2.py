"""The settings store of the runtime, Digitakt II (tests/digikit/scenarios/settings-dt2.toml): written 2 s after the last change to the copy that is not current, read back after a restart, decoded by `patchkit store dump`."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("settings",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
