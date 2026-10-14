"""fx/master-drive: the DSP routine on the concrete SHARC+ core, against a float32 reference, and the patch itself.

The routine is assembled from the module source, linked at a test base with the buffer in normal-word L2 and the
drive word at the byte address of a poked normal word, and run block by block with the module's own data words
carried from one call to the next. Skips when Selache is not built. The emulator proof (both devices) is
tools/pk_proof_drive.py."""

import struct
import sys

import numpy as np
import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "tools"))

import pk_sharc  # noqa: E402
import pk_toolchains  # noqa: E402

pytestmark = pytest.mark.skipif(
    pk_toolchains.selache_dir() is None,
    reason="selache not built (tools/pk_toolchains.py selache)",
)

SRC = ROOT / "modules" / "fx" / "master-drive" / "src" / "dsp" / "master_drive.s"
BASE = 0xB90000
HOOK = 0xB9F000
BUF = 0x08020000  # normal-word DM address of the master buffer
WORD_NW = 0x08028000  # the drive word's normal word; the load is a short word at its byte address
DATA_NW = 0x08024000
NEUTRAL = 0x5000
MAXW = 0x7F00


def bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def val(x: int) -> float:
    return struct.unpack("<f", struct.pack("<I", x))[0]


class Rig:
    def __init__(self) -> None:
        import sharc_core.memory as mem

        self.obj = pk_sharc.assemble(SRC.read_text())
        self.addr = {n: DATA_NW + i for i, n in enumerate(self.obj.data_symbols)}
        ext = {
            "_fw_q31_convert": HOOK,
            "_fw_master_buf": BUF,
            "_fw_drive_word": mem.normal_word_to_byte(WORD_NW),
            **self.addr,
        }
        self.linked = pk_sharc.link(self.obj, BASE, ext)
        self.state = {}
        for n, (sec, off, _g) in self.obj.data_symbols.items():
            blob = self.obj.data[sec][0]
            self.state[n] = int.from_bytes(blob[off : off + 4], "little")

    def block(self, x: np.ndarray, word: int, regs: dict | None = None) -> dict:
        dm = {BUF + i: bits(float(v)) for i, v in enumerate(x)}
        dm.update({self.addr[n]: v for n, v in self.state.items()})
        dm[WORD_NW] = word & 0xFFFF
        names = list(self.addr)
        out = pk_sharc.run_call(
            self.linked,
            "_md_hook",
            regs={"R4": 1, "R8": 2, **(regs or {})},
            dm_words=dm,
            reads={"out": (BUF, 64), "data": (DATA_NW, len(names))},
            return_sw=HOOK,
            max_steps=5000,
        )
        assert out["halt"]["reason"] == "breakpoint", out["halt"]
        self.state = dict(zip(names, out["data"], strict=True))
        out["y"] = np.array([val(w) for w in out["out"]], dtype=np.float32)
        out["d"] = val(self.state["_md_d"])
        return out


def reference(x: np.ndarray, d: float, g0: float, a0: float, b0: float):
    """The routine's arithmetic in float32 for one block with the drive already slewed to d."""
    f = np.float32
    d = f(d)
    g1 = f(1) + d * f(7)
    a1 = f(1) - d
    m = f(1) + d * f(-0.3)
    b1 = d * m
    dg, da, db = (
        (g1 - f(g0)) * f(1 / 32),
        (a1 - f(a0)) * f(1 / 32),
        (b1 - f(b0)) * f(1 / 32),
    )
    y = np.empty(64, dtype=np.float32)
    for ch in range(2):  # left 32 samples, then right 32: the same ramp twice
        g, a, b = f(g0), f(a0), f(b0)
        for i in range(32):
            xi = x[32 * ch + i].astype(np.float32)
            t = np.clip(xi * g, f(-1), f(1))
            s = t + t * t * t * f(-1 / 3)
            y[32 * ch + i] = s * b + xi * a
            g, a, b = g + dg, a + da, b + db
    return y, (g1, a1, b1)


