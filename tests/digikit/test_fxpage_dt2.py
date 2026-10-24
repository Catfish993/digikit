"""Effect pages of run-time effects, Digitakt II (tests/digikit/scenarios/fx-pages-dt2.toml): the stock FX pages and PCM are unchanged with no effect loaded, the effect has a fourth page whose knobs
change the PCM as declared (MIX 0 is the dry sound bit for bit), deselecting it and restarting brings the stock view back. One file per group, so that verify runs them in parallel."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("fxpage",), "dt2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
