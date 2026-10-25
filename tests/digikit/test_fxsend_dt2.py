"""A run-time effect on the send route, Digitakt II (tests/digikit/scenarios/fx-send-dt2.toml): no amount leaves the PCM dry, an amount on the playing track adds the effect's return, the effect runs once a block."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxsend",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
