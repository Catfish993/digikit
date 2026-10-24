"""Effect values saved with the kit, Digitone II (tests/digikit/scenarios/fx-persist-dn2.toml): Save Kit stores the knob values and the effect IDs in the kit's spare bytes, Load Kit restores them
to the same effect and gives another effect, an empty row or a stock kit the defaults, with no zero on the way to the DSP. One file per group, so that verify runs them in parallel."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxpersist",), "dn2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
