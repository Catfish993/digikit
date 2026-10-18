"""machines/pluck on the concrete SHARC+ core, against the float32 model of tools/patchkit/pluck_ref.py.

The wrapper (core/dsp-voice) and the routine are assembled from the module sources and run block by block the way
the engine calls them (see tests/patchkit/test_dsp_voice.py). The routine's delay line (reserved, unloaded L2 on the
device) is carried from call to call as DM words. Skips when Selache is not built."""

import random
import struct

import numpy as np
import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

import support.dsp_voice_rig as dv  # noqa: E402

from tools.patchkit import (  # noqa: E402
    pk_sharc,  # noqa: E402
    pk_toolchains,  # noqa: E402
    pluck_ref,  # noqa: E402
)

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/patchkit/pk_toolchains.py selache)",
)

PLUCK = ROOT / "modules" / "machines" / "pluck" / "src" / "dsp" / "pluck.s"
PK_BASE = 0xB93000
PK_STATE, PK_RNG, PK_BUF = 0x2009B000, 0x2009B800, 0x200A0000
TYPE = 9
nw = dv.nw


def val(x: int) -> float:
    return struct.unpack("<f", struct.pack("<I", x))[0]


class PluckRig(dv.Rig):
    def __init__(self, seed: int = 0xD1617) -> None:
        self.ctx_rng = random.Random(seed)
        self.last_ctx: dict[str, int] = {}
        self.core = pk_sharc.assemble(dv.CORE.read_text())
        self.pluck = pk_sharc.assemble(PLUCK.read_text())
        ext_core = dv.core_externs()
        self.core_linked = pk_sharc.link(self.core, dv.CORE_BASE, ext_core)
        core_syms = self.core_linked.symbols
        ext = {
            "_dv_exp2": core_syms["_dv_exp2"],
            "_dv_frame_hw": core_syms["_dv_frame_hw"],
            "_pk_state": PK_STATE,
            "_pk_buf": PK_BUF,
            "_pk_rng": PK_RNG,
        }
        self.sine_linked = pk_sharc.link(self.pluck, PK_BASE, ext)
        self.render_pc = self.sine_linked.symbols["_pk_render"]
        self.entry = "_dv_entry"
        self.dm = {}
        self.reset()

    def reset(self) -> None:
        super().reset()
        self.dm[nw(dv.TABLE)] = TYPE
        for k in range(512):
            self.dm[nw(PK_STATE) + k] = 0
        self.dm[nw(PK_RNG)] = 0x2545F491
        self.line0 = (
            [0xFFFFFFFF] * 4096
        )  # the unloaded line, NaN: a read of what the routine did not write shows

    def param(self, track: int, slot: int, word: int) -> None:
        self.hw(0x6D + 0x30 * track + slot, word)

    def setup(
        self,
        note=60.0,
        tune=64.0,
        vel=100,
        decay=0x4000,
        pick=0,
        bright=0x4000,
        color=0,
        track=0,
    ) -> None:
        self.set_type(track, TYPE)
        self.set_note(track, note, tune, vel)
        self.param(track, 1, decay)
        self.param(track, 2, pick)
        self.param(track, 6, bright)
        self.param(track, 7, color)
        self.rec_word(2 * track, 0x1B8, 1)

    def block(self, track: int = 0) -> tuple[np.ndarray, dict]:
        v = 2 * track
        base = nw(PK_BUF) + track * 4096
        for k, w in enumerate(self.line0):
            self.dm[base + k] = w
        out = self.call(
            v,
            extra_reads={
                "state": (nw(PK_STATE), 512),
                "rng": (nw(PK_RNG), 1),
                "line": (base, 4096),
                "flags": (nw(dv.RECORDS + v * dv.REC + 0x1B8), 1),
            },
            max_steps=400000,
        )
        assert dv.stops_at(out, dv.RETURN_SW), out["halt"]
        for k, w in enumerate(out["state"]):
            self.dm[nw(PK_STATE) + k] = w
        self.dm[nw(PK_RNG)] = out["rng"][0]
        self.line0 = out["line"]
        self.dm[nw(dv.RECORDS + v * dv.REC + 0x1B8)] = out["flags"][0]
        return np.array([val(w) for w in out["out"]], dtype=np.float32), out


def run_blocks(rig: PluckRig, n: int, **kw) -> tuple[np.ndarray, list]:
    rig.setup(**kw)
    ys, outs = [], []
    for _ in range(n):
        y, out = rig.block(kw.get("track", 0))
        ys.append(y)
        outs.append(out)
    return np.concatenate(ys), outs


