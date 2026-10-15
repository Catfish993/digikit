"""Change bytes of a section without repacking it from scratch: keep the stock compressed stream, change only
the tokens an edit touches.

`tools.firmware.build.rebuild` stores every section with a literals-only packer, which grows the container to about three times
its stock size (4.29 MB against 1.48 MB for Digitakt II 1.16). Here the section's stream (tools/firmware/elz.py: aPLib-shaped,
literal or match tokens) is decoded into tokens, the edits are applied to the output, and the tokens are written out
again: a literal takes its new byte, a match whose source bytes no longer give the right output is replaced by a
match from elsewhere or by literals, every other token is kept. Every other section keeps its stock stored bytes, so
the container grows by a few kilobytes at most.

The result is checked by depacking it and comparing with the stock output plus exactly the requested edits. The
encoder writes the end marker the way `tools.firmware.aplib` does (gamma 2**24 + 2, byte 0xFF); the device's depacker accepts it
(the emulated bootstrap depacks these streams, tools/emulator/bootstrap_run.py).
"""

import struct

from tools.firmware import aplib, build, container
from tools.firmware.authcode import DEVICE_BY_ID, seal
from tools.firmware.elz import BIAS, FAR, REUSE, Bits, depack_section

NEAR_WINDOW = 0x20000


def tokens(stream):
    """Depack a section stream ([u32 length][u32 sum] + data).

    -> (output, tokens). A token is ("L", byte) or ("M", offset, count): `count` bytes copied from `offset` back.
    """
    length = struct.unpack_from(">I", stream, 0)[0]
    s = Bits(stream, 8, 8 + length)
    out = bytearray()
    toks = []
    last = 1
    while True:
        if s.bit():
            b = s.byte()
            out.append(b)
            toks.append(("L", b))
            continue
        g = s.gamma()
        if g == REUSE:
            off = last
        else:
            raw = ((g << 8) + s.byte()) & 0xFFFFFFFF
            if raw == BIAS:
                return bytes(out), toks
            off = (raw - BIAS) & 0xFFFFFFFF
            last = off
        short = 2 * s.bit() + s.bit()
        n = short if short else s.gamma() + 2
        if off > FAR:
            n += 1
        for _ in range(n + 1):
            out.append(out[-off])
        toks.append(("M", off, n + 1))


def _match_events(off, count, last):
    """Events of one match token -> (events, new `last`)."""
    ev = [(0, b"")]
    if off == last:
        g_bits = aplib._gamma_bits(REUSE)
        ev += [(b, b"") for b in g_bits]
    else:
        raw = off + BIAS
        g_bits = aplib._gamma_bits(raw >> 8)
        ev += [(b, b"") for b in g_bits[:-1]] + [(g_bits[-1], bytes((raw & 0xFF,)))]
        last = off
    field = count - 1 - (1 if off > FAR else 0)
    if 1 <= field <= 3:
        ev += [(field >> 1, b""), (field & 1, b"")]
    else:
        ev += [(0, b""), (0, b"")] + [(b, b"") for b in aplib._gamma_bits(field - 2)]
    return ev, last


def _encodable(off, count):
    field = count - 1 - (1 if off > FAR else 0)
    return field >= 1 and off >= 1


def encode(toks):
    """Tokens -> stream ([u32 length][u32 sum] + data) with the end marker."""
    events = []
    last = 1
    for t in toks:
        if t[0] == "L":
            events.append((1, bytes((t[1],))))
        else:
            ev, last = _match_events(t[1], t[2], last)
            events += ev
    events.append((0, b""))
    g_bits = aplib._gamma_bits(aplib._TERMINATOR_GAMMA_VALUE)
    events += [(b, b"") for b in g_bits[:-1]] + [
        (g_bits[-1], bytes((aplib._TERMINATOR_TRAILER_BYTE,)))
    ]
    body = aplib._assemble(events)
    return struct.pack(">II", len(body), sum(body) & 0xFFFFFFFF) + body


