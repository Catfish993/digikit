#!/usr/bin/env python3
"""Flag figures whose fixed-bit digits look like unedited template defaults.

The PRM figures are built from a template whose cells print default digits: one
row style reads 0010000000000000 (as in Type 1a), the other 1100000000000000, and
either can appear on any row. Where every fixed bit of a row equals that default, the digits
may never have been edited: the gray shading (which bits are fixed) is still
evidence, the values are not. Classic-table comparisons confirmed this failure
for Types 2a, 2b, 11c and 17b.
"""
import json
import re

TEMPLATE_TOP = "0010000000000000"
TEMPLATE_REST = "1100000000000000"
classic_covered = {"Type1a", "Type1b", "Type2a", "Type2b", "Type2c", "Type3a", "Type3b", "Type3c", "Type4a", "Type4b",
                   "Type5a_move", "Type5a (swap)", "Type5b (move)", "Type5b (swap)", "Type6a (mem)", "Type6a (nomem)",
                   "Type7a", "Type7b", "Type8a", "Type9a", "Type9b", "Type10a", "Type11a", "Type11c", "Type12a_imm",
                   "Type13a", "Type14a", "Type15a", "Type15b", "Type16a", "Type16b", "Type17a", "Type17b", "Type18a",
                   "Type19a", "Type19a_bitrev", "Type20a", "Type21a", "Type21c", "Type22c", "Type25a_direct",
                   "Type25a_pcrel", "Type25c_rframe"}

for f in json.load(open("figures.json"))["figures"]:
    name = re.sub(r"^Figure \d+-\d+:\s*", "", f["caption"]).replace(" Instruction", "").replace(" Opcode", "").replace(" Syntax", "").strip()
    if not name.startswith("Type"):
        continue
    verdicts = []
    for r in range(0, f["width"], 16):
        row = f["pattern"][r:r + 16]
        fixed_pos = [i for i, c in enumerate(row) if c in "01"]
        if fixed_pos:
            # Either template can appear on any row (16/32-bit figures start with the 1100 row).
            same = any(all(row[i] == t[i] for i in fixed_pos) for t in (TEMPLATE_TOP, TEMPLATE_REST))
            verdicts.append("TEMPLATE" if same and len(fixed_pos) >= 2 else "edited")
    source = "classic covers" if name in classic_covered else "SHARC+ only"
    flag = "  <-- digits unconfirmed" if "TEMPLATE" in verdicts and source == "SHARC+ only" else ""
    print(f"{name:18s} {source:15s} rows: {' '.join(verdicts) or '-'}{flag}")
