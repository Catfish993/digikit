"""tools/patchkit/fx_rings.py: the ring formats of the proof, on made-up bytes (no emulator)."""

import struct

from tools.patchkit import fx_rings as R


def make(master, flag=1, a_noise=0, c_noise=0):
    ra = bytearray(512)
    rc = bytearray(4096)
    half = (flag & 1) ^ 1
    a = R.expected_a(tuple(master))
    c = R.expected_c_pair(tuple(master))
    for i, v in enumerate(a):
        struct.pack_into("<i", ra, 256 * half + 4 * i, round(v) + a_noise)
    for k in range(32):
        for j in (0, 1):
            struct.pack_into(
                "<i", rc, 2048 * half + 64 * k + 4 * j, round(c[2 * k + j]) + c_noise
            )
        struct.pack_into("<i", rc, 2048 * half + 64 * k + 8, 12345 + k)
    return R.Snap(
        7, struct.pack("<I", flag), struct.pack("<64f", *master), bytes(ra), bytes(rc)
    )


MASTER = [R.f32(0.01 * (k % 7) - 0.03) for k in range(64)]


def test_the_half_just_rendered_is_the_other_one_than_the_flag():
    assert make(MASTER, flag=1).half == 0 and make(MASTER, flag=0).half == 1


def test_a_snapshot_that_follows_the_formats_has_no_error():
    s = make(MASTER)
    assert R.ring_a_error(s) <= 1 and R.ring_c_pair_error(s) <= 1
    assert R.c_others(s)[0] == 12345 and len(R.c_others(s)) == 32 * 14


def test_an_error_of_two_is_seen():
    assert R.ring_c_pair_error(make(MASTER, c_noise=2)) >= 2
    assert R.ring_a_error(make(MASTER, a_noise=3)) >= 3


def test_the_usb_scale_is_one_over_seven_tenths():
    assert abs(R.USB_SCALE - 1 / 0.7) < 1e-6
