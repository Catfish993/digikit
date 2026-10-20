"""The audio rings of the SHARC engine around the master block, for the proof of core/fx (tools/patchkit/pk_proof_fx.py, docs/findings/55).

At a block boundary the DSP holds, per device:

  master    64 floats at `master` (32 left, then 32 right): the final mix, after the effect chain of core/fx.
  ring A    the DAC ring: 2 halves of 64 words at `ring_a` (+ 0x100 per half): 32 frames of (left, right), each `-round(sample * 2^31)`.
  ring C    the USB ring: 2 halves of 512 words at `ring_c` (+ 0x800 per half): 32 frames of 16 words; words 0 and 1 of a frame are the
            left and right of the master pair, `fix(f32(sample * 0x3fb6db6e) * 2^31)` (the factor is 1.428571); words 2 to 15 carry the other channels
            (words 2 and 3 are the next pair, the rest was 0 in the first measurement).
  flag      the word at `flag`; the block just rendered is in half `(flag & 1) ^ 1` once the block handler has toggled it.

Both devices have the same formats; only the addresses differ (finding 55: DT2 from the converter at 0x1c74a1 and the pack at 0x1c28b5, DN2 from
the block handler at 0x1c9e76: rings A `0x2c0478`, C `0x2c08e8`, flag `0x268a38`). The USB factor is the literal `0x3fb6db6e` in the pack loop."""

from __future__ import annotations

import struct
from dataclasses import dataclass

USB_SCALE_BITS = 0x3FB6DB6E


@dataclass(frozen=True)
class Layout:
    master: int
    flag: int
    ring_a: int
    ring_c: int


LAYOUT = {
    "dt2": Layout(master=0x25F180, flag=0x25F780, ring_a=0x261CC8, ring_c=0x262138),
    "dn2": Layout(master=0x268438, flag=0x268A38, ring_a=0x2C0478, ring_c=0x2C08E8),
}
RING_A_BYTES = 2 * 256
RING_C_BYTES = 2 * 2048


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


USB_SCALE = struct.unpack("<f", struct.pack("<I", USB_SCALE_BITS))[0]


@dataclass(frozen=True)
class Snap:
    """The bytes of master, flag, ring A and ring C at one block boundary."""

    frame: int
    flag: bytes
    master: bytes
    ring_a: bytes
    ring_c: bytes

    @property
    def half(self) -> int:
        return (struct.unpack("<I", self.flag)[0] & 1) ^ 1

    def master_floats(self) -> tuple[float, ...]:
        return struct.unpack("<64f", self.master)

    def a_words(self) -> tuple[int, ...]:
        """The 64 words of the half just rendered."""
        return struct.unpack_from("<64i", self.ring_a, 256 * self.half)

    def c_words(self) -> tuple[int, ...]:
        """The 512 words of the half just rendered."""
        return struct.unpack_from("<512i", self.ring_c, 2048 * self.half)


def snap(emu, device: str, frame: int) -> Snap:
    lay = LAYOUT[device]

    def peek(addr: int, n: int) -> bytes:
        return bytes.fromhex(emu.dsp_peek(addr, n)["hex"].replace("??", "00"))

    return Snap(
        frame,
        peek(lay.flag, 4),
        peek(lay.master, 256),
        peek(lay.ring_a, RING_A_BYTES),
        peek(lay.ring_c, RING_C_BYTES),
    )


def expected_a(master: tuple[float, ...]) -> list[float]:
    """Ring A left and right words for the master block (each is the negated sample; compare within 1)."""
    out = []
    for k in range(32):
        out += [-master[k] * 2**31, -master[32 + k] * 2**31]
    return out


def expected_c_pair(master: tuple[float, ...]) -> list[float]:
    """Ring C words 0 and 1 of each of the 32 frames for the master block (compare within 1)."""
    out = []
    for k in range(32):
        out += [
            f32(master[k] * USB_SCALE) * 2**31,
            f32(master[32 + k] * USB_SCALE) * 2**31,
        ]
    return out


def max_diff(want, got) -> float:
    return max(abs(a - b) for a, b in zip(want, got, strict=True))


def ring_a_error(s: Snap) -> float:
    """Largest distance of the ring A half from the DAC conversion of the master block (either sign for the left, as measured)."""
    want = expected_a(s.master_floats())
    got = s.a_words()
    return max(min(abs(w - g), abs(w + g)) for w, g in zip(want, got, strict=True))


def ring_c_pair_error(s: Snap) -> float:
    """Largest distance of the master pair in the ring C half from the USB conversion of the master block."""
    want = expected_c_pair(s.master_floats())
    got = [w for k in range(32) for w in s.c_words()[16 * k : 16 * k + 2]]
    return max_diff(want, got)


def c_others(s: Snap) -> tuple[int, ...]:
    """Words 2 to 15 of every frame of the ring C half (the channels the effect does not touch)."""
    w = s.c_words()
    return tuple(x for k in range(32) for x in w[16 * k + 2 : 16 * k + 16])
