"""The send page of the track FX key, Digitakt II (tests/digikit/scenarios/fx-route-dt2.toml): the route from the settings, the knob for each send effect, the amounts in the settings and the DSP row, kit loads, the send-slot limit, no p-lock and no CC."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxroute",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
