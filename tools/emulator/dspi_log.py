"""Read and query a DSPI frame log written by the DSP peer (DIGI_DSPI_LOG=path).

Format: magic b"DSPILOG1", then per frame: u64 BE index, u8 attached,
u32 BE tx length + tx bytes, u32 BE reply length + reply bytes. Frames arrive
at 1500 Hz, so index / 1500 is seconds.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"DSPILOG1"
RATE = 1500


@dataclass
class Frame:
    index: int
    attached: bool
    tx: bytes
    reply: bytes


def read_log(path: str | Path) -> Iterator[Frame]:
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path}: not a DSPILOG1 file")
        while True:
            head = f.read(9)
            if not head:
                return
            if len(head) < 9:
                raise ValueError(f"{path}: truncated frame header")
            index, attached = struct.unpack(">QB", head)
            blobs = []
            for _ in range(2):
                raw = f.read(4)
                if len(raw) < 4:
                    raise ValueError(f"{path}: truncated frame {index}")
                (n,) = struct.unpack(">I", raw)
                blob = f.read(n)
                if len(blob) < n:
                    raise ValueError(f"{path}: truncated frame {index}")
                blobs.append(blob)
            yield Frame(index, bool(attached), blobs[0], blobs[1])


def _ranges(offsets: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for o in offsets:
        if out and out[-1][1] == o - 1:
            out[-1] = (out[-1][0], o)
        else:
            out.append((o, o))
    return out


def changed_offsets(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """Inclusive (first, last) ranges of offsets where a and b differ.

    Offsets past the end of the shorter one count as different.
    """
    n = min(len(a), len(b))
    offsets = [i for i in range(n) if a[i] != b[i]]
    offsets.extend(range(n, max(len(a), len(b))))
    return _ranges(offsets)


def fmt_ranges(ranges: list[tuple[int, int]]) -> str:
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges) or "-"


def parse_ranges(text: str) -> list[tuple[int, int]]:
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def _in(ranges: list[tuple[int, int]], o: int) -> bool:
    return any(a <= o <= b for a, b in ranges)


def _frames(path: str) -> list[Frame]:
    return list(read_log(path))


def _frame(frames: list[Frame], index: int) -> Frame:
    for fr in frames:
        if fr.index == index:
            return fr
    raise SystemExit(f"no frame {index}")


def cmd_summary(args: argparse.Namespace) -> None:
    frames = _frames(args.log)
    print(f"frames: {len(frames)} ({len(frames) / RATE:.3f} s)")
    hist = Counter(
        int.from_bytes(f.tx[:2], "big") if len(f.tx) >= 2 else -1 for f in frames
    )
    print("first u16 of tx:")
    for word, n in sorted(hist.items()):
        print(f"  0x{word:04x}: {n}" if word >= 0 else f"  short: {n}")
    print(f"distinct tx payloads: {len({f.tx for f in frames})}")
    changed: set[int] = set()
    for prev, cur in zip(frames, frames[1:], strict=False):
        for lo, hi in changed_offsets(prev.tx, cur.tx):
            changed.update(range(lo, hi + 1))
    print(f"tx offsets that ever change: {fmt_ranges(_ranges(sorted(changed)))}")


def _diff(label: str, a: bytes, b: bytes, limit: int | None) -> None:
    print(f"{label}:")
    shown = 0
    for lo, hi in changed_offsets(a, b):
        for o in range(lo, hi + 1):
            if limit is not None and shown >= limit:
                print(f"  ... (--len {limit})")
                return
            va = f"{a[o]:02x}" if o < len(a) else "--"
            vb = f"{b[o]:02x}" if o < len(b) else "--"
            print(f"  {o}: {va} {vb}")
            shown += 1
    if shown == 0:
        print("  identical")


def cmd_diff(args: argparse.Namespace) -> None:
    frames = _frames(args.log)
    fa, fb = _frame(frames, args.a), _frame(frames, args.b)
    print(
        f"A={fa.index} ({fa.index / RATE:.4f} s) B={fb.index} ({fb.index / RATE:.4f} s)"
    )
    _diff("tx", fa.tx, fb.tx, args.len)
    _diff("reply", fa.reply, fb.reply, args.len)


def cmd_changes(args: argparse.Namespace) -> None:
    frames = _frames(args.log)
    limit = parse_ranges(args.offsets) if args.offsets else None
    for prev, cur in zip(frames, frames[1:], strict=False):
        if not args.start <= cur.index < args.end:
            continue
        ranges = changed_offsets(prev.tx, cur.tx)
        if limit is not None:
            offs = [o for lo, hi in ranges for o in range(lo, hi + 1) if _in(limit, o)]
            ranges = _ranges(offs)
        if ranges:
            print(f"{cur.index} {cur.index / RATE:.4f}s {fmt_ranges(ranges)}")


def cmd_field(args: argparse.Namespace) -> None:
    for fr in read_log(args.log):
        if fr.index < args.start or (args.end is not None and fr.index >= args.end):
            continue
        raw = fr.tx[args.offset : args.offset + args.width]
        if len(raw) < args.width:
            continue
        value = int.from_bytes(raw, "big")
        print(f"{fr.index} {value:#0{2 + 2 * args.width}x} {fr.index / RATE:.4f}s")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("log")
    s.set_defaults(fn=cmd_summary)
    d = sub.add_parser("diff")
    d.add_argument("log")
    d.add_argument("a", type=int)
    d.add_argument("b", type=int)
    d.add_argument("--len", type=int, default=None)
    d.set_defaults(fn=cmd_diff)
    c = sub.add_parser("changes")
    c.add_argument("log")
    c.add_argument("--from", dest="start", type=int, default=0)
    c.add_argument("--to", dest="end", type=int, default=sys.maxsize)
    c.add_argument("--offsets")
    c.set_defaults(fn=cmd_changes)
    f = sub.add_parser("field")
    f.add_argument("log")
    f.add_argument("offset", type=int)
    f.add_argument("--width", type=int, choices=(1, 2, 4), default=1)
    f.add_argument("--from", dest="start", type=int, default=0)
    f.add_argument("--to", dest="end", type=int, default=None)
    f.set_defaults(fn=cmd_field)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
