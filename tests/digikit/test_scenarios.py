"""The scenario files of the digikit boot tests are well formed (tests/digikit/scenarios/*.toml, runner tools/patchkit/pk_digikit_scen.py).

The scenarios themselves run in `test_equiv.py`, `test_roundtrip.py` and `test_safe.py` (one file per group, so that verify runs them in
parallel); the tier is DIGIKIT_TIER: `quick` (default; the verify gate) or `full` (`verify --full`, merges to main).
"""

from tools.patchkit import pk_digikit_scen as S
from tools.patchkit.pk_dkrt import DEVICE_SYX


def test_scenario_files_are_well_formed():
    scens = S.load_scenarios()
    assert scens, "no scenario files"
    for s in scens:
        for image in s.images("full"):
            assert image in DEVICE_SYX, (s.name, image)
    # every kind has a scenario
    assert {s.kind for s in scens} == set(S.KINDS)
    # the quick tier (the verify gate) covers equivalence and the round trip; safe mode and the power cut run in the full tier
    assert {s.kind for s in S.selected("quick")} >= {"equiv", "roundtrip", "upload"}
    assert {s.kind for s in S.selected("full")} == set(S.KINDS)
