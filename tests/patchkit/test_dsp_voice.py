"""core/dsp-voice and test/sine-voice on the concrete SHARC+ core.

The wrapper and the sine routine are assembled from the module sources, linked at test bases with the firmware
symbols stood in by test addresses, and run the way the engine calls them: R4 = voice record, R8 = output block, R14
= loop counter, a frame built by the caller's CJUMP. The stock path must fall through with nothing but the displaced
stack adjustment and R0-R2 / I4 changed; a registered type must run the routine (even voice) or copy its block (odd
voice) and leave the callee-saved registers as found. The emulator proof is tools/patchkit/pk_proof_voice.py. Skips when
Selache is not built."""

import struct

import numpy as np
import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

import random  # noqa: E402

from support.dsp_voice_rig import (  # noqa: E402
    CONTEXT_REGS,
    M6_HOSTILE,
    M13_HOSTILE,
    OUT,
    RECORDS,
    RETURN_SW,
    STACK,
    STATE,
    STOCK_BP,
    TYPE,
    Rig,
    draw_context,
    nw,
    stops_at,
)

from tools.patchkit import pk_sharc, pk_toolchains  # noqa: E402

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/patchkit/pk_toolchains.py selache)",
)


def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def context_intact(out) -> None:
    """Every context register leaves as it came in (M7 / M14 are the stock epilogue's and hold)."""
    for name in CONTEXT_REGS:
        assert u32(out["regs"][name]) == u32(out["in"][name]), (name, out["in"][name])


def bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def val(x: int) -> float:
    return struct.unpack("<f", struct.pack("<I", x))[0]


@pytest.fixture(scope="module")
def rig_cls():
    return Rig


def test_every_word_redecodes(rig_cls):
    rig = rig_cls()
    assert rig.core_linked.checked > 120
    assert rig.sine_linked.checked > 90


def test_an_unregistered_type_falls_through_to_the_stock_renderer(rig_cls):
    rig = rig_cls()
    rig.set_type(0, 0)
    out = rig.call(0)
    assert stops_at(out, STOCK_BP), out["halt"]
    # only the displaced instruction's effect (I7 - 104 bytes) and R0-R2 / I4 differ; the callee-saved set is intact
    assert out["regs"]["I7"] == STACK - 104
    assert out["regs"]["I6"] == STACK
    # the context registers are the stock code's own: L4 (zeroed for the scan) is put back, nothing else was touched
    context_intact(out)
    for name in ("R3", "R5", "R15", "I0", "I5", "M1", "M3", "I8", "I9", "M8"):
        assert out["regs"][name] == out["in"][name], name
    assert out["regs"]["R4"] == out["in"]["R4"] and out["regs"]["R8"] == out["in"]["R8"]
    assert out["instructions"] < 80


@pytest.mark.parametrize("track", [0, 1, 4, 7, 15])
def test_a_registered_type_selects_by_the_track_and_the_right_half_of_the_cache_word(
    rig_cls, track
):
    rig = rig_cls()
    # the neighbour in the same 32-bit cache word is another type: it must not be mistaken for ours
    rig.set_type(track ^ 1, TYPE + 1)
    rig.set_type(track, TYPE)
    rig.set_note(track, 60)
    v = 2 * track
    rig.rec_word(v, 0x1B8, 1)  # ACTIVE
    out = rig.call(v)
    assert stops_at(out, RETURN_SW), out["halt"]
    assert any(w not in (0, None) for w in out["out"])
    assert out["regs"]["I6"] == 0x20000FF0, "RFRAME restores the caller's frame pointer"
    assert out["regs"]["I7"] == STACK, "RFRAME restores I7"
    # the neighbour track's voice, with the other type, falls through
    rig2 = rig_cls()
    rig2.set_type(track, TYPE)
    rig2.set_type(track ^ 1, TYPE + 1)
    out2 = rig2.call(2 * (track ^ 1))
    assert stops_at(out2, STOCK_BP)


def test_the_callee_saved_registers_survive_a_rendered_voice(rig_cls):
    rig = rig_cls()
    rig.set_type(0, TYPE)
    rig.set_note(0, 60)
    rig.rec_word(0, 0x1B8, 1)
    out = rig.call(0)
    assert stops_at(out, RETURN_SW)
    for name in ("R3", "R5", "R15", "I0", "I5", "M1", "M3", "I8", "I9", "M8"):
        assert out["regs"][name] == out["in"][name], name
    context_intact(out)


