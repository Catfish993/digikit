"""dspi_log: reading and querying the DSPI frame log."""

import importlib.util
import struct
import sys

from support.paths import REPO_ROOT

TOOL = REPO_ROOT / "tools" / "emulator" / "dspi_log.py"
_spec = importlib.util.spec_from_file_location("dspi_log", TOOL)
assert _spec and _spec.loader
dl = importlib.util.module_from_spec(_spec)
sys.modules["tools.emulator.dspi_log"] = dl
_spec.loader.exec_module(dl)

FRAMES = [
    (0, 1, b"\x00\x03\x00\x00\x00\x00", b"\xaa\xaa"),
    (1, 1, b"\x00\x03\x00\x07\x00\x00", b"\xaa\xbb"),
    (2, 0, b"\x00\x04\x00\x07\x00\x09", b""),
]


def write_log(path):
    data = b"DSPILOG1"
    for index, attached, tx, reply in FRAMES:
        data += struct.pack(">QBI", index, attached, len(tx)) + tx
        data += struct.pack(">I", len(reply)) + reply
    path.write_bytes(data)
    return path


def test_read_log(tmp_path):
    frames = list(dl.read_log(write_log(tmp_path / "x.log")))
    assert [f.index for f in frames] == [0, 1, 2]
    assert [f.attached for f in frames] == [True, True, False]
    assert frames[1].tx == FRAMES[1][2]
    assert frames[2].reply == b""


def test_changed_offsets():
    assert dl.changed_offsets(b"abc", b"abc") == []
    assert dl.changed_offsets(b"abcdef", b"aXcYZf") == [(1, 1), (3, 4)]
    assert dl.changed_offsets(b"ab", b"abcd") == [(2, 3)]


def test_diff_output(tmp_path, capsys):
    log = write_log(tmp_path / "x.log")
    dl.main(["diff", str(log), "0", "1"])
    out = capsys.readouterr().out
    assert "  3: 00 07" in out
    assert "  1: aa bb" in out
