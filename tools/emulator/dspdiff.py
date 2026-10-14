#!/usr/bin/env python3
"""Diff DSP memory dumps written by `coupled-run --dump-dsp`.

    uv run python -m tools.emulator.dspdiff A.bin B.bin [C.bin ...] [--image dn2-1.11]
    uv run python -m tools.emulator.dspdiff del.bin dry.bin --ignore-volatile --noise dry.bin dry2.bin

A dump is the engine's canonical state blob (`SHRD`, tools/sharc_dev/sharc_diff.py): every
memory byte written since the loaded image, as byte ranges keyed by canonical
address. Internal memory is 0x28000000 + the DSP byte address (so 0x2825c5e0 is
DSP 0x25c5e0); SDRAM is 0x80000000 + 4 * (normal word - 0x10000000).
A byte missing from a dump was never written (the image value); it is read as
0 here and the range is marked `?` when only some dumps hold it.

The report lists the differing 32-bit words grouped into ranges, each with every
dump's value (hex and float32). With `--image` the ranges are annotated with the
functions of the program database (tools/sharc_dev/sharc.py) whose code references them:
`dataref` values inside the range and resolved `ptr` loads and stores.
`--ignore-volatile --noise A B` drops the words that differ between two dumps of
the same scenario.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

PAGE = 4096
INTERNAL = 0x28000000
INTERNAL_END = 0x28400000
SDRAM = 0x80000000

Pages = dict[int, tuple["np.ndarray", "np.ndarray"]]


def read_ranges(path: str | Path) -> list[tuple[int, bytes]]:
    """The memory ranges (canonical byte address, bytes) of a dump."""
    from tools.sharc_dev import sharc_diff  # heavy import (sharc.py, networkx): only when parsing

    blob = Path(path).read_bytes()
    start = blob.find(b"SHRD")
    if start < 0:
        raise ValueError(f"{path}: no SHRD state blob")
    state = sharc_diff.unpack_state(blob[start:])
    return [
        (addr, bytes.fromhex(r["data"]))
        for addr, r in sorted(state["memory_ranges"].items())
    ]


def to_pages(ranges: Sequence[tuple[int, bytes]]) -> Pages:
    pages: Pages = {}
    for addr, data in ranges:
        pos = 0
        while pos < len(data):
            a = addr + pos
            base, off = a - a % PAGE, a % PAGE
            n = min(len(data) - pos, PAGE - off)
            if base not in pages:
                pages[base] = (np.zeros(PAGE, np.uint8), np.zeros(PAGE, np.uint8))
            d, m = pages[base]
            d[off : off + n] = np.frombuffer(data, np.uint8, n, pos)
            m[off : off + n] = 1
            pos += n
    return pages


def dsp_address(addr: int) -> str:
    """The DSP-side name of a canonical byte address."""
    if INTERNAL <= addr < INTERNAL_END:
        return f"{addr - INTERNAL:#08x}"
    if addr >= SDRAM:
        return f"nw {0x10000000 + (addr - SDRAM) // 4:#x}"
    return f"raw {addr:#x}"


def f32(word: int) -> str:
    value = struct.unpack("<f", struct.pack("<I", word))[0]
    return f"{value:.6g}"


def diff_words(
    dumps: Sequence[Pages], mask: Pages | None = None
) -> list[tuple[int, list[int], bool]]:
    """Differing words: (canonical address, one value per dump, partial).

    `partial` is true when some dump lacks one of the word's bytes.
    """
    zero = np.zeros(PAGE, np.uint8)
    out: list[tuple[int, list[int], bool]] = []
    bases = sorted(set().union(*(set(p) for p in dumps)))
    for base in bases:
        datas = np.stack([p[base][0] if base in p else zero for p in dumps])
        masks = np.stack([p[base][1] if base in p else zero for p in dumps])
        words = datas.view("<u4").reshape(len(dumps), PAGE // 4)
        differ = (words != words[0]).any(axis=0)
        if mask is not None and base in mask:
            differ &= mask[base][0].view("<u4")[: PAGE // 4] == 0
        present = masks.reshape(len(dumps), PAGE // 4, 4).sum(axis=2)
        partial = ((present > 0) & (present < 4)).any(axis=0) | (
            present.min(axis=0) != present.max(axis=0)
        )
        for i in np.nonzero(differ)[0]:
            out.append(
                (
                    base + 4 * int(i),
                    [int(w) for w in words[:, i]],
                    bool(partial[i]),
                )
            )
    return out


def noise_mask(a: Pages, b: Pages) -> Pages:
    """Pages whose word is non-zero where two same-scenario dumps differ."""
    mask: Pages = {}
    for addr, _values, _ in diff_words([a, b]):
        base = addr - addr % PAGE
        if base not in mask:
            mask[base] = (np.zeros(PAGE, np.uint8), np.zeros(PAGE, np.uint8))
        mask[base][0].view("<u4")[(addr - base) // 4] = 1
    return mask


def group(
    words: Sequence[tuple[int, list[int], bool]], gap: int
) -> list[list[tuple[int, list[int], bool]]]:
    groups: list[list[tuple[int, list[int], bool]]] = []
    for w in words:
        if groups and w[0] - groups[-1][-1][0] <= 4 * gap:
            groups[-1].append(w)
        else:
            groups.append([w])
    return groups


def annotate(img, start: int, end: int) -> list[str]:
    """Functions whose code references DSP byte addresses [start, end)."""
    if not INTERNAL <= start < INTERNAL_END:
        return []
    lo, hi = start - INTERNAL, end - INTERNAL
    rows = img.sql(
        "SELECT d.value, d.sw, d.role, i.function_sw FROM dataref d "
        "JOIN insn i ON i.image = d.image AND i.sw = d.sw "
        "WHERE d.image = ? AND d.value >= ? AND d.value < ? ORDER BY d.sw",
        img.name,
        lo,
        hi,
    )
    notes = [
        f"dataref {v:#x} at {sw:#x} ({role}) in fn {fn:#x}"
        if fn
        else f"dataref {v:#x} at {sw:#x}"
        for v, sw, role, fn in rows[:6]
    ]
    ptrs = img.sql(
        "SELECT p.address, p.sw, p.direction, i.function_sw FROM ptr p "
        "JOIN insn i ON i.image = p.image AND i.sw = p.sw "
        "WHERE p.image = ? AND p.address IS NOT NULL AND p.address >= ? AND p.address < ? "
        "ORDER BY p.sw",
        img.name,
        lo,
        hi,
    )
    notes += [
        f"{d} {a:#x} at {sw:#x} in fn {fn:#x}" if fn else f"{d} {a:#x} at {sw:#x}"
        for a, sw, d, fn in ptrs[:6]
    ]
    more = len(rows) + len(ptrs) - len(notes)
    if more > 0:
        notes.append(f"... {more} more")
    return notes


def report(
    names: Sequence[str],
    words: Sequence[tuple[int, list[int], bool]],
    gap: int,
    show: int,
    img=None,
    out=sys.stdout,
) -> int:
    groups = group(words, gap)
    print(
        f"{len(words)} differing words in {len(groups)} ranges; "
        f"dumps: {', '.join(f'{chr(65 + i)}={n}' for i, n in enumerate(names))}",
        file=out,
    )
    for g in groups:
        start, end = g[0][0], g[-1][0] + 4
        flag = "?" if any(w[2] for w in g) else ""
        print(
            f"\n{dsp_address(start)} .. {dsp_address(end)}  "
            f"({len(g)} words differ, {(end - start) // 4} spanned){flag}",
            file=out,
        )
        for addr, values, _ in g[:show]:
            cells = "  ".join(
                f"{chr(65 + i)}={v:08x}({f32(v)})" for i, v in enumerate(values)
            )
            print(f"  {dsp_address(addr)}: {cells}", file=out)
        if len(g) > show:
            print(f"  ... {len(g) - show} more words", file=out)
        if img is not None:
            for note in annotate(img, start, end):
                print(f"  ref: {note}", file=out)
    return len(groups)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dumps", nargs="+", metavar="DUMP", help="two or more dumps")
    p.add_argument(
        "--image",
        metavar="NAME",
        help='annotate with tools/sharc_dev/sharc.py image, e.g. "dn2-1.11"',
    )
    p.add_argument(
        "--gap", type=int, default=4, help="merge words closer than this many words"
    )
    p.add_argument("--show", type=int, default=8, help="words listed per range")
    p.add_argument(
        "--ignore-volatile",
        action="store_true",
        help="drop words that differ between --noise A B",
    )
    p.add_argument(
        "--noise", nargs=2, metavar=("A", "B"), help="two dumps of the same scenario"
    )
    args = p.parse_args(argv)
    if len(args.dumps) < 2:
        p.error("need at least two dumps")
    if args.ignore_volatile and not args.noise:
        p.error("--ignore-volatile needs --noise A B")
    dumps = [to_pages(read_ranges(d)) for d in args.dumps]
    mask = None
    if args.ignore_volatile:
        mask = noise_mask(*(to_pages(read_ranges(d)) for d in args.noise))
    img = None
    if args.image:
        from tools.sharc_dev import sharc

        img = sharc.load(args.image)
    words = diff_words(dumps, mask)
    report([Path(d).name for d in args.dumps], words, args.gap, args.show, img)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