def test_the_odd_voice_copies_the_even_voices_block(rig_cls):
    rig = rig_cls()
    rig.set_type(0, TYPE)
    rig.set_note(0, 60)
    rig.rec_word(0, 0x1B8, 1)
    rig.rec_word(1, 0x1B8, 1)
    even = rig.call(0)
    assert stops_at(even, RETURN_SW)
    # the block the even voice wrote is in the output page; the odd voice's call sees it in place
    for k, w in enumerate(even["out"]):
        rig.dm[nw(OUT) + k] = w
    odd = rig.call(1)
    assert stops_at(odd, RETURN_SW)
    assert odd["out"] == even["out"]


def voice_call(rig, v: int, ctx):
    """One registered voice with a note on; the odd voice sees the even voice's block in place."""
    t = v // 2
    rig.set_type(t, TYPE)
    rig.set_note(t, 60)
    rig.rec_word(v, 0x1B8, 1)
    if v % 2:
        for k in range(32):
            rig.dm[nw(OUT) + 32 * v + k] = 0
            rig.dm[nw(OUT) + 32 * (v - 1) + k] = 0x3F000000 + k
    return rig.call(v, ctx=ctx)


@pytest.mark.parametrize("v", [0, 1, 2, 3, 14, 15, 30, 31])
@pytest.mark.parametrize("m6", M6_HOSTILE)
@pytest.mark.parametrize("m13", M13_HOSTILE)
def test_the_machine_path_does_not_depend_on_m6_or_m13(rig_cls, v, m6, m13):
    """The hardware report's two causes: the odd-voice copy and the registry scan used M6, the machine call jumped by M13.
    Any M6 in {0x1b8, 4, 0, -1} and M13 in {4, -2, 0x10}: the same block as with the old friendly constants, and every
    context register intact afterwards."""
    ref = voice_call(rig_cls(), v, "friendly")
    assert stops_at(ref, RETURN_SW), ref["halt"]
    assert any(w not in (0, None) for w in ref["out"])
    out = voice_call(rig_cls(), v, {"M6": m6, "M13": m13})
    assert stops_at(out, RETURN_SW), out["halt"]
    assert out["out"] == ref["out"]
    context_intact(out)
    assert out["regs"]["I6"] == 0x20000FF0 and out["regs"]["I7"] == STACK


