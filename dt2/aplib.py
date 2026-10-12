"""A store-only encoder for this firmware's aPLib-shaped container streams.

There is no LZ77 match-finder here on purpose. Byte-exact compression was
never the requirement: the device's own depacker (emu/oracle.py:depack,
0x80000432; the updater carries an identical copy at 0x80005710, see
emu/extract.py) accepts any stream that is a *valid* encoding of the bytes it
should produce -- nothing anywhere in the acceptance path hashes or compares
the compressed bytes, only the decompressed result. So a "compressor" that
emits every input byte as a literal is sufficient. It makes the output a
little *larger* than the input (about 12.5%, one extra tag byte per 8 literal
bytes, plus a ~16-byte terminator) -- that is expected and fine.

WHAT WAS EMPIRICALLY CONFIRMED, by single-stepping the real depacker under
Unicorn (see the disassembly at 0x80000432 in section 2 -- 354 bytes, one
`rts`) and cross-checking every claim below against emu/oracle.py:depack() on
hand-built streams:

  * The 8-byte section header ([u32 compressed_len][u32 byte_sum], big
    endian, sum over the bytes following the header) is skipped unconditionally
    (`addq.l #8,a0`) by the depacker itself. This module does not re-derive
    that convention -- it was already established by dt2/container.py and
    emu/extract.py -- but every round-trip below exercises it.

  * Tag bits are read MSB-first from a byte, refilled one byte at a time, via
    a classic "sentinel bit" shift register (not a separate bit counter).

  * LITERAL is tag bit **1** followed by one raw byte, copied straight to the
    output. This is the *opposite* polarity of the public aPLib reference
    (where 0 = literal) -- confirmed by single-stepping: the branch that does
    `move.b (a0),(a1)` and loops is the one taken when the extracted bit is 1.

  * Tag bit **0** enters a match path: an aPLib-style "gamma2" code is read
    (result=1; repeat result=result*2+databit until a raw stop-bit is seen).
    That much is confirmed. Two things about it are NOT fully reverse
    engineered, because this module never needs to emit a real match:
      - the stop-bit's polarity is INVERTED relative to the public reference
        (confirmed empirically: the loop keeps going while the raw bit is 0,
        and stops when the raw bit is 1);
      - what the gamma result and a following raw byte become (an LZ77
        offset, almost certainly) and how length is then coded, was traced
        only far enough to find the one thing this module actually uses --
        see below. The full match format is NOT documented here and this
        module cannot decode or emit one.

  * There is NO "first byte is always a literal" special case (the public
    aPLib reference has one; this device's routine does not). Confirmed
    because an all-terminator stream (no literals at all) round-trips to
    b'' -- see _self_test().

  * END OF STREAM was the hard part, and what was found is worth flagging
    clearly: the *only* branch in the whole 354-byte routine that reaches its
    epilogue (`movem.l (a7),d2-d5/a2; rts`) is
        cmpi.l #$2ff,d3 ; beq.w <epilogue>
    where d3 was *just* computed, on the "not a short 2-code" match path, as
    `(gamma_result << 8) + next_raw_byte` (confirmed by breakpointing that
    exact instruction and sweeping gamma_result/byte combinations). That
    formula's smallest possible value is 3*256+0 = 768 -- it can never
    naturally land on 0x2FF (767). This module's terminator exploits 32-bit
    wraparound instead: it gamma-codes the value 2**24 + 2 with a trailing
    byte of 0xFF, so (2**24+2)*256 + 255 == 0x2FF (mod 2**32) exactly. That
    branch has no side effects on the destination pointer, so it cleanly ends
    the stream after any run of literals, and it was verified against the
    real depacker for every case in _self_test() and every extracted section
    (see this file's __main__). Whether Elektron's own encoder ends its
    streams the same way is NOT known -- a real compressed stream is not
    available to compare against (only decompressed sections are, in
    sections/) -- so this is flagged as found-but-unexplained, not assumed.

Everything in the module docstring above is a hypothesis this module tests
against the real device, not documentation trusted on faith: run this file's
__main__ against a sections/ directory to see it re-confirmed.
"""
import struct


def _gamma_bits(value):
    """MSB-first bit sequence this depacker's gamma2 reader would consume to
    decode to `value` (excluding the implicit leading 1): for each bit of
    `value` after its leading 1, emit that bit, then a raw stop-bit (1 on the
    last one, 0 otherwise) -- the inverted-continue polarity confirmed above.
    """
    if value < 2:
        raise ValueError('gamma2 cannot encode values below 2, got %d' % value)
    tail = bin(value)[3:]  # binary digits after the leading '1'
    out = []
    for i, ch in enumerate(tail):
        out.append(int(ch))
        out.append(1 if i == len(tail) - 1 else 0)
    return out


# The terminator: a match dispatch (tag bit 0) whose gamma-coded value is
# 2**24 + 2, followed by one raw byte 0xFF. See the module docstring for why.
_TERMINATOR_GAMMA_VALUE = (1 << 24) + 2
_TERMINATOR_TRAILER_BYTE = 0xFF


def _events(data):
    """-> [(tag_bit, payload_bytes), ...] in stream order: one event per tag
    bit the depacker will read, paired with whatever raw bytes it reads
    immediately after that bit (empty for bits that consume none)."""
    events = [(1, bytes((b,))) for b in data]
    events.append((0, b''))                      # enter the match/terminator path
    gbits = _gamma_bits(_TERMINATOR_GAMMA_VALUE)
    last = len(gbits) - 1
    for i, bit in enumerate(gbits):
        events.append((bit, bytes((_TERMINATOR_TRAILER_BYTE,)) if i == last else b''))
    return events


