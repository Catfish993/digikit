"""tools/cffacts.py and tools/cfmatch.py: the masked instruction tokens, the
matcher stages on a synthetic pair of images, and (when the dumps are present)
the match of Digitakt II 1.15C -> 1.16 against the Version Tracking export."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import cffacts
    import cfmatch
finally:
    sys.path.pop(0)

BASE = 0x40000400
HI = 0x40320000


def insn(addr: int, n: int, form: str, text: str) -> tuple[int, int, str, str]:
    return (addr, n, form, text)


def tok(insns, entry=0x40001000, lo=0x40001000, hi=0x40001100):
    return cffacts.tokens(insns, entry, lo, hi, BASE, HI)


def test_tokens_mask_what_a_relink_moves() -> None:
    body = [
        insn(0x40001000, 6, "jsr", "jsr (0x4011e8f0).l"),
        insn(0x40001006, 6, "move", "move.l #0x40203068,(a2)"),
        insn(0x4000100C, 6, "move", "move.l d0,(-0x7fffb8fc).l"),
        insn(0x40001012, 6, "move", "move.l #0x44f370a0,d1"),
        insn(0x40001018, 2, "bcc", "beq.b 0x40001040"),
        insn(0x4000101A, 2, "bcc", "bne.w 0x4011e8f0"),
        insn(0x4000101C, 4, "move", "move.l (0x10,a7),d0"),
    ]
    strict, loose, mnem = tok(body)
    assert strict == [
        "jsr (@X).l",
        "move.l #@I,(a2)",
        "move.l d0,(-0x7fffb8fc).l",  # SRAM stays: it does not move
        "move.l #@R,d1",
        "beq @+40",
        "bne @X",
        "move.l (0x10,a7),d0",
    ]
    assert (
        loose[1] == "move.l #N,(a2)"
        and loose[4] == "beq N"
        and loose[6] == "move.l (N,a7),d0"
    )
    assert mnem == ["jsr", "move.l", "move.l", "move.l", "beq", "bne", "move.l"]


def test_branch_size_suffix_is_not_part_of_the_hash() -> None:
    a = tok([insn(0x40001000, 2, "bra", "bra.b 0x40001010")])
    b = tok([insn(0x40001000, 4, "bra", "bra.w 0x40001010")])
    assert a == b


class Fake(cfmatch.Img):
    """An image of tiny functions: {entry: (size, mnemonics, loose, strict, callees)}."""

    def __init__(self, funcs, strs=None, vtables=None):
        self.name = "fake"
        self.entries = sorted(funcs)
        self.fname = {e: "FUN_%x" % e for e in funcs}
        self.size = {e: f[0] for e, f in funcs.items()}
        self.mnem = {e: f[1].split() for e, f in funcs.items()}
        self.ninsn = {e: len(self.mnem[e]) for e in funcs}
        self.h_mnem = {e: " ".join(self.mnem[e]) for e in funcs}
        self.h_loose = {e: f[2] for e, f in funcs.items()}
        self.h_strict = {e: f[3] for e, f in funcs.items()}
        self.calls = {e: list(f[4]) for e, f in funcs.items() if f[4]}
        self.callers = collections.defaultdict(set)
        for e, f in funcs.items():
            for c in f[4]:
                self.callers[c].add(e)
        self.strs = strs or {}
        self.vtables = vtables or {}
        self._tri = {}


def seq(n: int, salt: str) -> str:
    return " ".join("op%d%s" % (i % 7, salt) for i in range(n))


def test_matcher_stages_on_a_synthetic_pair() -> None:
    # src: A strict-unique, B changed (loose hash differs, same shape), C and D are
    # both reached only through callees, E sits between matched neighbours.
    src = Fake(
        {
            0x100: (40, seq(10, "a"), "la", "sa", [0x200, 0x300]),
            0x200: (60, seq(15, "b"), "lb", "sb", []),
            0x300: (60, seq(15, "c"), "lc", "sc", []),
            0x400: (80, seq(20, "d"), "ld", "sd", []),
            0x500: (50, seq(12, "e"), "le", "se", []),
            0x600: (50, seq(12, "f"), "lf", "sf", []),
        }
    )
    dst = Fake(
        {
            0x1100: (40, seq(10, "a"), "la", "sa", [0x1300, 0x1400]),  # strict
            0x1300: (60, seq(15, "b"), "lb", "sb", []),  # loose/strict equal: unique
            0x1400: (
                62,
                seq(14, "c") + " extra",
                "lc2",
                "sc2",
                [],
            ),  # changed: callee stage
            0x1500: (80, seq(20, "d"), "ld", "sd", []),
            0x1600: (
                52,
                seq(11, "e") + " extra",
                "le2",
                "se2",
                [],
            ),  # changed: gap stage
            0x1700: (50, seq(12, "f"), "lf", "sf", []),
        }
    )
    mt = cfmatch.Matcher(src, dst)
    mt.run()
    assert mt.m == {
        0x100: 0x1100,
        0x200: 0x1300,
        0x300: 0x1400,
        0x400: 0x1500,
        0x500: 0x1600,
        0x600: 0x1700,
    }
    assert mt.method[0x100] == "strict"
    assert mt.method[0x300] == "callee"
    assert mt.method[0x500] == "gap"


def test_ambiguous_hash_is_not_matched() -> None:
    twin = (20, seq(8, "t"), "lt", "st", [])
    src = Fake({0x100: twin, 0x200: twin})
    dst = Fake({0x1100: twin, 0x1200: twin})
    mt = cfmatch.Matcher(src, dst)
    mt.stage_hashes()
    assert mt.m == {}


VT = ROOT / "out" / "vt" / "dt2-1.15C_to_dt2-1.16.json"
DUMPS = [
    ROOT / "out" / "ghidra" / n / "manifest.json"
    for n in ("dt2-1.15C-emac", "dt2-1.16-emac")
]


@pytest.mark.skipif(
    not VT.exists() or not all(d.exists() for d in DUMPS),
    reason="needs the 1.15C and 1.16 Ghidra dumps and the Version Tracking export",
)
def test_digitakt_1_15c_to_1_16_against_version_tracking(tmp_path) -> None:
    mt = cfmatch.load_pair("dt2-1.15C-emac", "dt2-1.16-emac", str(tmp_path))
    res = cfmatch.evaluate(mt, str(VT))
    assert res["matched_pct_src"] > 95
    assert res["vt"]["precision_vs_vt"] > 0.995
    assert res["vt"]["recall_vs_vt"] > 0.99
    # class of the RTTI names agrees for the pairs that have both (the names do not
    # come from the byte hashes)
    pairs = sum(v["pairs"] for v in res["rtti_class_agree"].values())
    agree = sum(v["agree"] for v in res["rtti_class_agree"].values())
    assert agree / pairs > 0.995
    json.dumps(res)
