#!/usr/bin/env python3
"""Dump the Digitone II parameter-page layer from an emulator snapshot (finding 02, "DN2 parameter pages").

    uv run python -m tools.coldfire.dn2_pages SNAP [--params] [--slots]

Reads (no machine): the per-type page table at 0x42433ad4 (16-byte rows {count, begin, end, cap}; types 0..4), each
page descriptor (0x2c bytes: two std::string pointers, nine parameter codes, 10 ends the list), the parameter
descriptor table T1 (0x401f83a4, 321 rows of 0x3c bytes) and the slot -> code tables built by FUN_400dc634 at
start-up (0x42c65d18 machine 5 x 40, 0x42c65cd0 filter 6 x 3, 0x42c65b3c global). Addresses are DN2 1.11/1.12.
"""

from __future__ import annotations

import argparse
import struct

from tools.emulator.snapread import Snapshot  # noqa: E402

PAGE_TABLE, T1, T1_N, T1_STRIDE = 0x42433AD4, 0x401F83A4, 0x141, 0x3C
MACH_SLOTS, FILT_SLOTS, GLOB_SLOTS = 0x42C65D18, 0x42C65CD0, 0x42C65B3C
TYPES = ["FM TONE", "WAVETONE", "FM DRUM", "SWARMER", "MIDI"]


def u32(s: Snapshot, a: int) -> int:
    v = s.u32(a)
    return 0 if v is None else v


def cstr(s: Snapshot, p: int) -> str:
    if not p:
        return "-"
    b = s.read(p, 40) or b""
    return b.split(b"\0")[0].decode("latin1")


def stdstr(s: Snapshot, p: int) -> str:
    """A COW std::string: the pointer is the character data."""
    return cstr(s, p) if p else "-"


def t1_row(s: Snapshot, code: int) -> dict:
    a = T1 + code * T1_STRIDE
    w = [u32(s, a + 4 * i) for i in range(15)]

    def sx(v):
        return v - (1 << 32) if v & 0x80000000 else v

    return dict(
        code=code,
        owner=sx(w[0]),
        slot=sx(w[1]),
        mn=sx(w[2]),
        mx=sx(w[3]),
        df=sx(w[4]),
        flag=w[5],
        s18=struct.unpack(">hh", struct.pack(">I", w[6])),
        kind=sx(w[7]),
        long=cstr(s, w[10]),
        ownername=cstr(s, w[11]),
        short=cstr(s, w[12]),
        fmt=w[13],
        w8=w[8],
        w9=w[9],
        w14=w[14],
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("snap")
    ap.add_argument("--params", action="store_true", help="dump all T1 rows")
    ap.add_argument("--slots", action="store_true", help="dump the slot -> code tables")
    a = ap.parse_args()
    s = Snapshot(a.snap)
    for t in range(6):
        r = PAGE_TABLE + 16 * t
        cnt, beg, end, cap = (u32(s, r + 4 * i) for i in range(4))
        print(
            f"type {t} {TYPES[t] if t < 5 else '(fallback?)'}: row {r:#x} count={cnt} begin={beg:#x} end={end:#x} cap={cap:#x}"
        )
        for p in range(cnt if cnt < 20 else 0):
            d = beg + 0x2C * p
            ids = [u32(s, d + 8 + 4 * i) for i in range(9)]
            print(
                f"  page {p} @{d:#x}: {stdstr(s, u32(s, d))!r}/{stdstr(s, u32(s, d + 4))!r} ids={[hex(i) for i in ids]}"
            )
            for i in ids:
                if i and i != 10 and i < T1_N:
                    q = t1_row(s, i)
                    print(
                        f"      {i:#x} owner={q['owner']} slot={q['slot']} {q['long']}/{q['short']} [{q['mn']:#x}..{q['mx']:#x}] def {q['df']:#x}"
                    )
    if a.slots:
        print("machine slot table 0x42c65d18 [type][slot-25]:")
        for t in range(5):
            row = [u32(s, MACH_SLOTS + 4 * (40 * t + k)) for k in range(40)]
            print(f"  {TYPES[t]:9s}", " ".join(f"{x:x}" for x in row))
        print("filter slot table 0x42c65cd0 [type][3]:")
        for t in range(6):
            print("  ", [hex(u32(s, FILT_SLOTS + 4 * (3 * t + k))) for k in range(3)])
    if a.params:
        for c in range(T1_N):
            q = t1_row(s, c)
            print(
                f"{c:#x} owner={q['owner']:3d} slot={q['slot']:3d} [{q['mn']:#x}..{q['mx']:#x}] def={q['df']:#x} flag={q['flag']} "
                f"w18={q['s18']} kind={q['kind']:#x} {q['ownername']}: {q['long']}/{q['short']} fmt={q['fmt']:#x}"
            )


if __name__ == "__main__":
    main()
