#!/usr/bin/env python3
"""List pairs of extracted instruction patterns that no fixed bit distinguishes.

Patterns are aligned at bit 47 (VISA 16/32-bit forms occupy the top of the 48-bit
frame, as the figures number them). Two patterns collide when, over the bits both
cover, every position is either a wildcard in one of them or equal in both.
Unless one is a documented special case of the other, a collision points at an
error in the source figures or in the extraction.
"""
import json
import re

figs = [f for f in json.load(open("figures.json"))["figures"] if re.search(r"Type\s*\d", f["caption"])]

def name(f):
    return re.sub(r"^Figure \d+-\d+:\s*", "", f["caption"]).replace(" Instruction", "").replace(" Opcode", "").replace(" Syntax", "")

def bits(f):
    # map absolute bit -> '0'/'1'/None(wildcard); figures for 32-bit VISA forms numbered 31..0 are rebased to 47..16
    offset = 47 - f["msb"]
    out = {}
    for i, ch in enumerate(f["pattern"]):
        out[f["msb"] - i + offset] = ch if ch in "01" else None
    return out

collisions = []
for i, a in enumerate(figs):
    for b in figs[i + 1:]:
        ba, bb = bits(a), bits(b)
        common = set(ba) & set(bb)
        if all(ba[k] is None or bb[k] is None or ba[k] == bb[k] for k in common):
            fixed_a = sum(v is not None for v in ba.values())
            fixed_b = sum(v is not None for v in bb.values())
            collisions.append((name(a), a["width"], fixed_a, name(b), b["width"], fixed_b))
for c in collisions:
    print(f"{c[0]:22s} ({c[1]}b, {c[2]} fixed)  <->  {c[3]:22s} ({c[4]}b, {c[5]} fixed)")
print(f"{len(collisions)} colliding pairs among {len(figs)} instruction figures")
