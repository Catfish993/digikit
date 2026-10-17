"""The host side of module upload (tools/device/dkupload.py): the framing of docs/design/digikit-upload.md, no emulator."""

import pytest

from tools.device import dkupload as D


def test_the_packing_examples_of_the_spec():
    # seven bytes with the first high: `80 01 02 03 04 05 06` packs to `40 00 01 02 03 04 05 06`; `FF` packs to `40 7F`
    msg = D.frame(D.CMD_DATA, 5, bytes.fromhex("80010203040506"))
    assert msg == bytes.fromhex(
        "f07d646b01 11 05 40 00010203040506 f7".replace(" ", "")
    )
    assert D.frame(D.CMD_INFO, 1, b"\xff")[7:-1] == bytes.fromhex("407f")


def test_a_reply_parses_and_foreign_messages_do_not():
    body = bytes([0xC5, 0x12, 0x80])  # status -59 (E_NOFILE), then two bytes
    from tools.firmware.build import encode_8in7

    reply = (
        bytes.fromhex("f07d646b01")
        + bytes([D.CMD_DELETE | 0x40, 9])
        + encode_8in7(body)
        + b"\xf7"
    )
    assert D.parse_reply(reply) == (D.CMD_DELETE, 9, -59, bytes([0x12, 0x80]))
    assert D.parse_reply(D.frame(D.CMD_INFO, 1)) is None  # a request is not a reply
    assert (
        D.parse_reply(bytes.fromhex("f000203c10000100f7")) is None
    )  # the stock MidiRpc


def test_split_messages_takes_complete_ones_and_keeps_the_rest():
    stream = bytearray(b"\x90\x01\x02\xf0\x01\xf7\xf0\x02\x03")
    assert D.split_messages(stream) == [b"\xf0\x01\xf7"]
    assert bytes(stream) == b"\xf0\x02\x03"


def test_a_put_is_begin_data_end_with_two_in_flight():
    sent: list[tuple[int, int]] = []

    class Fake:
        def __init__(self):
            self.queue: list[bytes] = []

        def send(self, msg: bytes) -> None:
            from tools.firmware.build import encode_8in7

            cmd, seq = msg[5], msg[6]
            sent.append((cmd, len(self.queue)))
            self.queue.append(
                msg[:4] + bytes([1, cmd | 0x40, seq]) + encode_8in7(b"\x00") + b"\xf7"
            )
            if cmd == D.CMD_END or cmd == D.CMD_BEGIN:
                self.queue = self.queue[-1:]

        def recv(self) -> bytes:
            return self.queue.pop(0)

    up = D.Uploader(Fake(), chunk=512)
    up.put("a.bin", bytes(512 * 5 + 7))
    cmds = [c for c, _ in sent]
    assert cmds == [D.CMD_BEGIN] + [D.CMD_DATA] * 6 + [D.CMD_END]
    # at most two DATA requests were outstanding when one was sent
    assert max(n for c, n in sent if c == D.CMD_DATA) <= 1
    with pytest.raises(ValueError):
        D.Uploader(Fake(), chunk=100)
