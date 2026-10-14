#!/usr/bin/env python3
"""Extract instruction bit-layout figures from the public SHARC+ Core Programming Reference.

Source: Analog Devices, "SHARC+ Core Programming Reference", Rev 1.5 (June 2023),
analog.com/media/en/dsp-documentation/processor-manuals/sc58x-2158x-prm.pdf

The opcode figures are vector drawings, so the layout is recovered from geometry
rather than from extracted text:
  * a row is a stroked rectangle of 9 pt cells, with bit numbers printed above;
  * a gray-filled rectangle covers fixed opcode bits (their values are printed in
    the cells); a yellow-filled rectangle covers unused bits -- in the instruction
    figures (ch. 14-17) bits the instruction does not use, in the compute-field
    figures (ch. 18) bits outside the field being described (e.g. the Type 2c
    prefix around ShortCompute);
  * digits printed in white cells are placeholders and are ignored;
  * a field is a 3-segment bracket under a run of cells, joined by a leader path
    to its label (e.g. "compute[22:16]"); a single-bit field has a leader only.

Every extracted figure is cross-checked (bits accounted for exactly once, label
widths match bracket widths) and anything that fails is reported, not guessed.
"""
import json
import re
import sys
from dataclasses import dataclass, field

import pymupdf

CELL = 9.0
TOL = 1.2
GRAY = (0.82, 0.82, 0.82)
YELLOW = (0.95, 0.80, 0.19)
LABEL_RE = re.compile(r"^(?P<name>[A-Za-z_][\w]*)(\[(?P<hi>\d+)(:(?P<lo>\d+))?\]?)?$")  # tolerates a missing "]" typo


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def color_is(c, ref, tol=0.03):
    return c is not None and all(abs(x - y) <= tol for x, y in zip(c, ref))


