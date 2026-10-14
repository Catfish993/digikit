"""tools/dspdiff.py on synthetic memory dumps."""

from __future__ import annotations

import io
import struct
import sys

from support.paths import REPO_ROOT

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "tools"))
import dspdiff as dd  # noqa: E402

BASE = 0x28000000 + 0x25C5E0


def words(*values: int) -> bytes:
    return b"".join(struct.pack("<I", v) for v in values)


def test_identical_dumps_have_no_difference() -> None:
    a = dd.to_pages([(BASE, words(1, 2, 3))])
    b = dd.to_pages([(BASE, words(1, 2, 3))])
    assert dd.diff_words([a, b]) == []


def test_differing_words_and_partial_marker() -> None:
    a = dd.to_pages([(BASE, words(1, 2, 3))])
    b = dd.to_pages([(BASE, words(1, 9, 3)), (BASE + 12, words(5))])
    found = dd.diff_words([a, b])
    assert [(w[0], w[1]) for w in found] == [(BASE + 4, [2, 9]), (BASE + 12, [0, 5])]
    assert [w[2] for w in found] == [False, True]  # the second word is missing in A


def test_three_dumps_and_grouping() -> None:
    a = dd.to_pages([(BASE, words(0, 0, 0, 0, 0, 0, 0, 0, 0, 0))])
    b = dd.to_pages([(BASE, words(1, 1, 0, 0, 0, 0, 0, 0, 0, 1))])
    c = dd.to_pages([(BASE, words(0, 0, 0, 0, 0, 0, 0, 0, 0, 0))])
    found = dd.diff_words([a, b, c])
    assert [w[1] for w in found] == [[0, 1, 0]] * 3
    groups = dd.group(found, gap=2)
    assert [len(g) for g in groups] == [2, 1]


def test_noise_mask_drops_volatile_words() -> None:
    n1 = dd.to_pages([(BASE, words(7, 1))])
    n2 = dd.to_pages([(BASE, words(8, 1))])
    mask = dd.noise_mask(n1, n2)
    a = dd.to_pages([(BASE, words(1, 2))])
    b = dd.to_pages([(BASE, words(2, 3))])
    assert [w[0] for w in dd.diff_words([a, b])] == [BASE, BASE + 4]
    assert [w[0] for w in dd.diff_words([a, b], mask)] == [BASE + 4]


def test_pages_cross_a_page_boundary() -> None:
    edge = 0x28000000 + 0x1000 - 4
    pages = dd.to_pages([(edge, words(1, 2))])
    assert sorted(pages) == [0x28000000, 0x28001000]


def test_address_names_and_float() -> None:
    assert dd.dsp_address(BASE) == "0x25c5e0"
    assert dd.dsp_address(0x80000000 + 8) == "nw 0x10000002"
    assert dd.f32(0x3F800000) == "1"


def test_report_lists_values() -> None:
    a = dd.to_pages([(BASE, words(0x3F800000))])
    b = dd.to_pages([(BASE, words(0))])
    out = io.StringIO()
    n = dd.report(["a", "b"], dd.diff_words([a, b]), gap=4, show=8, out=out)
    assert n == 1
    assert "A=3f800000(1)  B=00000000(0)" in out.getvalue()
