"""The smoke tour over the matrix (slow; needs digi-emud, the firmware and fresh ready inputs).

    uv run python -m pytest tests/patchkit/smoke --slow            # all cells, goldens compared
    uv run python tools/pk_harness.py run --update                 # (re)record the goldens

Plus the teeth: the harness must see a fault, a stall and a DSP halt when they happen (`mem.cf.poke` breaks the guest
on purpose).
"""

from __future__ import annotations

import pytest

import pk_harness as h

pytestmark = pytest.mark.slow

CELLS = h.load_matrix(h.DEFAULT_MATRIX)


@pytest.mark.parametrize("cell", CELLS, ids=[c.name for c in CELLS])
def test_matrix_cell(cell):
    ok, line, session = h.run_cell(cell, h.Goldens())
    if line.startswith("FAIL") and ": setup:" in line:
        pytest.skip(line)
    assert ok, (
        line
        + "\n"
        + "\n".join(f"{c.name}: {c.detail}" for c in session.checks if not c.ok)
    )


def _fails_with(session, steps, word):
    ok = session.run_segment("teeth", steps)
    bad = [c for c in session.checks if not c.ok and c.name == "teeth/health"]
    assert not ok and bad and word in bad[-1].detail, [
        c.detail for c in session.checks[-3:]
    ]
    return bad[-1].detail


@pytest.mark.parametrize("stock_session", ["dt2", "dn2"], indirect=True)
def test_a_stalled_main_loop_is_a_failure(stock_session):
    def hang(s):  # bra.s . after the loop's call: the heartbeat stops
        s.emu.poke32(s.emu.status()["cf"]["mainloop_pc"] + 12, 0x60FE4E71)

    detail = _fails_with(stock_session, [{"_call": hang}, {"wait_ms": 1500}], "stall")
    assert "mainloop_hits" in detail
    assert stock_session.wait_event("stall")
    stock_session.events.clear()


@pytest.mark.parametrize("stock_session", ["dt2", "dn2"], indirect=True)
def test_a_guest_fault_is_a_failure(stock_session):
    def crash(s):  # an illegal instruction at the loop head
        s.emu.poke32(s.emu.status()["cf"]["mainloop_pc"], 0xFFFFFFFF)

    _fails_with(stock_session, [{"_call": crash}, {"wait_ms": 200}], "fault")
    assert stock_session.wait_event("fault")
    stock_session.events.clear()


_PATTERN_STEPS = [
    {"tap": ["PTN", "TRIG 3"]},
    {"tap": "PLAY"},
    {"wait_ms": 1500},
]


@pytest.mark.parametrize("stock_session", ["dt2", "dt2:1.16"], indirect=True)
def test_play_in_patterns_3_and_4_runs_clean(stock_session):
    """DT2 1.17 and 1.16 stock: PLAY in pattern 3 or 4 used to halt the generated SHARC core on a conditional Type 7a
    (`IF EQ modify(I4, M4)` at 0x1cbc93, finding 17); the core models it now (finding 05)."""
    for trig in ("TRIG 3", "TRIG 4"):
        steps = [{"tap": ["PTN", trig]}, {"tap": "PLAY"}, {"wait_ms": 1500}]
        assert stock_session.run_segment(f"pattern-{trig}", steps), [
            c.detail for c in stock_session.checks if not c.ok
        ]


@pytest.mark.parametrize("stock_session", ["dt2"], indirect=True)
def test_a_dsp_halt_is_a_failure(stock_session):
    """The harness must see a DSP halt: an undecodable word where pattern 3 executes the conditional MODIFY."""

    def break_dsp(s):
        s.emu.patch(text="PM 1cbc93 ffffffffffff")

    steps = [{"_call": break_dsp}, *_PATTERN_STEPS]
    if stock_session.run_segment("teeth", steps):
        pytest.skip("no halt: the patched word is not executed or not refused")
    bad = [c for c in stock_session.checks if not c.ok]
    assert "DSP halt" in bad[-1].detail, bad[-1].detail
    assert stock_session.wait_event("halt")