def signal(n: int = 64) -> np.ndarray:
    k = np.arange(n)
    return (0.8 * np.sin(k * 0.37) + 0.15 * np.sin(k * 1.9)).astype(np.float32)


@pytest.fixture(scope="module")
def rig_cls():
    return Rig


def test_every_word_redecodes_and_the_short_word_load_is_the_swse_form(rig_cls):
    rig = rig_cls()
    assert rig.linked.checked >= 80
    import sharc_asm
    import sharc_isa

    isa = sharc_isa.load_instruction_set()
    d = rig.linked.code_boot
    r = isa.decode_bytes(d[:6])
    parcels = [
        int.from_bytes(d[i : i + 2], "little")
        for i in range(0, r.instruction.extent_bytes, 2)
    ]
    assert sharc_asm.decode_text(0, parcels).endswith("(swse)")


@pytest.mark.parametrize("word", [NEUTRAL, 0, 0x4000, 0xFFFF, 0x7000 - 0x2000])
def test_neutral_and_below_is_bit_identical_and_cheap(rig_cls, word):
    rig = rig_cls()
    x = signal()
    regs = {f"R{n}": 0x1000 + n for n in (5, 6, 7, 9, 10, 11, 12, 13)}
    for _ in range(3):
        out = rig.block(x, word, regs)
        assert np.array_equal(out["y"].view(np.uint32), x.view(np.uint32))
        assert out["d"] == 0.0
        assert out["instructions"] < 80
    assert out["regs"]["R4"] == 1 and out["regs"]["R8"] == 2


def test_full_drive_matches_the_float32_reference_block_by_block(rig_cls):
    rig = rig_cls()
    x = signal()
    d = np.float32(0)
    g, a, b = np.float32(1), np.float32(1), np.float32(0)
    regs = {f"R{n}": 0x2000 + n for n in (5, 6, 7, 9, 10, 11, 12, 13)}
    worst = 0.0
    for _blk in range(140):
        d = min(np.float32(1), d + np.float32(1 / 128))
        want, (g, a, b) = reference(x, d, g, a, b)
        out = rig.block(x, MAXW, regs)
        worst = max(worst, float(np.max(np.abs(out["y"] - want))))
        assert out["d"] == pytest.approx(float(d), abs=1e-6)
        for n in (5, 6, 7, 9, 10, 11, 12, 13):  # saved and restored
            assert out["regs"][f"R{n}"] == 0x2000 + n
    assert worst < 2e-6, worst
    assert out["d"] == pytest.approx(1.0, abs=1e-6)
    assert 700 < out["instructions"] < 1300
    # saturated: loud input comes out below the dry level's peak, small input is boosted by the makeup rule
    assert np.max(np.abs(out["y"])) < np.max(np.abs(x))


def test_release_reaches_exact_bypass_without_a_step(rig_cls):
    rig = rig_cls()
    x = signal()
    for _ in range(140):
        rig.block(x, MAXW)
    steps = 0
    last_wet = None
    while True:
        out = rig.block(x, NEUTRAL)
        steps += 1
        if out["d"] == 0.0:
            break
        last_wet = out
        assert steps < 200
    assert 120 <= steps <= 130  # 1/128 per block
    # the block that ends at d = 0 ramps to the exact bypass coefficients: its last sample is one ramp step (1/32 of 1/128 of the effect) from x
    assert abs(float(out["y"][-1] - x[-1])) < 1e-4  # one 1/32 step of the last ramp
    nxt = rig.block(x, NEUTRAL)
    assert np.array_equal(nxt["y"].view(np.uint32), x.view(np.uint32))
    assert last_wet is not None


def test_a_knob_step_moves_the_output_by_a_small_amount_per_block(rig_cls):
    """Click check: one jump of the word from neutral to full changes the first block by little."""
    rig = rig_cls()
    x = signal()
    base = rig.block(x, NEUTRAL)["y"]
    first = rig.block(x, MAXW)["y"]
    assert float(np.max(np.abs(first - base))) < 0.06
    assert (
        float(np.max(np.abs(np.diff(first))))
        < 2 * float(np.max(np.abs(np.diff(base)))) + 0.02
    )
