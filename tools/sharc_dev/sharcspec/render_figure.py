#!/usr/bin/env python3
"""Render the area around the opcode rows on a PRM page, for visual checking."""
import sys
import pymupdf
from tools.sharc_dev.sharcspec import extract_figures as ef

def render(doc, pno, out, dpi=170):
    page = doc[pno - 1]
    got = ef.extract_page(page, pno)
    rows = [rr["row"] for rr in got[0]] if got else []
    if not rows:
        return False
    y0 = min(r.y0 for r in rows) - 30
    y1 = max(r.y1 for r in rows) + 50
    page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(60, y0, 590, y1)).save(out)
    return True

if __name__ == "__main__":
    doc = pymupdf.open("../../docs/refs/sc58x-2158x-prm.pdf")
    for p in sys.argv[1:]:
        print(p, render(doc, int(p), f"renders/fig_p{p}.png"))
