"""Run-time effects, Digitone II (tests/digikit/scenarios/fx-dyn-dn2.toml): the effect selected at boot sits in the chain and scales the PCM, deselect and restart gives the dry PCM again, one effect more than the slots is refused. One file per group, so that verify runs them in parallel."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fx",), "dn2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
