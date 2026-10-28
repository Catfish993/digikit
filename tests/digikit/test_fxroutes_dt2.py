"""The ROUTE... choice of the MODULES side panel and the manifest's route fields, Digitakt II (tests/digikit/scenarios/fx-routes-dt2.toml): the default route from the manifest, the routes the choice offers, APPLY & RESTART? and the route in effect after it, the FX view's order."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxroutes",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