def model_blocks(
    n: int, note=60.0, tune=64.0, vel=100, decay=0x4000, pick=0, bright=0x4000, color=0
) -> np.ndarray:
    q = pluck_ref.Params(
        int(note * 256), int(tune * 256), decay, pick, bright, color, vel << 8
    )
    s = pluck_ref.Pluck()
    s.note_on(q)
    return np.concatenate([s.block(q) for _ in range(n)])


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {
            "note": 48,
            "decay": 0x7000,
            "pick": 0x2000,
            "bright": 0x2000,
            "color": 0x3000,
        },
        {"note": 84, "tune": 66.0, "vel": 127, "bright": 0x7F00, "color": 0x7F00},
        {"note": 33, "pick": 0x7F00, "decay": 0x1000, "bright": 0},
    ],
    ids=["default", "low", "high-tuned", "pick-max"],
)
def test_the_routine_matches_the_float32_model(kw):
    rig = PluckRig()
    n = 40
    got, _ = run_blocks(rig, n, **kw)
    want = model_blocks(n, **kw)
    assert np.all(np.isfinite(got))
    assert np.max(np.abs(want)) > 0.05
    err = float(np.max(np.abs(got - want)))
    # one rounding differs now and then (FLOAT of the xorshift word, TRUNC of the delay); the audio agrees to 1e-5
    assert err < 2e-5 * max(1.0, float(np.max(np.abs(want)))), err


def f0_from_crossings(y: np.ndarray, f_guess: float, fs: float = 48000.0) -> float:
    """Frequency of the fundamental: bandpass around f_guess (FFT), then a least-squares fit of the interpolated
    upward zero crossings against their index (the slope is the period)."""
    spec = np.fft.rfft(y * np.hanning(len(y)))
    f = np.fft.rfftfreq(len(y), 1 / fs)
    spec[(f < 0.7 * f_guess) | (f > 1.3 * f_guess)] = 0
    x = np.fft.irfft(spec, len(y))
    x = x[len(x) // 10 : -len(x) // 10]
    idx = np.flatnonzero((x[:-1] < 0) & (x[1:] >= 0))
    t = idx + (-x[idx]) / (x[idx + 1] - x[idx])
    k = np.arange(len(t))
    slope = np.polyfit(k, t, 1)[0]
    return fs / slope


@pytest.mark.parametrize("note", [24, 36, 48, 60, 72, 84, 96, 100])
def test_the_string_is_in_tune(note):
    """The fundamental of the rendered string, from the phase slope of the first partial, within the model's
    analytic error (< 0.2 cents up to note 100) plus the measurement's."""
    rig = PluckRig()
    want = 440.0 * 2 ** ((note - 69) / 12)
    period = 48000.0 / want
    n_blocks = int(np.ceil(max(3200, 40 * period) / 32)) + 4
    y, _ = run_blocks(
        rig, n_blocks, note=note, decay=0x7F00, bright=0x7F00, color=0x7000, pick=0
    )
    y = y[int(4 * period) :].astype(np.float64)
    got = f0_from_crossings(y, want)
    cents = 1200 * np.log2(got / want)
    assert abs(cents) < 1.5, (note, got, want, cents)


def test_instruction_counts_per_block():
    rig = PluckRig()
    rig.setup(note=48)
    _, first = rig.block()
    _, steady = rig.block()
    # note-on block includes the excitation (N + 1 samples of noise, mean, comb); the steady block is 32 samples
    assert steady["instructions"] < 1500, steady["instructions"]
    assert first["instructions"] < 60000, first["instructions"]
    rig.rec_word(0, 0x1B8, 0)
    idle = rig.call(0)
    # wrapper (about 270: it saves, pins and restores the context registers, finding 45, and writes four breadcrumbs, finding 48; the
    # breadcrumbs go into the reply records as well on a device, about 70 more) + 32 stores
    assert idle["instructions"] < 300


def test_the_voice_ends_when_it_is_quiet_and_clears_its_flags():
    rig = PluckRig()
    # fast decay, dark: the string dies within a few hundred blocks
    rig.setup(note=84, decay=0x0000, bright=0x0000)
    ended = None
    for i in range(1500):
        _, out = rig.block()
        if (out["flags"][0] & 0xFF) == 0:
            ended = i
            break
    assert ended is not None and ended > 5
    assert out["flags"][0] & 0xFF00 == 0, "the mark is cleared"
    # the next block is silent
    rig.rec_word(0, 0x1B8, 0)
    y, _ = rig.block()
    assert not np.any(y)


def test_a_new_note_restarts_the_string():
    rig = PluckRig()
    y1, _ = run_blocks(rig, 6, note=60)
    rig.rec_word(0, 0x1B8, 1)  # the arm routine: ACTIVE = 1, mark cleared
    y2, _ = rig.block()
    assert np.max(np.abs(y2)) > 0.05
    assert not np.allclose(y2, y1[:32])  # a new noise burst
