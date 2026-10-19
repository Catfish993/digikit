"""The rig of the machines/pluck tests (test_pluck.py, test_pluck_tune.py, test_pluck_tune_low.py): the routine on the concrete SHARC+ core.

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

SKIP_NO_SELACHE = pytest.mark.skipif(
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
