"""The frequency shifter digikit/freqshift on a Digitakt II (tests/digikit/scenarios/fx-freqshift-dt2.toml): the page and its knobs, the sine moves by the shift in both directions with the image 40 dB down,
loading the effect with MIX 0 leaves the PCM as it was, Save Kit and Load Kit give SHIFT back. One file per group, so that verify runs them in parallel."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxshift",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
