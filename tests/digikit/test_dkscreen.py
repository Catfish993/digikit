"""The host side of the screenshot (tools/device/dkscreen.py): framing and decoding of the MidiRpc Screenshot reply, no emulator."""

import pytest

from tools.device import dkscreen as S
from tools.firmware.build import encode_8in7


def reply(payload: bytes, seq: int = 1) -> bytes:
    body = bytes((0, seq, 0, seq, S.SCREENSHOT | 0x80)) + payload
    return bytes.fromhex("f000203c1000") + encode_8in7(body) + b"\xf7"


def test_request_is_the_packed_device_10_frame():
    assert (
        S.request_frame(1)
        == bytes.fromhex("f000203c1000 00 0001 0000 04 f7".replace(" ", ""))[:6]
        + S.request_frame(1)[6:]
    )
    assert S.request_frame(1)[-1] == 0xF7 and S.request_frame(1)[:6] == bytes.fromhex(
        "f000203c1000"
    )


def test_decode_returns_the_1024_bytes_after_the_header():
    shadow = (
        bytes(range(256)) * 4
    )  # every value, bit 7 included (the packing must restore it)
    assert S.decode(reply(S.HEADER + shadow)) == shadow


@pytest.mark.parametrize(
    "payload", [b"", S.HEADER + bytes(1000), bytes(9) + bytes(1024)]
)
def test_decode_refuses_a_reply_of_another_shape(payload):
    with pytest.raises(ValueError):
        S.decode(reply(payload))


def test_png_is_scaled_and_has_the_frame_size():
    png = S.png_of(bytes(1024), 2)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (
        int.from_bytes(png[16:20], "big") == 256
        and int.from_bytes(png[20:24], "big") == 128
    )
