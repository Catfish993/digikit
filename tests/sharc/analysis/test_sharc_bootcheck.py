"""tools/sharc_dev/sharc_bootcheck.py: the boot ROM rules (ADSP-2156x HWR chapter 40) on synthetic and stock streams."""

import struct

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
from tools.patchkit import dsp_patch  # noqa: E402
from tools.sharc_dev import sharc_bootcheck as bc  # noqa: E402

FIRST, FINAL, IGNORE, FILL, INIT = 0x4000, 0x8000, 0x1000, 0x0100, 0x0800
HDR = 16


def hdr(target, count, flags=0, argument=0, sign=0xAD):
    raw = bytearray(
        struct.pack("<IIII", sign << 24 | flags | 1, target, count, argument)
    )
    raw[2] = 0
    for b in bytes(raw):
        raw[2] ^= b
    return bytes(raw)


def stock_like() -> bytes:
    """Two applications like the real streams: initcode app, then the main one ending in FINAL."""
    app0 = hdr(0x120230, 0, FIRST | IGNORE, 0x20 + HDR)  # points at the next FIRST
    app0 += hdr(0x28240000, 16) + bytes(16)
    app0 += hdr(0x120230, 0, INIT)
    # app0 arg is bytes from the end of its header to the next FIRST header
    app0 = hdr(0x120230, 0, FIRST | IGNORE, len(app0) - HDR) + app0[HDR:]
    body = hdr(0x2000_0000, 16) + bytes(range(16))
    body += hdr(0x8000_0000, 0x40, FILL, 0xDEADBEEF)
    first = hdr(0x1C1338, 0, FIRST | IGNORE, len(body) + HDR)  # + the FINAL header
    final = hdr(0x1C1338, 0, FINAL)
    return app0 + first + body + final


def rules(blob):
    return bc.check(blob).rules()


def test_synthetic_stock_passes():
    rep = bc.check(stock_like())
    assert rep.ok, [str(f) for f in rep.findings]


def test_bad_signature_checksum_and_alignment():
    blob = stock_like()
    assert "H1" in rules(blob[:15] + b"\xac" + blob[16:]) or "H1" in rules(
        bytes([blob[0], blob[1], blob[2], 0xAC]) + blob[4:]
    )
    bad = bytearray(blob)
    bad[2] ^= 0x10
    assert "H2" in rules(bytes(bad))
    odd = hdr(0x2000_0002, 16) + bytes(16)
    s = stock_like()
    at = len(s) - HDR
    assert "H3" in rules(s[:at] + odd + s[at:])
    odd = hdr(0x2000_0000, 6) + bytes(8)
    assert "H3" in rules(s[:at] + odd + s[at:])


def test_first_block_must_be_first_and_not_fill():
    blob = stock_like()
    assert "H4" in rules(hdr(0x2000_0000, 4) + bytes(4) + blob)
    fill_first = hdr(0x2000_0000, 8, FIRST | FILL, 0) + blob[HDR:]
    assert "H5" in rules(fill_first)


def test_final_missing_trailing_or_cut():
    blob = stock_like()
    assert "H8" in rules(blob[:-HDR])  # no FINAL
    assert "H8" in rules(blob + bytes(HDR))  # bytes after FINAL
    assert "H8" in rules(blob[:-5])  # cut header
    cut = blob[: len(blob) - HDR - 8]  # payload overrun is a cut stream too
    assert "H8" in rules(cut)


def test_rom_workspace_is_not_bootable():
    s = stock_like()
    at = len(s) - HDR
    for target in (0x200F_E000, 0x200F_FFFC, 0x200F_DFFC):
        blob = s[:at] + hdr(target, 8) + bytes(8) + s[at:]
        blob = fix_next(blob, 8 + HDR)
        assert ("H9" in rules(blob)) == (
            target >= 0x200F_E000 or target == 0x200F_DFFC
        ), hex(target)
    ok = fix_next(s[:at] + hdr(0x200F_DFF8, 8) + bytes(8) + s[at:], 8 + HDR)
    assert bc.check(ok).ok


def fix_next(blob, grown):
    """Grow the last FIRST block's next pointer like Stream.append_block does."""
    rep = bc.check(blob)
    b = [x for x in rep.blocks if "FIRST" in x.flags][-1]
    head = bytearray(blob[b.offset : b.offset + HDR])
    struct.pack_into("<I", head, 12, b.argument + grown)
    head[2] = 0
    for x in bytes(head):
        head[2] ^= x
    return blob[: b.offset] + bytes(head) + blob[b.offset + HDR :]


