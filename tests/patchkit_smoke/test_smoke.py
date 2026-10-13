"""The smoke tour over the matrix (slow; needs digi-emud, the firmware and fresh ready inputs).

    uv run python -m pytest tests/patchkit_smoke --slow            # all cells, goldens compared
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
    assert any(e["kind"] == "stall" for e in stock_session.events)
    stock_session.events.clear()


@pytest.mark.parametrize("stock_session", ["dt2", "dn2"], indirect=True)
def test_a_guest_fault_is_a_failure(stock_session):
    def crash(s):  # an illegal instruction at the loop head
        s.emu.poke32(s.emu.status()["cf"]["mainloop_pc"], 0xFFFFFFFF)

    _fails_with(stock_session, [{"_call": crash}, {"wait_ms": 200}], "fault")
    assert any(e["kind"] == "fault" for e in stock_session.events)
    stock_session.events.clear()


@pytest.mark.parametrize("stock_session", ["dt2"], indirect=True)
def test_a_dsp_halt_is_a_failure(stock_session):
    """DT2 1.17 stock: PLAY in pattern 3 halts the generated SHARC core (conditional MODIFY, finding 17)."""
    steps = [
        {"tap": ["PTN", "TRIG 3"]},
        {"tap": "PLAY"},
        {"wait_ms": 1500},
    ]
    if stock_session.run_segment("teeth", steps):
        pytest.skip("the known DSP halt is gone (conditional Type 7a): drop this test")
    bad = [c for c in stock_session.checks if not c.ok]
    assert "DSP halt" in bad[-1].detail, bad[-1].detail
    assert any(e["kind"] == "halt" for e in stock_session.events)
