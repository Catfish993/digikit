"""The unsigned 64/64 divide of DT2 1.17 (short-word 0xb88e4e) runs to the end.

Its quotient assembly uses the register-form FEXT (0xb88e8b, 0xb88edb), which
the core once did not implement: a per-voice 64-bit counter passing 2^48
halted the DSP ("native-trap: sharc_core.compute._compute:359 raise
ValueError at 0xb88e8b"). The routine takes the dividend in R13:R12 and the
divisor in R15:R14 (high:low) and leaves the remainder in R15:R14. The two
quotient paths that use FEXT are checked against Python's divmod, stopping
before the delayed return, whose last quotient bits are ORed in by
instructions in the delay slots:

- divisor below 2^31 (R15 = 0): stops at 0xb88e8f, quotient high word
  R13 | (R2 << (R0 - 32)), low word R12, remainder in R14;
- divisor of 33 or more bits: stops at 0xb88ede, quotient R12 | (R2 << R0),
  remainder in R15:R14.

Skipped when the firmware image is absent.
"""

import os
import random

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
BLOB = ROOT / "out/sections/dt2-1.17/section_7_BLOB.bin"
pytestmark = pytest.mark.skipif(not BLOB.exists(), reason="DT2 1.16 image absent")

from tools.sharc_dev import sharc_run as sr  # noqa: E402
from tools.sharc_dev.sharc_core.encoding import UREG_CODES  # noqa: E402

MASK = 0xFFFFFFFF
UDIV64 = 0xB88E4E
SMALL_DIVISOR_STOP = 0xB88E8F
WIDE_DIVISOR_STOP = 0xB88EDE


@pytest.fixture(scope="module")
def memory():
    cwd = os.getcwd()
    os.chdir(ROOT)  # the loader resolves out/ from the working directory
    try:
        return sr._load_image_memory("dt2-1.17")
    finally:
        os.chdir(cwd)


def divide(memory, dividend, divisor):
    runner = sr.Runner(
        memory,
        UDIV64,
        regs={
            "R12": dividend & MASK,
            "R13": dividend >> 32,
            "R14": divisor & MASK,
            "R15": divisor >> 32,
        },
        breakpoints=[SMALL_DIVISOR_STOP, WIDE_DIVISOR_STOP],
    )
    result = runner.run(5000)
    assert result.halt.reason == "breakpoint", result.halt.text

    def reg(name):
        return runner.state.uregs[UREG_CODES[name]].value

    return result.halt.pc_sw, {
        n: reg(n) for n in ("R0", "R2", "R12", "R13", "R14", "R15")
    }


def test_small_divisor_quotient_and_remainder(memory):
    # 64-bit dividend above 2^48, divisor below 2^31: R13 != 0 takes the
    # FEXT path at 0xb88e8b.
    random.seed(116)
    for _ in range(40):
        dividend = random.getrandbits(random.choice([49, 56, 64])) | (1 << 48)
        divisor = random.randrange(1, 1 << 31)
        pc, g = divide(memory, dividend, divisor)
        assert pc == SMALL_DIVISOR_STOP
        shift = (g["R0"] - 32) & MASK
        high = g["R13"] | (((g["R2"] << shift) & MASK) if shift < 32 else 0)
        quotient, remainder = divmod(dividend, divisor)
        assert (high << 32) | g["R12"] == quotient, (hex(dividend), hex(divisor))
        assert g["R14"] == remainder


def test_wide_divisor_quotient_and_remainder(memory):
    random.seed(1160)
    seen = 0
    for _ in range(80):
        dividend = random.getrandbits(random.choice([56, 64])) | (1 << 50)
        divisor = random.getrandbits(random.choice([33, 40, 52, 62])) | (1 << 32)
        quotient, remainder = divmod(dividend, divisor)
        if quotient == 0:
            continue  # leaves through a different exit, not the FEXT path
        pc, g = divide(memory, dividend, divisor)
        if pc != WIDE_DIVISOR_STOP:
            continue
        seen += 1
        low = g["R12"] | (((g["R2"] << g["R0"]) & MASK) if g["R0"] < 32 else 0)
        assert low == quotient, (hex(dividend), hex(divisor))
        assert (g["R15"] << 32) | g["R14"] == remainder
    assert seen > 10
