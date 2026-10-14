"""RECIPS / RSQRTS: the documented values (SHARC+ PRM pp.19-16..19-18).

The seed mantissa ROM table is not published, so only the special cases are
exact; the ordinary case is checked for the documented accuracy (8 bits for
recips, 4 for rsqrts) of the opt-in approximation. A model with a different
seed table still gives the same quotient after the Newton steps of the
firmware's division at DN2 0x1c06ba (checked by hand: 31.4159/48000 and
1/(1+6.545e-4) agree to 1 ulp with any 8-bit seed).
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from support.paths import REPO_ROOT

TOOLS = os.path.join(
    str(REPO_ROOT), "tools"
)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from sharc_core.compute_alu import _alu_recip_seed_impl  # noqa: E402
from sharc_core.floats import Const  # noqa: E402


def f2b(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def b2f(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b))[0]


def seed(name: str, bits: int, approx: bool = True) -> int:
    _rn, value, _op, _updates = _alu_recip_seed_impl(0, 0, Const(bits), name, approx)
    assert isinstance(value, Const), value
    return value.value & 0xFFFFFFFF


class RecipsDocumented(unittest.TestCase):
    def test_recips_special_cases(self):
        self.assertEqual(seed("recips", 0x00000000, False), 0x7F800000)  # +0 -> +inf
        self.assertEqual(seed("recips", 0x80000000, False), 0xFF800000)  # -0 -> -inf
        self.assertEqual(seed("recips", 0x7FC00000, False), 0xFFFFFFFF)  # NaN
        self.assertEqual(seed("recips", f2b(2.0**126), False), 0)  # exp > 125
        self.assertEqual(seed("recips", f2b(-(2.0**126)), False), 0x80000000)

    def test_rsqrts_special_cases(self):
        self.assertEqual(seed("rsqrts", 0x00000000, False), 0x7F800000)
        self.assertEqual(seed("rsqrts", 0x7F800000, False), 0)  # +inf -> +0
        self.assertEqual(seed("rsqrts", f2b(-4.0), False), 0xFFFFFFFF)  # negative

    def test_recips_seed_is_8_bit_accurate_and_keeps_15_low_bits_zero(self):
        for x in (1.0, 1.5, 3.0, 48000.0, 1.00065, 1e-3, 123456.0, -7.25):
            s = seed("recips", f2b(x))
            self.assertEqual(s & 0x7FFF, 0)
            self.assertLess(abs(b2f(s) * x - 1.0), 2.0**-7, x)

    def test_rsqrts_seed_is_4_bit_accurate(self):
        for x in (1.0, 2.0, 48000.0, 0.01, 1e5):
            s = seed("rsqrts", f2b(x))
            self.assertEqual(s & 0x7FFFF, 0)
            self.assertLess(abs(b2f(s) * x**0.5 - 1.0), 2.0**-3, x)

    def test_ordinary_input_without_the_flag_has_no_numeric_seed(self):
        _rn, value, _op, _u = _alu_recip_seed_impl(
            0, 0, Const(f2b(3.0)), "recips", False
        )
        self.assertNotIsInstance(value, Const)


if __name__ == "__main__":
    unittest.main()
