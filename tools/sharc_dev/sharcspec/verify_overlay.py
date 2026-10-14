#!/usr/bin/env python3
"""Overlay extracted encodings on the PRM figures and tile them into contact sheets.

Under every bit row, the extracted classification is printed in red, one character
per cell: 0/1 = fixed bit, a letter = field (legend printed above the figure),
- = unused (yellow), ? = white cell with no field. Comparing the red line with ADI's own
drawing is the visual check for each figure.
"""
import string
import sys

import pymupdf

from tools.sharc_dev.sharcspec import extract_figures as ef

PDF = "../../docs/refs/sc58x-2158x-prm.pdf"
RED = (0.85, 0, 0)


def main(first=301, last=563, per_sheet=4):
    doc = pymupdf.open(PDF)
    clips = []  # (page index, clip rect, title)
    carried = []
    for pno in range(first, last + 1):
        page = doc[pno - 1]
        got = ef.extract_page(page, pno)
        if not got:
            continue
        results, _ = got
        figs, trailing, _ = ef.group_figures(page, pno, results)
        groups = list(figs.items())
        if trailing:
            groups.append((f"(continues on p{pno + 1})", {"rows": trailing}))
        for caption, f in groups:
            rows = f["rows"]
            codes = {}
            for rr in rows:
                row = rr["row"]
                line = []
                for c in rr["cells"]:
                    if c["kind"] == "fixed":
                        line.append(str(c["value"]))
                    elif c["kind"] == "field":
                        if c["field"] not in codes:
                            codes[c["field"]] = string.ascii_letters[len(codes)]
                        line.append(codes[c["field"]])
                    elif c["kind"] == "unused":
                        line.append("-")
                    else:
                        line.append("?")
                for i, ch in enumerate(line):
                    page.insert_text((row.cell_center(i) - 1.8, row.y1 + 6.5), ch, fontsize=6.5, fontname="cour", color=RED)
            top = min(rr["row"].y0 for rr in rows)
            bottom = max(rr["row"].y1 for rr in rows)
            legend = "  ".join(f"{v}={k}" for k, v in codes.items())
            page.insert_text((62, top - 18), f"p{pno} {caption[:40]}", fontsize=6.5, fontname="helv", color=RED)
            for n, chunk in enumerate(range(0, len(legend), 110)):
                page.insert_text((62, top - 11 + 6 * n), legend[chunk:chunk + 110], fontsize=5.5, fontname="cour", color=RED)
            clips.append((pno - 1, pymupdf.Rect(55, top - 28, 590, bottom + 48)))

    sheets = pymupdf.open()
    for s in range(0, len(clips), per_sheet):
        batch = clips[s:s + per_sheet]
        height = sum(c.height for _, c in batch)
        out = sheets.new_page(width=535, height=height)
        y = 0
        for pidx, clip in batch:
            out.show_pdf_page(pymupdf.Rect(0, y, 535, y + clip.height), doc, pidx, clip=clip)
            y += clip.height
    for i, page in enumerate(sheets):
        page.get_pixmap(dpi=150).save(f"renders/sheet_{i:02d}.png")
    print(f"{len(clips)} figure crops on {len(sheets)} sheets")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
