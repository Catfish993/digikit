"""dt2/aplibpack.py: the compressor round-trips through dt2/elz.py, and
dt2.build.rebuild keeps untouched sections verbatim."""

import random

import pytest
from support.paths import REPO_ROOT

from dt2 import aplibpack, build, elz
from dt2.container import sections

ROOT = REPO_ROOT


def _samples():
    rng = random.Random(7)
    yield b""
    yield b"a"
    yield b"ab"
    yield bytes(100_000)  # one long run: offset 1, lengths past the gamma steps
    yield bytes([0xFF]) * 70_000
    yield bytes(rng.randrange(256) for _ in range(5000))
    yield (b"abcabcabd" * 400)[:3500]
    # Offsets past 3328 (the far-match length bias) and the repeat-offset code.
    block = bytes(rng.randrange(256) for _ in range(4000))
    yield block + bytes(rng.randrange(256) for _ in range(500)) + block + block
    words = [
        bytes(rng.randrange(256) for _ in range(rng.randrange(2, 9))) for _ in range(60)
    ]
    yield b"".join(rng.choice(words) for _ in range(4000))


@pytest.mark.parametrize("data", list(_samples()))
def test_round_trip(data):
    stream = aplibpack.pack_section(data)
    assert elz.depack_section(stream) == data
    assert len(stream) <= len(data) + len(data) // 8 + 32 or not data


def test_compresses_repetitive_data():
    data = (b"digitakt" * 4000)[:30_000]
    assert len(aplibpack.pack(data)) < len(data) // 20


def test_rebuild_keeps_untouched_sections_verbatim():
    syx = ROOT / "Digitakt_II_OS1.16.syx"
    if not syx.exists():
        pytest.skip("needs the 1.16 .syx")
    cont, secs = sections(str(syx))
    stock = {sid: bytes(cont[off : off + clen]) for sid, off, clen, _ in secs}
    new8 = bytearray(elz.depack_section(stock[8]))
    new8[0] ^= 1
    out = ROOT / "out" / "runs" / "test_aplibpack.syx"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_bytes(build.rebuild(str(syx), {8: bytes(new8)}, pack="aplib"))
        cont2, secs2 = sections(str(out))
        got = {sid: bytes(cont2[off : off + clen]) for sid, off, clen, _ in secs2}
    finally:
        out.unlink(missing_ok=True)
    for sid in stock:
        if sid != 8:
            assert got[sid] == stock[sid], sid
    assert elz.depack_section(got[8]) == bytes(new8)
    assert len(got[8]) < len(build.store_for_section(8, bytes(new8)))
