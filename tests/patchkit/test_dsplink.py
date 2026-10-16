"""core/dsp-link (docs/findings/31-dsp-link.md): static checks of the module against the four stock images, and the
ColdFire half (dl_send, dl_poll, dl_tick) on the native ColdFire core. The DSP half and the whole channel run in the
coupled emulator: `uv run python -m tools.patchkit.pk_dsplink proof` (tools/patchkit/pk_dsplink.py).

Skips what needs a stock firmware or the m68k toolchain when they are absent."""

import re
import struct
import tomllib

import pytest
from support.paths import REPO_ROOT

from tools.firmware import config
from tools.patchkit import pk_build, pk_link, pk_toolchains

ROOT = REPO_ROOT
MOD = ROOT / "modules" / "core" / "dsp-link"
IMAGES = {
    "dt2-1.16": "Digitakt_II_OS1.16.syx",
    "dt2-1.17": "Digitakt_II_OS1.17.syx",
    "dn2-1.11": "Digitone_II_OS1.11.syx",
    "dn2-1.12": "Digitone_II_OS1.12.syx",
}
# the DSP's frame-tail geometry per device (src/dsp/dt2.s, dn2.s) and the call site's arguments (finding 23, S4b)
GEOMETRY = {"dt2": (0x804, 174), "dn2": (0xA80, 15)}
MAGIC, ACK_MAGIC, ACK_OFF = 0xD15C, 0xA5D1, 0x60
BASE = 0x47E8B000
DATA = 0x47EC0000
needs_gcc = pytest.mark.skipif(
    pk_toolchains.m68k_bin("gcc") is None, reason="m68k toolchain not installed"
)


def define(text: str, name: str) -> int:
    m = re.search(rf"#define\s+{name}\s+(0x[0-9A-Fa-f]+|\d+)u?", text)
    assert m, name
    return int(m.group(1), 0)


def port(name: str) -> dict:
    return tomllib.loads((ROOT / "devices" / "ports" / f"{name}.toml").read_text())


def anchors(name: str) -> dict:
    return {a["name"]: a for a in port(name)["anchor"] if a["name"].startswith("dl.")}


def main_os(image: str) -> bytes:
    from tools.patchkit import port_resolve

    return port_resolve.sections_of(config.stock(IMAGES[image]))[3]


def test_objects_are_current():
    pk_build.build_module(MOD, check=True)