@dataclass
class Row:
    x0: float
    y0: float
    x1: float
    y1: float
    bits: list = field(default_factory=list)  # bit number per cell, left to right

    @property
    def ncells(self):
        return round((self.x1 - self.x0) / CELL)

    @property
    def pitch(self):
        # Some rows are drawn a fraction of a point wider than 9 pt x cells.
        return (self.x1 - self.x0) / self.ncells

    def cell_center(self, i):
        return self.x0 + self.pitch * i + self.pitch / 2

    def cell_at(self, x):
        i = int((x - self.x0) // self.pitch)
        return i if 0 <= i < self.ncells else None


def spans_of(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip():
                    out.append(s)
    return out


def paths_of(page):
    """Yield (kind, fill, points) for each drawing; points is a list of segments."""
    for d in page.get_drawings():
        segs, rects = [], []
        for it in d["items"]:
            if it[0] == "l":
                segs.append((it[1], it[2]))
            elif it[0] == "re":
                rects.append(it[1])
        yield d, segs, rects


def connected_polyline(segs):
    """Return ordered points if segments form one connected chain, else None."""
    if not segs:
        return None
    pts = [segs[0][0], segs[0][1]]
    for a, b in segs[1:]:
        if near(a.x, pts[-1].x) and near(a.y, pts[-1].y):
            pts.append(b)
        else:
            return None
    return pts


def extract_page(page, pno):
    spans = spans_of(page)
    bitlabels = [s for s in spans if "CourierNewPS-BoldMT" in s["font"] and s["size"] < 5 and s["text"].strip().isdigit()]
    cell_digits = [s for s in spans if s["font"] == "CourierNewPSMT" and s["text"].strip() in ("0", "1")]
    if not bitlabels:
        return []

    rows, fills, polylines = [], [], []
    for d, segs, rects in paths_of(page):
        if d["type"] in ("f", "fs"):
            for r in rects:
                fills.append((d.get("fill"), r))
            # Some fills are closed polygons of axis-aligned lines rather than rectangles.
            if not rects and segs and all(near(a.x, b.x, 0.05) or near(a.y, b.y, 0.05) for a, b in segs):
                fills.append((d.get("fill"), d["rect"]))
        if d["type"] in ("s", "fs"):
            for r in rects:
                if near(r.height, CELL, 0.6) and r.width >= CELL - 0.5 and near(r.width / CELL, round(r.width / CELL), 0.12):
                    rows.append(Row(r.x0, r.y0, r.x1, r.y1))
            pl = connected_polyline(segs)
            if pl and len(pl) >= 3:
                polylines.append(pl)

    for row in rows:
        n = row.ncells
        labels = [s for s in bitlabels if near(s["bbox"][3], row.y0, 2.5) and row.x0 - 1 <= (s["bbox"][0] + s["bbox"][2]) / 2 <= row.x1 + 1]
        bits = [None] * n
        for s in labels:
            i = row.cell_at((s["bbox"][0] + s["bbox"][2]) / 2)
            if i is not None:
                bits[i] = int(s["text"])
        row.bits = bits
    rows = [r for r in rows if r.bits and all(b is not None for b in r.bits)]
    if not rows:
        return []

    results = []
    for row in rows:
        cells = []
        for i, bit in enumerate(row.bits):
            cx, cy = row.cell_center(i), (row.y0 + row.y1) / 2
            kind = "unassigned"
            for fill, r in fills:
                if r.x0 - 0.5 <= cx <= r.x1 + 0.5 and r.y0 - 0.5 <= cy <= r.y1 + 0.5:
                    if color_is(fill, GRAY):
                        kind = "fixed"
                    elif color_is(fill, YELLOW):
                        kind = "unused"
                    elif not color_is(fill, (1, 1, 1)):
                        kind = f"fill{tuple(round(c, 2) for c in fill)}"
            digit = None
            for s in cell_digits:
                sx, sy = (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
                if row.x0 + row.pitch * i <= sx <= row.x0 + row.pitch * (i + 1) and row.y0 <= sy <= row.y1:
                    digit = int(s["text"])
            cells.append({"bit": bit, "kind": kind, "value": digit if kind in ("fixed", "unused") else None})
        results.append({"row": row, "cells": cells})

    # Field annotations. Every stroked line segment outside the rows is grouped into
    # connected components (shared endpoints or T-junctions). A component that ends
    # beside a label and touches cells just below a row assigns that label to the
    # cells between its outermost contacts (a bracket touches both end cells, a
    # single-bit leader touches one).
    segs = []
    for d, dsegs, _ in paths_of(page):
        if d["type"] != "s":
            continue
        for a, b in dsegs:
            inside = any(max(a.y, b.y) <= rr["row"].y1 + 0.6 and min(a.y, b.y) >= rr["row"].y0 - 8
                         and rr["row"].x0 - 1 <= min(a.x, b.x) and max(a.x, b.x) <= rr["row"].x1 + 1 for rr in results)
            if not inside:
                segs.append((a, b))

    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def on_segment(p, a, b, tol=1.0):
        if min(a.x, b.x) - tol <= p.x <= max(a.x, b.x) + tol and min(a.y, b.y) - tol <= p.y <= max(a.y, b.y) + tol:
            dx, dy = b.x - a.x, b.y - a.y
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            return abs(dy * (p.x - a.x) - dx * (p.y - a.y)) / length <= tol
        return False

    for i, (a, b) in enumerate(segs):
        for j in range(i + 1, len(segs)):
            c, e = segs[j]
            if on_segment(a, c, e) or on_segment(b, c, e) or on_segment(c, a, b) or on_segment(e, a, b):
                parent[find(i)] = find(j)

    comps = {}
    for i, seg in enumerate(segs):
        comps.setdefault(find(i), []).append(seg)

    labels = [s for s in spans if "CourierNewPS-BoldMT" in s["font"] and 5 <= s["size"] < 7]
    anomalies = []

    def label_at(pt):
        for s in labels:
            ly = (s["bbox"][1] + s["bbox"][3]) / 2
            if near(ly, pt.y, 3) and (0 < s["bbox"][0] - pt.x < 16 or 0 < pt.x - s["bbox"][2] < 16):
                return s["text"].strip()
        return None

    for comp in comps.values():
        ends = [p for seg in comp for p in seg]
        labs = {label_at(p) for p in ends} - {None}
        contacts = []
        for p in ends:
            for k, rr in enumerate(results):
                row = rr["row"]
                if 0 < p.y - row.y1 < 7:
                    i = row.cell_at(p.x)
                    # Ticks are usually centred; a few are drawn up to ~4 pt off centre.
                    if i is not None and near(p.x, row.cell_center(i), 4.0):
                        contacts.append((k, i))
        if not labs and not contacts:
            continue
        if len(labs) != 1:
            anomalies.append(f"p{pno}: component with labels {sorted(labs)} and {len(contacts)} contacts")
            continue
        lab = labs.pop()
        rows_hit = {k for k, _ in contacts}
        if len(rows_hit) != 1:
            anomalies.append(f"p{pno}: label {lab!r} touches {len(rows_hit)} rows")
            continue
        k = rows_hit.pop()
        idx = [i for _, i in contacts]
        rr = results[k]
        for i in range(min(idx), max(idx) + 1):
            c = rr["cells"][i]
            if c["kind"] == "field":
                anomalies.append(f"p{pno}: bit {c['bit']} claimed by {c['field']!r} and {lab!r}")
            c["kind"], c["field"] = "field", lab
    return results, anomalies


def figure_tags(page):
    """Small bold Helvetica tag printed at the top left of each figure, e.g. 'Type2b'."""
    return [(s["bbox"][1], s["text"].strip()) for s in spans_of(page) if s["font"] == "Helvetica-Bold" and 6 <= s["size"] <= 8]


def group_figures(page, pno, results):
    """Attach rows to the caption that follows them. Rows after the last caption on a
    page are returned separately: the figure continues on the next page."""
    spans = spans_of(page)
    captions = []
    for i, s in enumerate(spans):
        m = re.match(r"Figure (\d+)-(\d+):", s["text"].strip())
        if m:
            title = s["text"].strip()
            if i + 1 < len(spans) and near(spans[i + 1]["bbox"][1], s["bbox"][1], 2):
                title += spans[i + 1]["text"]
            captions.append((s["bbox"][1], title.strip()))
    captions.sort()
    tags = figure_tags(page)
    figs, trailing = {}, []
    for rr in results:
        y = rr["row"].y0
        cap = next((c for c in captions if c[0] > y), None)
        if cap is None:
            trailing.append(rr)
            continue
        prev = max((c[0] for c in captions if c[0] < cap[0]), default=0)
        tag = next((t for ty, t in tags if prev < ty < cap[0]), None)
        figs.setdefault(cap[1], {"rows": [], "tag": tag, "page": pno})["rows"].append(rr)
    return figs, trailing, tags


def summarize(pno, caption, rows, tag=None):
    rows = sorted(rows, key=lambda r: (r["row"].y0, r["row"].x0))
    bits, fields, problems, unspecified, unused_values = {}, {}, [], [], {}
    for rr in rows:
        for c in rr["cells"]:
            if c["kind"] == "unused":
                unused_values[c["bit"]] = c["value"]
            if c["bit"] in bits:
                problems.append(f"bit {c['bit']} appears twice")
            bits[c["bit"]] = c
            if c["kind"] == "field":
                fields.setdefault(c["field"], []).append(c["bit"])
    hi, lo = max(bits), min(bits)
    pattern = ""
    for b in range(hi, lo - 1, -1):
        c = bits.get(b)
        if c is None:
            pattern += "_"
            problems.append(f"bit {b} missing")
        elif c["kind"] == "fixed":
            if c["value"] is None:
                problems.append(f"fixed bit {b} has no printed value")
            pattern += str(c["value"])
        elif c["kind"] == "field":
            pattern += "x"
        elif c["kind"] == "unused":
            pattern += "-"
        elif c["kind"] == "unassigned":
            pattern += "?"
            unspecified.append(b)
        else:
            pattern += "?"
            problems.append(f"bit {b} is {c['kind']}")
    flist = []
    for label, fb in fields.items():
        fb = sorted(fb, reverse=True)
        m = LABEL_RE.match(label)
        entry = {"label": label, "bits": [fb[0], fb[-1]] if len(fb) > 1 else [fb[0]]}
        if fb != list(range(fb[0], fb[0] - len(fb), -1)):
            problems.append(f"field {label!r} is not contiguous: {fb}")
        if m:
            entry["name"] = m.group("name")
            if m.group("hi") is not None:
                fhi = int(m.group("hi"))
                flo = int(m.group("lo")) if m.group("lo") is not None else fhi
                entry["value_bits"] = [fhi, flo]
                if fhi - flo + 1 != len(fb):
                    problems.append(f"field {label!r} labels {fhi - flo + 1} bits but spans {len(fb)}")
        else:
            problems.append(f"unparsed label {label!r}")
        flist.append(entry)
    if unspecified:
        problems.append(f"bits {unspecified[0]}..{unspecified[-1]} ({len(unspecified)}) are white with no field label; check the text")
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    cap_title = re.sub(r"^Figure \d+-\d+:\s*", "", caption)
    if tag and caption.startswith("Figure") and not norm(cap_title).startswith(norm(tag)):
        problems.append(f"figure tag {tag!r} does not match caption {cap_title!r}")
    return {
        "page": pno,
        "caption": caption,
        "msb": hi,
        "lsb": lo,
        "width": hi - lo + 1,
        "pattern": pattern,
        "fields": flist,
        "unspecified_bits": unspecified,
        "unused_printed": "".join(str(unused_values[b]) for b in sorted(unused_values, reverse=True)),
        "tag": tag,
        "problems": problems,
    }


def main(pdf, first, last, out):
    doc = pymupdf.open(pdf)
    figures, anomalies = [], []
    carried, carried_tag = [], None
    for pno in range(first, last + 1):
        page = doc[pno - 1]
        got = extract_page(page, pno)
        if not got:
            continue
        results, anom = got
        anomalies += anom
        figs, trailing, tags = group_figures(page, pno, results)
        if carried:
            if figs:
                first_cap = min(figs, key=lambda c: min(rr["row"].y0 for rr in figs[c]["rows"]))
                figs[first_cap]["rows"] = carried + figs[first_cap]["rows"]
                figs[first_cap]["tag"] = figs[first_cap]["tag"] or carried_tag
                figs[first_cap]["page"] = f"{pno - 1}-{pno}"
            else:
                anomalies.append(f"p{pno - 1}: rows continue past page {pno} without a caption")
            carried = []
        for caption, f in figs.items():
            figures.append(summarize(f["page"], caption, f["rows"], f["tag"]))
        if trailing:
            carried = trailing
            carried_tag = next((t for ty, t in sorted(tags, reverse=True)), None)
    legend = {"0/1": "fixed bit (gray cell)", "x": "field bit (see fields)", "-": "unused bit (yellow cell)", "?": "white cell with no field label"}
    json.dump({"source": "SHARC+ Core Programming Reference Rev 1.5 (sc58x-2158x-prm.pdf)", "pattern_legend": legend, "figures": figures, "anomalies": anomalies}, open(out, "w"), indent=1)
    bad = [f for f in figures if f["problems"]]
    print(f"{len(figures)} figures, {len(bad)} with problems, {len(anomalies)} attachment anomalies -> {out}")


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else "../../docs/refs/sc58x-2158x-prm.pdf"
    main(pdf, 301, 563, "figures.json")