@pytest.mark.parametrize("seed", range(12))
def test_a_drawn_context_changes_nothing(rig_cls, seed):
    """Random contexts (M5, M6, M13, M15, L0-L5, MODE1 drawn from hostile values): the same block, the registers intact,
    the callee-saved set as found; the stock path too."""
    rng = random.Random(seed)
    for v in (2 * (seed % 16), 2 * (seed % 16) + 1):
        ctx = draw_context(rng)
        ref = voice_call(rig_cls(), v, "friendly")
        out = voice_call(rig_cls(), v, ctx)
        assert stops_at(out, RETURN_SW), (ctx, out["halt"])
        assert out["out"] == ref["out"], ctx
        context_intact(out)
        for name in ("R3", "R5", "R15", "I0", "I5", "M1", "M3", "I8", "I9", "M8"):
            assert out["regs"][name] == out["in"][name], name
        # the unregistered neighbour falls through with the context untouched
        stock = rig_cls()
        stock.set_type(v // 2, 0)
        so = stock.call(v, ctx=ctx)
        assert stops_at(so, STOCK_BP), (ctx, so["halt"])
        context_intact(so)


PROBE = """
.SECTION/PM seg_pmco;
.GLOBAL _probe;
_probe:
    I0 = 0x2009B000;
    R0 = M5;
    DM(0, I0) = R0;
    R0 = M6;
    DM(1, I0) = R0;
    R0 = M7;
    DM(2, I0) = R0;
    R0 = M13;
    DM(3, I0) = R0;
    R0 = M14;
    DM(4, I0) = R0;
    R0 = M15;
    DM(5, I0) = R0;
    R0 = L0;
    DM(6, I0) = R0;
    R0 = L1;
    DM(7, I0) = R0;
    R0 = L2;
    DM(8, I0) = R0;
    R0 = L3;
    DM(9, I0) = R0;
    R0 = L4;
    DM(10, I0) = R0;
    R0 = L5;
    DM(11, I0) = R0;
    R0 = MODE1;
    DM(12, I0) = R0;
    JUMP (M13, I13);
"""
PROBE_BASE = 0xB93800


class ProbeRig(Rig):
    """The wrapper around a routine that stores the context registers it is entered with."""

    def __init__(self) -> None:
        super().__init__()
        self.sine_linked = pk_sharc.link(pk_sharc.assemble(PROBE), PROBE_BASE, {})
        self.render_pc = self.sine_linked.symbols["_probe"]
        self.reset()


def test_the_context_is_pinned_for_the_routine():
    """The routine runs with M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, the L registers 0 and MODE1 with SIMD,
    TRUNCATE and ALUSAT clear and RND32 set, whatever the context was (the ABI in the wrapper's header)."""
    rng = random.Random(7)
    for _ in range(8):
        rig = ProbeRig()
        rig.set_type(0, TYPE)
        rig.set_note(0, 60)
        rig.rec_word(0, 0x1B8, 1)
        ctx = draw_context(rng)
        out = rig.call(0, ctx=ctx, extra_reads={"seen": (nw(STATE), 13)})
        assert stops_at(out, RETURN_SW), (ctx, out["halt"])
        seen = out["seen"]
        assert seen[:6] == [0, 1, 0xFFFFFFFF, 0, 1, 0xFFFFFFFF], (ctx, seen)
        assert seen[6:12] == [0] * 6, (ctx, seen)
        assert seen[12] & 0x20A000 == 0 and seen[12] & 0x10000, (ctx, hex(seen[12]))
        context_intact(out)


def sine_blocks(rig, note, tune=64.0, blocks=60):
    rig.set_type(0, TYPE)
    rig.set_note(0, note, tune)
    rig.rec_word(0, 0x1B8, 1)
    ys = []
    state = [nw(STATE) + k for k in range(32)]
    for _ in range(blocks):
        out = rig.call(
            0, extra_reads={"state": (nw(STATE), 32), "rec": (nw(RECORDS) + 110, 1)}
        )
        assert stops_at(out, RETURN_SW), out["halt"]
        ys += [val(w) for w in out["out"]]
        for a, w in zip(state, out["state"], strict=True):
            rig.dm[a] = w
        rig.dm[nw(RECORDS) + 110] = out["rec"][0]
    return np.array(ys, dtype=np.float64)


def peak_hz(x, fs=48000.0):
    """Frequency from the interpolated upward zero crossings (works for a few cycles)."""
    idx = np.flatnonzero((x[:-1] < 0) & (x[1:] >= 0))
    t = idx + (-x[idx]) / (x[idx + 1] - x[idx])
    assert len(t) >= 3, "fewer than two periods"
    return fs * (len(t) - 1) / (t[-1] - t[0])


@pytest.mark.parametrize("note", [12, 36, 60, 69, 84, 108, 127])
def test_the_sine_follows_the_note_within_a_cent(rig_cls, note):
    rig = rig_cls()
    want = 440.0 * 2 ** ((note - 69) / 12)
    y = sine_blocks(rig, note, blocks=max(100, int(4.2 * 48000 / want / 32)))
    got = peak_hz(y)
    cents = 1200 * np.log2(got / want)
    assert abs(cents) < 1.0, (note, got, want, cents)
    amp = float(np.max(np.abs(y)))
    assert 0.35 < amp < 0.40, amp  # velocity 100 / 127 * 0.5 = 0.3937


def test_tune_shifts_the_pitch_by_semitones(rig_cls):
    rig = rig_cls()
    y = sine_blocks(rig, 60, tune=65.0, blocks=100)
    want = 440.0 * 2 ** ((61 - 69) / 12)
    assert abs(1200 * np.log2(peak_hz(y) / want)) < 1.0


def test_a_retrigger_restarts_the_oscillator_and_a_stopped_voice_is_silent(rig_cls):
    rig = rig_cls()
    y = sine_blocks(rig, 60, blocks=4)
    assert abs(y[0]) < 1e-6, "the oscillator starts at phase 0"
    # byte +0x1b9 now holds the mark; the arm routine's short store of 1 at +0x1b8 clears it: a new note
    rig.rec_word(0, 0x1B8, 1)
    y2 = []
    out = rig.call(0, extra_reads={"state": (nw(STATE), 32)})
    y2 = [val(w) for w in out["out"]]
    assert abs(y2[0]) < 1e-6
    # ACTIVE = 0: zeros
    rig.rec_word(0, 0x1B8, 0)
    out = rig.call(0)
    assert out["out"] == [0] * 32
