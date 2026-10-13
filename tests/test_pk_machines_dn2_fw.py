"""core/machines (Digitone II) as a module: the ColdFire routines against a model of the Rust generator's behaviour
(native/patchkit/src/machines/dn2.rs). The routines touch no firmware code, so the DT2 1.17 ready state runs them.

Skips when the m68k toolchain or the DT2 1.17 image is absent."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pk_cfunit  # noqa: E402
import pk_toolchains  # noqa: E402

SYX = ROOT / "Digitakt_II_OS1.17.syx"
SRC = ROOT / "modules" / "core" / "machines" / "src" / "cf" / "dn2" / "machines.s"
ROWSIZE = 64
FIRST = 5
ROWS = 0x47EC0000
STOCK_LIST = 0x47EC1000
STR_AT = 0x47EC3000
OBJ = 0x47EC4000
RC = [0x1000 + 0x100 * i for i in range(9)]
RC_LEN = [4, 4, 6, 6, 6, 6, 6, 6, 6]
STOCK = [0, 1, 2, 3, 4]

STUBS = [
    "_mf_t_dispatch",
    "_mf_t_list",
    "_mf_t_group",
    "_mf_t_typemap0",
    "_mf_t_typemap1",
    "_mf_t_typemap2",
    ("_mf_ft", 6),
    ("_mf_rd", 8),
    ("_mf_ow", 6),
]

pytestmark = [
    pytest.mark.skipif(
        pk_toolchains.m68k_bin("gcc") is None, reason="m68k toolchain not installed"
    ),
    pytest.mark.skipif(not SYX.exists(), reason="Digitakt_II_OS1.17.syx not present"),
]

# (name, short, clone_of, position, own_dsp)
ENTRIES = [
    ("FM Clone", "FMC", 0, 4, 0),
    ("Sine", "SIN", 0, 4, 1),
    ("Mid Clone", "MCL", 4, 255, 0),
]
COUNT = len(ENTRIES)


def be32(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


def tables():
    rows, strings = b"", b""
    for name, short, clone, pos, own in ENTRIES:
        n = STR_AT + len(strings)
        strings += name.encode() + b"\0"
        s = STR_AT + len(strings)
        strings += short.encode() + b"\0"
        rows += be32(0) + be32(0) + be32(0) * 8 + be32(10)
        rows += be32(clone) + be32(pos) + be32(own) + be32(n) + be32(s)
    assert len(rows) == ROWSIZE * COUNT
    return {
        ROWS: rows,
        STR_AT: strings,
        STOCK_LIST: b"".join(be32(t) for t in STOCK),
    }


@pytest.fixture(scope="module")
def unit():
    sym = {
        "_mf_first": FIRST,
        "_mf_count": COUNT,
        "_mf_rows": ROWS,
        "_mf_list_stock": STOCK_LIST,
    }
    sym.update({f"_mf_rc{i}": a for i, a in enumerate(RC)})
    return pk_cfunit.CfUnit(
        SYX,
        [SRC],
        STUBS,
        sym,
        [
            "mf_dispatch",
            "mf_list_entry",
            "mf_group",
            "mf_typemap0",
            "mf_typemap1",
            "mf_typemap2",
            "mf_frametype",
            "mf_reader",
            "mf_owner",
        ],
    )


def test_dispatch_and_list(unit):
    mem = tables()
    res = unit.run_cases(
        mem,
        [
            {"routine": "mf_dispatch", "stack": [unit.rts, FIRST + k]}
            for k in range(COUNT)
        ]
        + [
            {"routine": "mf_dispatch", "stack": [unit.rts, t]}
            for t in (4, FIRST + COUNT)
        ]
        + [{"routine": "mf_list_entry"}],
    )
    for k in range(COUNT):
        assert res[k].stub == "rts" and res[k].d[0] == ROWS + k * ROWSIZE
    assert [r.stub for r in res[COUNT:]] == ["_mf_t_dispatch"] * 2 + ["_mf_t_list"]


def test_group_sends_added_types_to_the_group_one_path(unit):
    mem = tables()
    res = unit.run_cases(
        mem, [{"routine": "mf_group", "d": {0: t, 1: 5}} for t in range(0, 12)]
    )
    for t, r in enumerate(res):
        assert r.stub == "_mf_t_group"
        assert r.d[0] == (0 if FIRST <= t < FIRST + COUNT else t)


@pytest.mark.parametrize(
    "name,off", [("mf_typemap0", 4), ("mf_typemap1", 8), ("mf_typemap2", 0xC)]
)
def test_type_maps_replace_the_argument_by_clone_of(unit, name, off):
    mem = tables()
    ts = [0, 4, FIRST, FIRST + 1, FIRST + 2, FIRST + COUNT, 99]
    cases = []
    for t in ts:
        stack = [0xAAAA] * 8
        stack[off // 4] = t
        cases.append({"routine": name, "d": {0: 7, 1: 8}, "a": {0: 9}, "stack": stack})
    for t, r in zip(ts, unit.run_cases(mem, cases), strict=True):
        want = ENTRIES[t - FIRST][2] if FIRST <= t < FIRST + COUNT else t
        assert r.stub == "_mf_t_" + name[3:]
        assert r.stack[off // 4] == want
        assert r.d[1] == 8 and r.a[0] == 9  # d0 is free at the sites
        assert [x for i, x in enumerate(r.stack[:8]) if i != off // 4] == [0xAAAA] * 7


def test_frame_type_sends_clone_unless_own_dsp(unit):
    mem = tables()
    mem[OBJ + 0xD8C] = bytes([0])
    ts = [0, 3, 4, FIRST, FIRST + 1, FIRST + 2, FIRST + COUNT, 200]
    res = []
    for t in ts:
        m = dict(mem)
        m[OBJ + 0xD8C] = bytes([t])
        res.append(
            unit.run_cases(
                m,
                [
                    {
                        "routine": "mf_frametype",
                        "d": {0: 0x12345678, 1: 5, 2: 6},
                        "a": {0: OBJ, 1: 7},
                    }
                ],
            )[0]
        )
    for t, r in zip(ts, res, strict=True):
        if FIRST <= t < FIRST + COUNT:
            name, short, clone, pos, own = ENTRIES[t - FIRST]
            want = t if own else clone
        else:
            want = t if t < 128 else t - 256
        assert r.stub == "_mf_ft"
        assert r.d[0] & 0xFFFF == want & 0xFFFF
        assert r.d[1:3] == [5, 6] and r.a[1] == 7


def test_type_reader_maps_except_for_the_identity_callers(unit):
    cases, want = [], []
    for t in [0, 4, FIRST, FIRST + 1, FIRST + 2, FIRST + COUNT, -3]:
        mem_t = bytes([t & 0xFF])
        for caller in [0x777, *[RC[i] + RC_LEN[i] for i in range(9)], RC[0]]:
            cases.append(
                (
                    mem_t,
                    {
                        "routine": "mf_reader",
                        "d": {0: 1, 1: 2},
                        "a": {0: OBJ, 1: 0x55},
                        "stack": [0x99, caller],
                    },
                )
            )
            real = caller in [RC[i] + RC_LEN[i] for i in range(9)]
            w = ENTRIES[t - FIRST][2] if FIRST <= t < FIRST + COUNT and not real else t
            want.append(w & 0xFFFFFFFF)
    for (mem_t, case), w in zip(cases, want, strict=True):
        mem = tables()
        mem[OBJ + 0xDE] = mem_t
        r = unit.run_cases(mem, [case])[0]
        assert r.stub == "_mf_rd"
        assert r.d[0] == w, (mem_t, case["stack"], hex(r.d[0]), hex(w))
        assert r.a[1] == 0x55 and r.stack[:2] == [0x99, case["stack"][1]]


def test_owner_test_maps_d3(unit):
    for t in [0, 2, FIRST, FIRST + 1, FIRST + 2, FIRST + COUNT]:
        mem = tables()
        mem[OBJ + 0xDE] = bytes([t])
        r = unit.run_cases(
            mem,
            [
                {
                    "routine": "mf_owner",
                    "d": {0: 11, 2: 0x4242, 3: 9},
                    "a": {0: OBJ, 1: 3},
                    "stack": [1, 2],
                }
            ],
        )[0]
        want = ENTRIES[t - FIRST][2] if FIRST <= t < FIRST + COUNT else t
        assert r.stub == "_mf_ow" and r.d[3] == want
        assert r.d[0] == 11 and r.a[1] == 3
        assert r.stack[0] == 0x4242 and r.stack[1:3] == [1, 2]  # d2 pushed