def test_constants_agree_between_the_two_sides():
    c = (MOD / "src/cf/dsp-link.c").read_text()
    s = (MOD / "src/dsp/dsp-link.inc").read_text()
    for name, want in (
        ("DL_MAGIC", MAGIC),
        ("DL_ACK_MAGIC", ACK_MAGIC),
        ("DL_ACK_OFF", ACK_OFF),
        ("DL_AREA_BYTES", 0x20000),
    ):
        assert define(c, name) == want == define(s, name)
    assert define(s, "DL_AREA_WORDS") * 4 == define(s, "DL_AREA_BYTES")
    assert re.search(
        r"\.PKBSS _dl_area, 131072;", s
    )  # the reserved area is DL_AREA_BYTES long


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_anchors_are_in_the_port(image):
    a = anchors(image)
    assert set(a) == {
        "dl.cf.frame_call",
        "dl.cf.frame_fn",
        "dl.cf.tx_lit",
        "dl.dsp.block_tail",
    }


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_call_site_geometry_matches_the_dsp_constants(image):
    if not config.stock(IMAGES[image]).exists():
        pytest.skip("firmware not present")
    dev = image[:3]
    a = anchors(image)
    main = main_os(image)
    base = 0x40000400
    call = a["dl.cf.frame_call"]["addr"] - base
    # `jsr abs.l` to the stock driver, preceded by the four pushes: record copy, frame bytes, data, data bytes
    assert main[call : call + 2] == b"\x4e\xb9"
    assert (
        struct.unpack(">I", main[call + 2 : call + 6])[0] == a["dl.cf.frame_fn"]["addr"]
    )
    pre = main[call - 20 : call]
    m = re.fullmatch(
        rb"\x48\x79(....)\x48\x78(..)\x48\x79(....)\x48\x78(..)", pre, re.S
    )
    assert m, pre.hex()
    frame_bytes = struct.unpack(">H", m.group(2))[0]
    data_bytes = struct.unpack(">H", m.group(4))[0]
    assert frame_bytes == 0xABC
    e0 = (data_bytes // 2 + 1) & ~1
    tail_off, words = GEOMETRY[dev]
    assert 2 * e0 == tail_off
    assert (frame_bytes // 2 - e0) // 2 == words
    for name, want in (("DL_TAIL_OFF", tail_off), ("DL_TAIL_WORDS", words)):
        text = (MOD / "src" / "dsp" / f"{dev}.s").read_text()
        assert define(text, name) == want
    # the literal of `lea <tx queue>,a0` in the driver
    lit = a["dl.cf.tx_lit"]["addr"] - base
    assert main[lit - 2 : lit] == b"\x41\xf9"
    assert 0x80000000 <= struct.unpack(">I", main[lit : lit + 4])[0] < 0x80010000


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_dsp_site_is_the_ring_toggle_store(image):
    from tools.sharc_dev import sharc

    if not config.stock(IMAGES[image]).exists():
        pytest.skip("firmware not present")
    pm = anchors(image)["dl.dsp.block_tail"]["pm"]
    img = sharc.load("dt2-1.16" if image[:3] == "dt2" else "dn2-1.11")
    rows = dict(img.listing(pm - 1, 4))
    assert re.fullmatch(r"DM\(0x[0-9a-f]+\) = R1", rows[hex(pm)]), rows


# ---- the ColdFire half on the core -------------------------------------------------------------------------------

AREA = (
    0x2001AB00  # a stand-in for the DSP's reserved area (a DSP address; only compared)
)
TXLIT = DATA + 0x100  # holds the queue's address
TXQ = DATA + 0x1000  # the fake queue: 4 bytes per entry
REC = DATA + 0x8000


@pytest.fixture(scope="module")
def built():
    if pk_toolchains.m68k_bin("gcc") is None:
        pytest.skip("m68k toolchain not installed")
    syms = {"_dl_area": AREA, "dl_tx_lit": TXLIT, "dl_stock_send": BASE + 0x100}
    return pk_link.build([MOD / "src/cf/dsp-link.c"], BASE, syms)[1]


@pytest.fixture(scope="module")
def machine():
    from tools.patchkit.pk_cfcall import CfMachine

    syx = config.stock(IMAGES["dt2-1.17"])
    if not syx.exists():
        pytest.skip("firmware not present")
    return CfMachine(syx)


def pokes(linked, extra):
    return {
        linked.base: linked.image,
        linked.base + len(linked.image): b"\0" * linked.bss_size,
        TXLIT: struct.pack(">I", TXQ),
        **extra,
    }


def run(machine, linked, extra, calls):
    return machine.run(pokes(linked, extra), calls)


def call(linked, fn, args, reads=()):
    return {"pc": linked.symbols[fn], "args": list(args), "reads": list(reads)}


LINK_FIELDS = ["state", "err", "buf", "dst", "nwords", "xid", "cap", "nchunks", "acked", "next", "stall", "since", "sent", "resent", "nerr", "ending", "dirty"]  # fmt: skip


def link_of(linked, r):
    b = r.reads[0]
    return dict(zip(LINK_FIELDS, struct.unpack(">17I", b), strict=True))


def tail_words(blob: bytes, e0: int, n: int) -> list[int]:
    """Words of the queue tail from entry e0: entry e has a command halfword and the data halfword at +2."""
    out = []
    for j in range(n):
        lo = struct.unpack(">H", blob[4 * (e0 + 2 * j) + 2 : 4 * (e0 + 2 * j) + 4])[0]
        hi = struct.unpack(
            ">H", blob[4 * (e0 + 2 * j + 1) + 2 : 4 * (e0 + 2 * j + 1) + 4]
        )[0]
        out.append(lo | (hi << 16))
    return out


def check_word(words: list[int]) -> int:
    s1 = s2 = 0
    for w in words:
        s1 = (s1 + w) & 0xFFFFFFFF
        s2 = (s2 + s1) & 0xFFFFFFFF
    return s1 ^ s2


@needs_gcc
def test_dl_send_checks_its_arguments(machine, built):
    ln = built.symbols["dl_link"]
    src = DATA + 0x4000
    ok = call(built, "dl_send", [AREA + 0x40, src, 64], [(ln, 68)])
    cases = [
        ok,
        call(
            built, "dl_send", [AREA + 0x80, src, 64]
        ),  # busy: the first request has not been taken
    ]
    r = run(machine, built, {src: bytes(range(64))}, cases)
    assert [x.d0 & 0xFFFFFFFF for x in r] == [0, 0xFFFFFFFF]
    lk = link_of(built, r[0])
    assert (
        lk["state"] == 4
        and lk["dst"] == 0x10
        and lk["nwords"] == 16
        and lk["buf"] == src
    )
    bad = [
        [AREA + 1, src, 64],  # unaligned destination
        [AREA, src, 63],  # length not a multiple of 4
        [AREA, src, 0],
        [AREA - 4, src, 64],  # before the area
        [AREA + 0x20000, src, 4],  # after it
        [AREA + 0x1FFFC, src, 8],  # runs over its end
        [AREA, 0, 64],
    ]
    res = run(machine, built, {}, [call(built, "dl_send", a) for a in bad])
    assert [x.d0 & 0xFFFFFFFF for x in res] == [0xFFFFFFFE] * len(bad)
    last = run(
        machine,
        built,
        {src: bytes(4)},
        [call(built, "dl_send", [AREA + 0x1FFFC, src, 4])],
    )
    assert last[0].d0 == 0


@needs_gcc
def test_first_tick_writes_a_chunk_the_dsp_can_check(machine, built):
    ln = built.symbols["dl_link"]
    src = DATA + 0x4000
    data = bytes((i * 7 + 3) & 0xFF for i in range(1024))
    nwords = len(data) // 4
    words = [int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4)]
    # DT2 geometry: 0x802 data bytes, a 0xabc-byte frame: tail from entry 0x402, 174 words, 171 for the payload
    tick = call(built, "dl_tick", [0x802, 0, 0xABC, 0], [(ln, 68), (TXQ, 0x55E * 4)])
    r = run(
        machine,
        built,
        {src: data},
        [call(built, "dl_send", [AREA + 0x20, src, len(data)]), tick, tick],
    )
    first = r[1]
    lk = link_of(built, first)
    assert (lk["cap"], lk["nchunks"], lk["xid"], lk["state"], lk["sent"]) == (
        171,
        2,
        1,
        1,
        1,
    )
    w = tail_words(first.reads[1], 0x402, 174)
    assert w[0] == MAGIC | (0 << 16)
    off, n, last, end, xid = (
        w[1] & 0x7FFF,
        (w[1] >> 15) & 0xFF,
        (w[1] >> 23) & 1,
        (w[1] >> 24) & 1,
        w[1] >> 25,
    )
    assert (off, n, last, end, xid) == (8, 171, 0, 0, 1)
    assert w[2 : 2 + n] == words[:171]
    assert w[2 + n] == check_word(w[: 2 + n])
    # no acknowledgement came, so the second tick sends chunk 1
    w1 = tail_words(r[2].reads[1], 0x402, 174)
    assert w1[0] == MAGIC | (1 << 16)
    off1, n1, last1 = w1[1] & 0x7FFF, (w1[1] >> 15) & 0xFF, (w1[1] >> 23) & 1
    assert (off1, n1, last1) == (8 + 171, nwords - 171, 1)
    assert w1[2 : 2 + n1] == words[171:]
    assert w1[2 + n1] == check_word(w1[: 2 + n1])
    # halfwords of the queue outside the tail are untouched (the stock driver writes the command halfwords there)
    blob = first.reads[1]
    assert blob[: 0x402 * 4] == bytes(0x402 * 4)


@needs_gcc
def test_acknowledgements_drive_the_state(machine, built):
    ln = built.symbols["dl_link"]
    src = DATA + 0x4000
    data = (
        bytes(range(256)) * 2
    )  # 128 words: one chunk on DT2 geometry... use DN2 geometry: 12 words per chunk
    n_chunks = 11  # 128 / 12 rounded up

    def ack(cum, xid, status, nerr=0):
        rec = bytearray(0xABC)
        struct.pack_into(
            ">HHHH", rec, ACK_OFF, ACK_MAGIC, cum, xid | (status << 8), nerr
        )
        return bytes(rec)

    def tick(rec_addr):
        return call(built, "dl_tick", [0xA80, 0, 0xABC, rec_addr], [(ln, 68)])

    recs = {
        REC + 0x1000 * i: ack(*a)
        for i, a in enumerate([(0, 1, 0), (5, 1, 0), (5, 2, 0), (11, 1, 1), (11, 1, 1)])
    }
    r0, r1, r2, r3, r4 = recs
    calls = [
        call(built, "dl_send", [AREA, src, len(data)]),
        tick(
            r0
        ),  # starts: DN2 geometry (0xa80 data bytes, 15 tail words), 12 payload words
        tick(r1),  # five chunks taken
        tick(r2),  # an acknowledgement for another transfer id is ignored
        tick(r3),  # all taken, status done
        tick(r4),  # END chunks follow
    ]
    r = run(machine, built, {src: data, **recs}, calls)
    lk = [link_of(built, x) for x in r[1:]]
    assert (lk[0]["cap"], lk[0]["nchunks"], lk[0]["state"]) == (12, n_chunks, 1)
    assert lk[1]["acked"] == 5 and lk[1]["state"] == 1
    assert lk[2]["acked"] == 5 and lk[2]["state"] == 1
    assert (
        lk[3]["acked"] == 11 and lk[3]["state"] == 2 and lk[3]["ending"] == 5
    )  # the first END chunk goes out in the same tick
    assert lk[4]["ending"] == 4
    # polling
    p = run(
        machine,
        built,
        {src: data, **recs},
        [
            call(built, "dl_send", [AREA, src, len(data)]),
            call(built, "dl_poll", [REC + 0x100], [(REC + 0x100, 28)]),
        ],
    )
    assert p[1].d0 == 1  # busy
    assert struct.unpack(">7I", p[1].reads[0])[0] == 1


@needs_gcc
def test_dsp_error_status_and_timeout_stop_the_transfer(machine, built):
    ln = built.symbols["dl_link"]
    src = DATA + 0x4000
    rec = bytearray(0xABC)
    struct.pack_into(
        ">HHHH", rec, ACK_OFF, ACK_MAGIC, 0, 1 | (3 << 8), 0
    )  # status 3: destination refused
    tick = call(built, "dl_tick", [0xA80, 0, 0xABC, REC], [(ln, 68)])
    r = run(
        machine,
        built,
        {src: bytes(64), REC: bytes(rec)},
        [call(built, "dl_send", [AREA, src, 64]), tick],
    )
    lk = link_of(built, r[1])
    assert lk["state"] == 3 and lk["err"] == 3 and lk["ending"] == 5
    # no acknowledgement at all: the timeout is 900 ticks
    ticks = [
        call(built, "dl_tick", [0xA80, 0, 0xABC, 0], [(ln, 68)]) for _ in range(902)
    ]
    r = run(
        machine,
        built,
        {src: bytes(64)},
        [call(built, "dl_send", [AREA, src, 64]), *ticks],
    )
    assert link_of(built, r[-1])["state"] == 3 and link_of(built, r[-1])["err"] == 100
    # a frame with no room in the tail (a data length that fills it) is a geometry error, never a write
    r = run(
        machine,
        built,
        {src: bytes(64)},
        [
            call(built, "dl_send", [AREA, src, 64]),
            call(built, "dl_tick", [0xAB0, 0, 0xABC, 0], [(ln, 68), (TXQ, 64)]),
        ],
    )
    assert link_of(built, r[1])["err"] == 101 and r[1].reads[1] == bytes(64)
