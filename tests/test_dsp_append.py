"""Appending blocks to the SHARC boot stream (tools/dsp_patch.py Stream.append_block) on a synthetic stream."""

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import dsp_patch
    import sharcldr
finally:
    sys.path.pop(0)

DATA, FIRST, FINAL = 0x0001, 0x4001, 0x8001


def stream() -> bytes:
    """A first block, two data blocks (one in L2) and a final block."""
    out = dsp_patch.block_header(0x28240000, 8, FIRST) + bytes(range(8))
    out += dsp_patch.block_header(0x2000_0000, 16) + bytes(range(16, 32))
    out += dsp_patch.block_header(0x8000_0000, 4) + b"\x01\x02\x03\x04"
    out += dsp_patch.block_header(0x0012_0230, 0, FINAL)
    return out


def test_header_is_valid_for_the_loader_parser():
    blob = stream()
    blocks = sharcldr.parse_blocks(blob)
    assert [b["target_address"] for b in blocks] == [
        0x28240000,
        0x2000_0000,
        0x8000_0000,
        0x120230,
    ]
    assert "FINAL" in blocks[-1]["flags"]
    assert blocks[0]["core"] == 0


def test_append_goes_before_final_and_leaves_the_rest_alone():
    s = dsp_patch.Stream(stream())
    before = bytes(s.blob)
    payload = bytes(range(100, 111))  # 11 bytes: padded to 12
    n = s.append_block(0x2001_0000, payload)
    assert n == len(before) + 16 + 12
    blocks = sharcldr.parse_blocks(bytes(s.blob))
    assert [b["target_address"] for b in blocks][-2:] == [0x2001_0000, 0x120230]
    assert "FINAL" in blocks[-1]["flags"]
    # everything before the final block is unchanged, the final block moved to the end
    assert bytes(s.blob[: len(before) - 16]) == before[:-16]
    assert bytes(s.blob[-16:]) == before[-16:]
    assert s.read(0x2001_0000, 12) == payload + b"\0"
    assert s.read(0x2000_0000, 16) == bytes(range(16, 32))


def test_append_refuses_loaded_memory_wrong_window_and_odd_addresses():
    s = dsp_patch.Stream(stream())
    with pytest.raises(SystemExit, match="already loads"):
        s.append_block(0x2000_000C, b"abcd")  # overlaps the L2 block's last word
    with pytest.raises(SystemExit, match="outside"):
        s.append_block(0x2820_0000, b"abcd")
    with pytest.raises(SystemExit, match="outside"):
        s.append_block(0x200F_FFFC, b"abcdefgh")  # runs past the L2 window
    with pytest.raises(SystemExit, match="multiple of 4"):
        s.append_block(0x2001_0002, b"abcd")
    with pytest.raises(SystemExit, match="empty"):
        s.append_block(0x2001_0000, b"")


def test_append_refuses_a_stream_over_the_boot_routine_limit():
    s = dsp_patch.Stream(stream())
    room = dsp_patch.MAX_STREAM - len(s.blob) - 16
    wide = ((0x2000_0000, 0x2400_0000),)
    s.append_block(0x2002_0000, bytes(room - room % 4), wide)
    with pytest.raises(SystemExit, match="more than"):
        s.append_block(0x2300_0000, b"abcd", wide)
    assert len(s.blob) <= dsp_patch.MAX_STREAM


def test_free_ranges_exclude_loaded_bytes():
    s = dsp_patch.Stream(stream())
    assert s.free_ranges() == [(0x2000_0010, 0x2010_0000)]
    s.append_block(0x2000_0100, b"abcdefgh")
    assert s.free_ranges() == [(0x2000_0010, 0x2000_0100), (0x2000_0108, 0x2010_0000)]


def test_patch_file_append_op_parses_and_is_not_an_image_edit():
    patch = dsp_patch.parse("APPEND 20010000 0a0b0c0d\nBYTES 20000000 00\n")
    assert [op.append for op in patch.ops] == [True, False]
    assert patch.ops[0].addr == 0x2001_0000
    assert struct.unpack("<I", patch.ops[0].new)[0] == 0x0D0C0B0A
