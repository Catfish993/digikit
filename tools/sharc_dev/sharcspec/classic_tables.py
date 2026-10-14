#!/usr/bin/env python3
"""Parse the instruction opcode tables of the public classic SHARC Programming Reference.

Source: Analog Devices, "SHARC Processor Programming Reference" Rev 2.4 (April 2013),
analog.com/media/en/dsp-documentation/processor-manuals/adsp-2136x_2137x_214xx_pgr_rev2.4.pdf
Chapter 10, "Instruction Set Opcodes".

Each table is a grid: a header row of bit numbers, then a value row whose cells are
bounded by vertical divider rules (thin filled rectangles). A value cell holds a
binary constant as wide as the cell (fixed bits), a field name (sometimes stacked
one letter per line), or nothing. This is the older core; SHARC+ is documented as
instruction-set compatible, so it serves as an independent second source.
"""
import json
import re
import sys

import pymupdf

PDF = "../../docs/refs/adsp-2136x_2137x_214xx_pgr_rev2.4.pdf"


def vrules(page):
    out = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "re":
                r = it[1]
                if r.width < 1.2 and r.height > 8:
                    out.append(r)
    return out


def hrules(page):
    out = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "re":
                r = it[1]
                if r.height < 1.2 and r.width > 20:
                    out.append(r)
    return out


def parse_page(page, pno):
    words = page.get_text("words")
    headings = []
    for i, w in enumerate(words):
        # Table headings are large ("Type 2a", 14.4 pt): words about 17 pt tall.
        if w[4] == "Type" and w[3] - w[1] > 14 and i + 1 < len(words) and re.fullmatch(r"\d+[a-d]", words[i + 1][4]):
            headings.append((w[1], "Type " + words[i + 1][4]))
    vr, hr = vrules(page), hrules(page)

    # Header rows: runs of numeric words on one baseline.
    rows = {}
    for w in words:
        if w[4].isdigit() and int(w[4]) <= 47:
            rows.setdefault(round(w[1]), []).append(w)
    tables = []
    for y, ws in sorted(rows.items()):
        ws.sort(key=lambda w: w[0])
        nums = [int(w[4]) for w in ws]
        if len(ws) < 3 or any(a - b != 1 for a, b in zip(nums, nums[1:])):
            continue
        top = min(w[1] for w in ws)
        bottom_header = max(w[3] for w in ws)
        # Column centres come from the header numbers; cell edges from the header dividers.
        edges = sorted({round((r.x0 + r.x1) / 2, 1) for r in vr if r.y0 <= top + 1 and r.y1 >= bottom_header - 1})
        if len(edges) < len(ws) + 1:
            continue
        # The value row ends at the next horizontal rule below the header.
        below = sorted(r.y0 for r in hr if r.y0 > bottom_header + 3 and edges[0] - 2 <= r.x0 <= edges[0] + 2)
        if not below:
            continue
        vtop, vbot = bottom_header + 2, below[0]
        mid = (vtop + vbot) / 2
        bounds = sorted({round((r.x0 + r.x1) / 2, 1) for r in vr if r.y0 <= mid <= r.y1})
        cols = []
        for w in ws:
            cx = (w[0] + w[2]) / 2
            left = max((e for e in edges if e < cx), default=None)
            right = min((e for e in edges if e > cx), default=None)
            cols.append((int(w[4]), left, right))
        # Group header columns into value cells split by the tall dividers.
        cells, cur = [], []
        for bit, left, right in cols:
            if cur and any(abs(left - b) < 1.0 for b in bounds):
                cells.append(cur)
                cur = []
            cur.append((bit, left, right))
        if cur:
            cells.append(cur)
        entries = []
        for cell in cells:
            x0, x1 = cell[0][1], cell[-1][2]
            inside = sorted((w for w in words if vtop <= w[1] and w[3] <= vbot and x0 <= (w[0] + w[2]) / 2 <= x1), key=lambda w: (round(w[1]), w[0]))
            # Stacked single letters read top to bottom; words on one line left to right.
            text = "".join(w[4] for w in inside) if inside and all(len(w[4]) == 1 for w in inside) else " ".join(w[4] for w in inside)
            hi, lo = cell[0][0], cell[-1][0]
            if text and re.fullmatch(r"[01]+", text) and len(text) == hi - lo + 1:
                kind = "fixed"
            elif not text:
                kind = "blank"
            else:
                kind = "field"
            entries.append({"bits": [hi, lo], "kind": kind, "text": text})
        name = max((h for h in headings if h[0] < top), default=(None, None))[1]
        tables.append({"page": pno, "y": top, "type": name, "cells": entries})
    return tables


def main():
    doc = pymupdf.open(PDF)
    tables = []
    for pno in range(440, 480):
        tables += parse_page(doc[pno - 1], pno)
    # A type heading can be followed by several tables (e.g. Type 8a absolute and
    # PC-relative). Each table's first header row starts at bit 47; later rows continue it.
    variants = []
    for t in tables:
        if not t["type"]:
            continue
        first_bit = t["cells"][0]["bits"][0]
        if first_bit == 47 or not variants or variants[-1]["type"] != t["type"]:
            variants.append({"type": t["type"], "page": t["page"], "cells": []})
        variants[-1]["cells"].extend(t["cells"])
    out = {}
    for v in variants:
        name = v["type"]
        n = sum(1 for k in out if k == name or k.startswith(name + " #"))
        key = name if n == 0 else f"{name} #{n + 1}"
        cells = sorted(v["cells"], key=lambda c: -c["bits"][0])
        msb, lsb = cells[0]["bits"][0], cells[-1]["bits"][1]
        pattern = ""
        for c in cells:
            hi, lo = c["bits"]
            width = hi - lo + 1
            pattern += c["text"] if c["kind"] == "fixed" else ("." * width if c["kind"] == "blank" else "x" * width)
        if len(pattern) != msb - lsb + 1:
            print(f"warning: {key} pattern length {len(pattern)} != {msb - lsb + 1}")
        out[key] = {"page": v["page"], "msb": msb, "lsb": lsb, "pattern": pattern, "cells": cells}
    json.dump(out, open("classic.json", "w"), indent=1)
    for name, t in out.items():
        print(f"{name:10s} {t['msb']:2d}..{t['lsb']:<2d} {t['pattern']}  " + " ".join(f"{c['text']}[{c['bits'][0]}:{c['bits'][1]}]" for c in t["cells"] if c["kind"] == "field"))


if __name__ == "__main__":
    main()