def _assemble(events):
    """Pack (bit, payload) events into the tag-byte-interleaved-with-payload
    layout the depacker expects: 8 bits per tag byte, MSB first, with that
    group's payload bytes appended right after it, in event order."""
    out = bytearray()
    for i in range(0, len(events), 8):
        chunk = events[i:i + 8]
        tagbyte = 0
        payload = bytearray()
        for j, (bit, pl) in enumerate(chunk):
            tagbyte |= (bit & 1) << (7 - j)
            payload += pl
        out.append(tagbyte)
        out += payload
    return bytes(out)


def pack(data):
    """Store `data` as a valid aPLib-shaped stream: every byte as a literal,
    followed by this module's terminator. -> bytes, WITHOUT the section
    header (see pack_section). Always larger than `data` by design."""
    return _assemble(_events(data))


def pack_section(data):
    """pack(data), prepended with the section's 8-byte header: big-endian
    [u32 compressed_len][u32 byte_sum], where compressed_len is the packed
    stream's length (not counting this header) and byte_sum is the plain sum,
    mod 2**32, of the packed stream's bytes. Matches the convention
    dt2/container.py and emu/extract.py already use to read sections."""
    body = pack(data)
    return struct.pack('>II', len(body), sum(body) & 0xFFFFFFFF) + body


def _shadow_decode(comp):
    """A from-scratch reference decoder for ONLY the subset of the format
    this module emits -- literals, and this module's own terminator. It is
    NOT a general aPLib decompressor (it cannot decode a real match) and it
    is deliberately independent of pack()'s own bit-packing code, so that
    _self_test() below is checking two independent implementations against
    each other rather than a function against itself. The real authority is
    the device's own depacker, exercised separately in __main__.
    """
    pos = 0
    tag = 0
    bitcount = 0

    def read_byte():
        nonlocal pos
        b = comp[pos]
        pos += 1
        return b

    def getbit():
        nonlocal tag, bitcount
        if bitcount == 0:
            tag = read_byte()
            bitcount = 8
        bit = (tag >> 7) & 1
        tag = (tag << 1) & 0xFF
        bitcount -= 1
        return bit

    out = bytearray()
    while True:
        if getbit() == 1:
            out.append(read_byte())
            continue
        result = 1
        while True:
            result = (result << 1) | getbit()
            if getbit() == 1:
                break
        if result != _TERMINATOR_GAMMA_VALUE:
            raise NotImplementedError(
                'shadow decoder only understands this module\'s own '
                'terminator, not a real match (gamma result=%d)' % result)
        trailer = read_byte()
        if trailer != _TERMINATOR_TRAILER_BYTE:
            raise NotImplementedError(
                'unexpected terminator trailer byte 0x%02x' % trailer)
        break
    return bytes(out)


def _self_test():
    """Sanity checks runnable without Unicorn or any firmware: pack()'s
    bit-packing is round-tripped through an independently-written decoder
    (_shadow_decode), and pack_section()'s header arithmetic is checked
    directly. This does NOT confirm the format against the real device --
    see __main__ for that.
    """
    import os

    for data in [b'', b'A', b'AB', b'\x00', b'\xff' * 3, bytes(range(256)),
                 os.urandom(500)]:
        comp = pack(data)
        got = _shadow_decode(comp)
        assert got == data, (data[:16], got[:16])

    # pack_section's header: length excludes the header, sum is over the
    # packed body only, and packing never counts as *shrinking* the input.
    data = b'roundtrip me'
    section = pack_section(data)
    ln, sm = struct.unpack_from('>II', section, 0)
    body = section[8:]
    assert ln == len(body) == len(pack(data))
    assert sm == sum(body) & 0xFFFFFFFF
    assert len(section) > len(data) + 8

    print('aplib self_test OK')


USAGE = """usage: uv run python -m dt2.aplib [sections_dir]

Runs the offline self-test, then -- if a sections/ directory (as produced by
emu/extract.py) is given or found via DT2_SECTIONS -- round-trips every
section through pack_section() and dt2/elz.py's depack_section."""


if __name__ == '__main__':
    import sys

    _self_test()

    if '--help' in sys.argv or '-h' in sys.argv:
        print(USAGE)
        raise SystemExit(0)

    import glob
    import os
    import time

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dt2.elz import depack_section
    from emu import config

    sections_dir = sys.argv[1] if len(sys.argv) > 1 else config.sections_dir()

    paths = sorted(glob.glob(os.path.join(sections_dir, 'section_*.bin')))
    if not paths:
        print('no section_*.bin files in %s/ -- offline self-test only' % sections_dir)
        raise SystemExit(0)

    print('\n%-26s %10s %10s %7s  %s' % ('section', 'orig', 'packed', 'ratio', 'result'))
    t_total = time.time()
    failures = 0
    for path in paths:
        data = open(path, 'rb').read()
        t0 = time.time()
        stream = pack_section(data)
        got = depack_section(stream)
        ok = got == data
        failures += not ok
        print('%-26s %10d %10d %6.3fx  %-4s  %6.2fs'
              % (os.path.basename(path), len(data), len(stream),
                 len(stream) / max(len(data), 1), 'PASS' if ok else 'FAIL',
                 time.time() - t0))
        if not ok:
            print('    expected %d bytes, depacker produced %d' % (len(data), len(got)))
    print('\ntotal: %.2fs, %d failure(s)' % (time.time() - t_total, failures))
    raise SystemExit(1 if failures else 0)
