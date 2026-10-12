#!/usr/bin/env python3
"""Decode a DIGI_PANEL_TX_LOG capture (UART8 TX: OLED and LED bytes, one `icount hex` line per drain).

    uv run python tools/panel_tx.py LOG [--base ICOUNT] [--leds]

Records (framing from the enqueue callers of 0x4000220c on dt2 1.16): 0x10|page, column, 8 data bytes (OLED
page row); 0xb8 (present/swap); 0xb9 idx val (LED set); 0xb5 idx r g b; 0xb7 x; 0xbb a b c; 0xbc x; 0x40|n 0.
"""

import argparse

LEN = {0xB8: 0, 0xB9: 2, 0xB5: 4, 0xB7: 1, 0xBB: 3, 0xBC: 1}


def load(path):
    out = []
    for line in open(path):
        t, h = line.split()
        out += [(int(t), b) for b in bytes.fromhex(h)]
    return out


def parse(stream):
    i, recs = 0, []
    while i < len(stream):
        t, c = stream[i]
        n = 9 if 0x10 <= c <= 0x17 else LEN.get(c, 1 if 0x40 <= c <= 0x4F else None)
        if n is None:
            recs.append((t, None, bytes([c])))
            i += 1
            continue
        recs.append((t, c, bytes(b for _, b in stream[i + 1 : i + 1 + n])))
        i += 1 + n
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--base", type=float, default=0.0)
    ap.add_argument("--leds", action="store_true", help="only LED records")
    a = ap.parse_args()
    for t, c, d in parse(load(a.log)):
        if a.leds and c not in (0xB9, 0xB5):
            continue
        print(f"{(t - a.base) / 1e6:9.2f}M", "??" if c is None else f"{c:02x}", d.hex(" "))


if __name__ == "__main__":
    main()