def test_next_application_pointer():
    s = stock_like()
    at = len(s) - HDR
    grown = s[:at] + hdr(0x2001_0000, 8) + bytes(8) + s[at:]
    assert "H12" in rules(grown)  # the pointer no longer reaches the end
    assert bc.check(fix_next(grown, 8 + HDR)).ok


def test_indirect_and_callback_are_refused():
    s = stock_like()
    at = len(s) - HDR
    for flag in (0x2000, 0x0400):
        blob = fix_next(s[:at] + hdr(0x2001_0000, 4, flag) + bytes(4) + s[at:], 4 + HDR)
        assert "H10" in rules(blob)


def test_ignore_block_does_not_load_and_clears_final():
    s = stock_like()
    at = len(s) - HDR
    # IGNORE payload in the workspace is not a load; IGNORE|FINAL does not end the stream
    blob = fix_next(s[:at] + hdr(0x200F_E000, 4, IGNORE) + bytes(4) + s[at:], 4 + HDR)
    assert bc.check(blob).ok
    only = s[:at] + hdr(0x1C1338, 0, FINAL | IGNORE)
    assert "H8" in rules(only)


def test_append_block_keeps_the_stream_valid_and_stock_plus_inserts():
    stock = stock_like()
    st = dsp_patch.Stream(stock)
    st.append_block(0x2001_0000, bytes(range(100)))
    st.append_block(0x2002_0000, bytes(7))
    patched = bytes(st.blob)
    assert bc.check(patched).ok, [str(f) for f in bc.check(patched).findings]
    errors = [f for f in bc.check_against_stock(stock, patched) if f.level == "error"]
    assert not errors, [str(f) for f in errors]


def test_append_refuses_the_rom_workspace():
    st = dsp_patch.Stream(stock_like())
    with pytest.raises(SystemExit, match="outside the allowed windows"):
        st.append_block(0x200F_E000, bytes(4))
    with pytest.raises(SystemExit, match="outside the allowed windows"):
        st.append_block(0x200F_DFFC, bytes(8))
    st.append_block(0x200F_DFFC, bytes(4))


def test_against_stock_allows_payload_edits_and_detects_header_changes():
    stock = stock_like()
    st = dsp_patch.Stream(stock)
    st.append_block(0x2001_0000, bytes(8))
    patched = bytearray(st.blob)
    patched[HDR + HDR + 3] ^= 1  # inside stock block 1's payload
    found = bc.check_against_stock(stock, bytes(patched))
    assert not [f for f in found if f.level == "error"]
    assert any(f.rule == "P6" for f in found)
    head = bytearray(st.blob[HDR : 2 * HDR])  # stock block 1's header: move its target
    head[4] ^= 0x10
    head[2] = 0
    for x in bytes(head):
        head[2] ^= x
    patched = bytes(st.blob[:HDR]) + bytes(head) + bytes(st.blob[2 * HDR :])
    errors = [
        f.rule for f in bc.check_against_stock(stock, patched) if f.level == "error"
    ]
    assert "P2" in errors


@pytest.mark.parametrize("image", ["dt2-1.16", "dt2-1.17", "dn2-1.11", "dn2-1.12"])
def test_stock_streams_pass(image):
    path = ROOT / "out" / "sections" / image / "section_7_BLOB.bin"
    if not path.exists():
        pytest.skip("no extracted sections")
    rep = bc.check(path.read_bytes())
    assert rep.ok, [str(f) for f in rep.errors]


def test_header_checksum_is_the_whole_header_xor():
    """The 'other 24 bits' reading (xor of block-code bytes 0, 1, 3) does not hold on stock headers."""
    path = ROOT / "out" / "sections" / "dt2-1.16" / "section_7_BLOB.bin"
    if not path.exists():
        pytest.skip("no extracted sections")
    blob = path.read_bytes()
    rep = bc.check(blob)
    assert rep.ok
    three = sum(
        1
        for b in rep.blocks
        if blob[b.offset + 2]
        == blob[b.offset] ^ blob[b.offset + 1] ^ blob[b.offset + 3]
    )
    assert three < len(rep.blocks) // 4
