"""Remaining full-tier sample and input-adapter live scenarios, Digitone II; quick tier has no live cases. CLI transfer acceptance belongs to tests/regression/runtime/transfer-live.ts."""

import pytest
from scen_cases import cases, run


@pytest.mark.parametrize(("scen", "image"), cases(("live",), "dn2"))
def test_scenario(scen, image, tmp_path_factory, request):
    run(scen, image, tmp_path_factory, request)
