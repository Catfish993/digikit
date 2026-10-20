"""pcm_pin (tools/patchkit/pk_dkrt_machine.py): the SHA-256 of the run-time note from its onset."""

import hashlib

import numpy as np

from tools.patchkit import pk_dkrt_machine as M


class FakeRig:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))


def note(lead: int) -> np.ndarray:
    x = np.zeros(40000, dtype=np.float32)
    x[lead:] = np.sin(np.arange(40000 - lead) * 0.05)
    x[lead] = 0.5
    return x


def test_the_pin_ignores_the_silence_before_the_onset():
    n = 1000
    a, b = note(100), note(300)
    want = hashlib.sha256(a[100 : 100 + n].astype("<f4").tobytes()).hexdigest()
    for x in (a, b):
        rig, rep = FakeRig(), {}
        M.pcm_pin(rig, x, want, rep, "voice", n)
        assert rig.checks[0][1], rig.checks
        assert rep["pcm_pin"]["sha256"] == want


def test_a_wrong_pin_a_record_request_and_an_odd_onset_fail():
    for pin, x in (("0" * 64, note(100)), ("record", note(100)), ("", note(101))):
        rig = FakeRig()
        M.pcm_pin(rig, x, pin, {}, "voice", 1000)
        assert not rig.checks[0][1]
