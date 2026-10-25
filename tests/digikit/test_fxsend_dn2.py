"""A run-time effect on the send route, Digitone II (tests/digikit/scenarios/fx-send-dn2.toml): no amount leaves the PCM dry, an amount on the playing track adds the effect's return, the effect runs once a block."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxsend",), "dn2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