def reencode(stream, edits):
    """Packed section stream and {output offset: new byte} -> (new stream, stats)."""
    out, toks = tokens(stream)
    target = bytearray(out)
    for offset, value in edits.items():
        if offset >= len(out):
            raise ValueError(
                f"offset {offset:#x} is past the section's {len(out):#x} bytes"
            )
        target[offset] = value
    new = bytearray()
    result = []
    stats = {"tokens": len(toks), "kept": 0, "rematched": 0, "literalised": 0}
    for t in toks:
        d = len(new)
        if t[0] == "L":
            new.append(target[d])
            result.append(("L", target[d]))
            stats["kept"] += 1
            continue
        _, off, count = t
        if all(
            target[d + i]
            == (new[d + i - off] if d + i - off < d else target[d + i - off])
            for i in range(count)
        ):
            new += target[d : d + count]
            result.append(t)
            stats["kept"] += 1
            continue
        span = bytes(target[d : d + count])
        p = new.rfind(span, max(0, d - NEAR_WINDOW), d) if count >= 2 else -1
        if p >= 0 and _encodable(d - p, count):
            result.append(("M", d - p, count))
            stats["rematched"] += 1
        else:
            result += [("L", b) for b in span]
            stats["literalised"] += 1
        new += span
    packed = encode(result)
    if depack_section(packed) != bytes(target):
        raise ValueError("re-encoded stream does not depack to the requested output")
    return packed, stats


def _layout(c, entries):
    """The source container's layout with new stored bytes: header and table as they were (offsets and sizes
    updated), the first payload where it was (0x90 on 1.16, 0x80 earlier), sections in table order, 16-byte
    aligned, then alignment padding and a zeroed 32-byte trailer slot."""
    n = len(entries)
    out = bytearray(c[: container.TABLE_OFF + container.ENTRY_SZ * n])
    first = min(
        struct.unpack_from(">I", c, container.TABLE_OFF + container.ENTRY_SZ * k + 4)[0]
        for k in range(n)
    )
    out += bytes(first - len(out))
    for k, (sid, dest, stored) in enumerate(entries):
        struct.pack_into(
            ">IIII",
            out,
            container.TABLE_OFF + container.ENTRY_SZ * k,
            sid,
            len(out),
            len(stored),
            dest,
        )
        out += stored + bytes((-len(stored)) % 16)
    out += bytes(32)
    return bytes(out)


def patch_section(syx_path, section_id, edits):
    """Source .syx and {offset in the depacked section: new byte} -> (new .syx bytes, stats).

    Only the edited section's stored bytes change; every other section keeps its stock stored bytes. The container
    is relaid (sections follow each other, 16-byte aligned), sealed with the HMAC trailer, and framed with the
    source's own framing messages.
    """
    with open(syx_path, "rb") as fh:
        raw = fh.read()
    dec = container.decode_syx(syx_path)
    total_len = struct.unpack_from(">I", dec, 0)[0]
    c = bytes(dec[8 : 8 + total_len])
    n = struct.unpack_from(">I", c, container.COUNT_OFF)[0]
    entries = []
    stats = None
    for k in range(n):
        sid, off, clen, dest = struct.unpack_from(
            ">IIII", c, container.TABLE_OFF + container.ENTRY_SZ * k
        )
        stored = c[off : off + clen]
        if sid == section_id:
            stored, stats = reencode(stored, edits)
        entries.append((sid, dest, stored))
    if stats is None:
        raise ValueError(f"no section {section_id}")
    first, last = build._source_framing(raw)
    device = DEVICE_BY_ID[first[1:-1][3]]
    sealed = seal(_layout(c, entries), device)
    stream = struct.pack(">II", len(sealed), build.content_checksum(sealed)) + sealed
    stats["container_bytes"] = len(sealed)
    stats["stock_container_bytes"] = total_len
    return build.encode_syx(stream, first[1:-1][3], first, last), stats
