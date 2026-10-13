"""core/dsp-voice and test/sine-voice on the concrete SHARC+ core.

The wrapper and the sine routine are assembled from the module sources, linked at test bases with the firmware
symbols stood in by test addresses, and run the way the engine calls them: R4 = voice record, R8 = output block, R14
= loop counter, a frame built by the caller's CJUMP. The stock path must fall through with nothing but the displaced
stack adjustment and R0-R2 / I4 changed; a registered type must run the routine (even voice) or copy its block (odd
voice) and leave the callee-saved registers as found. The emulator proof is tools/pk_proof_voice.py. Skips when
Selache is not built."""

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pk_sharc  # noqa: E402
import pk_toolchains  # noqa: E402

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/pk_toolchains.py selache)",
)

CORE = ROOT / "modules" / "core" / "dsp-voice" / "src" / "dsp" / "dsp-voice.s"
SINE = ROOT / "modules" / "test" / "sine-voice" / "src" / "dsp" / "sine_voice.s"
CORE_BASE, SINE_BASE = 0xB90000, 0xB92000
STOCK_BP = 0xB9F000  # stands in for the stock renderer's second instruction
RETURN_SW = 0xB9F100  # the caller's return point
# byte addresses of the stand-ins (L2), one 4 KiB page each
TYPE_CACHE, FRAME, RECORDS, OUT, TABLE, STATE, STACK = (
    0x20091000,
    0x20092000,
    0x20093000,
    0x20098000,
    0x2009A000,
    0x2009B000,
    0x2009C000,
)
REC = 0x1D8
TYPE = 8


def nw(byte: int) -> int:
    return byte // 4


def bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def val(x: int) -> float:
    return struct.unpack("<f", struct.pack("<I", x))[0]


class Rig:
    def __init__(self) -> None:
        self.core = pk_sharc.assemble(CORE.read_text())
        self.sine = pk_sharc.assemble(SINE.read_text())
        self.sine_linked = None
        ext_core = {
            "_fw_type_cache": TYPE_CACHE,
            "_fw_render_next": STOCK_BP,
            "_fw_frame": FRAME,
            "_dv_table": TABLE,
        }
        # the sine links against the core's symbols; the core against the table address
        core_syms = pk_sharc.link(self.core, CORE_BASE, ext_core).symbols
        self.core_linked = pk_sharc.link(self.core, CORE_BASE, ext_core)
        ext_sine = {
            "_dv_exp2": core_syms["_dv_exp2"],
            "_dv_frame_hw": core_syms["_dv_frame_hw"],
            "_sv_state": STATE,
        }
        self.sine_linked = pk_sharc.link(self.sine, SINE_BASE, ext_sine)
        self.render_pc = self.sine_linked.symbols["_sv_render"]
        self.entry = "_dv_entry"
        self.dm: dict[int, int] = {}
        self.reset()

    def reset(self) -> None:
        self.dm = {}
        for k in range(34):
            self.dm[nw(TABLE) + k] = 0
        for k in range(32):
            self.dm[nw(STATE) + k] = 0
        for k in range(8):
            self.dm[nw(TYPE_CACHE) + k] = 0
        for k in range(256):
            self.dm[nw(FRAME) + k] = 0
        for k in range(32 * REC // 4):
            self.dm[nw(RECORDS) + k] = 0
        self.dm[nw(TABLE)] = TYPE
        self.dm[nw(TABLE) + 1] = self.render_pc

    def hw(self, k: int, v: int) -> None:
        word = nw(FRAME) + k // 2
        cur = self.dm.get(word, 0)
        sh = 16 * (k % 2)
        self.dm[word] = (cur & ~(0xFFFF << sh) & 0xFFFFFFFF) | ((v & 0xFFFF) << sh)

    def set_type(self, track: int, t: int) -> None:
        word = nw(TYPE_CACHE) + track // 2
        cur = self.dm.get(word, 0)
        sh = 16 * (track % 2)
        self.dm[word] = (cur & ~(0xFFFF << sh)) | (t << sh)

    def set_note(
        self, track: int, note: float, tune: float = 64.0, vel: int = 100
    ) -> None:
        self.hw(1 + track, int(note * 256))
        self.hw(0x6D + 0x30 * track, int(tune * 256))
        self.hw(0x1A + track, vel << 8)

    def rec_word(self, v: int, off: int, value: int) -> None:
        self.dm[nw(RECORDS + v * REC + off)] = value

    def call(
        self, v: int, regs: dict | None = None, extra_reads: dict | None = None
    ) -> dict:
        r4 = RECORDS + v * REC
        r8 = OUT + v * 0x80
        saved = {f"R{n}": 0x7000 + n for n in (3, 5, 6, 7, 9, 10, 11, 13, 15)}
        saved.update(
            {
                "I0": 0x20000010,
                "I1": 0x20000020,
                "I2": 0x20000030,
                "I3": 0x20000040,
                "I5": 0x20000050,
            }
        )
        saved.update(
            {"M1": 11, "M2": 12, "M3": 13, "I8": 0x20000080, "I9": 0x20000090, "M8": 5}
        )
        r = {
            "R4": r4,
            "R8": r8,
            "R12": 32,
            "R14": 32 - v,
            "M5": 0,
            "M6": 1,
            "M7": -1,
            "M13": 0,
            "M14": 1,
            "M15": -1,
            "I6": STACK,
            "I7": STACK,
            **saved,
            **(regs or {}),
        }
        self.dm[nw(STACK) - 1] = (
            RETURN_SW - 1
        )  # the return address the CJUMP pushed: [I6 - 1]
        self.dm[nw(STACK)] = 0x20000FF0  # the caller's frame pointer: [I6]
        reads = {"out": (nw(OUT + v * 0x80), 32), "table": (nw(TABLE), 4)}
        reads.update(extra_reads or {})
        out = pk_sharc.run_call(
            self.core_linked,
            self.entry,
            regs=r,
            dm_words=dict(self.dm),
            reads=reads,
            return_sw=RETURN_SW,
            others=(self.sine_linked,),
            read_regs=(
                "I6",
                "I7",
                "I4",
                "M5",
                "M6",
                "M7",
                "I8",
                "I9",
                "M8",
                "R3",
                "R5",
                "R15",
                "I0",
                "I5",
                "M1",
                "M3",
            ),
            max_steps=20000,
            extra_breaks=(STOCK_BP,),
        )
        out["in"] = r
        return out


@pytest.fixture(scope="module")
def rig_cls():
    return Rig


def stops_at(out: dict, sw: int) -> bool:
    return out["halt"]["reason"] == "breakpoint" and out["final_pc_sw"] == sw


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
    assert (out["regs"]["M5"], out["regs"]["M6"], out["regs"]["M7"]) == (
        0,
        1,
        0xFFFFFFFF,
    )


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
