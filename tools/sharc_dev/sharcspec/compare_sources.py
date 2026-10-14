#!/usr/bin/env python3
"""Compare SHARC+ PRM figure extractions (figures.json) with classic PGR tables (classic.json).

For each instruction present in both, every bit from 47 down is classified:
  same-fixed   both give the same constant
  VALUE-CONFLICT  both give a constant and they differ
  prm-fixed/classic-field, prm-field/classic-fixed, prm-fixed/classic-blank, ...
Classic types that the PRM draws as one figure with a selector field (e.g. Type 8a
absolute/relative -> field "r") are compared variant by variant, so the selector bit
shows as prm-field/classic-fixed with the variant's value.
"""
import json
import re

prm = json.load(open("figures.json"))["figures"]
classic = json.load(open("classic.json"))

# PRM figure name -> classic table keys drawn by that figure.
PAIRS = {
    "Type1a": ["Type 1a"], "Type1b": ["Type 1b"], "Type2a": ["Type 2a"], "Type2b": ["Type 2b"], "Type2c": ["Type 2c"],
    "Type3a": ["Type 3a"], "Type3b": ["Type 3b"], "Type3c": ["Type 3c"], "Type4a": ["Type 4a"], "Type4b": ["Type 4b"],
    "Type5a_move": ["Type 5a"], "Type5a (swap)": ["Type 5a #2"], "Type5b (move)": ["Type 5b"], "Type5b (swap)": ["Type 5b #2"],
    "Type6a (mem)": ["Type 6a"], "Type6a (nomem)": ["Type 6a #2"], "Type7a": ["Type 7a"], "Type7b": ["Type 7b"],
    "Type8a": ["Type 8a", "Type 8a #2"], "Type9a": ["Type 9a", "Type 9a #2"], "Type9b": ["Type 9b", "Type 9b #2"],
    "Type10a": ["Type 10a", "Type 10a #2"], "Type11a": ["Type 11a", "Type 11a #2"], "Type11c": ["Type 11c", "Type 11c #2"],
    "Type12a_imm": ["Type 12a"], "Type13a": ["Type 13a"], "Type14a": ["Type 14a"], "Type15a": ["Type 15a"], "Type15b": ["Type 15b"],
    "Type16a": ["Type 16a"], "Type16b": ["Type 16b"], "Type17a": ["Type 17a"], "Type17b": ["Type 17b"], "Type18a": ["Type 18a"],
    "Type19a": ["Type 19a"], "Type19a_bitrev": ["Type 19a #2"], "Type20a": ["Type 20a"], "Type21a": ["Type 21a"], "Type21c": ["Type 21c"],
    "Type22c": ["Type 22c"], "Type25a_direct": ["Type 25a"], "Type25a_pcrel": ["Type 25a #2"], "Type25c_rframe": ["Type 25c"],
}


def prm_name(f):
    return re.sub(r"^Figure \d+-\d+:\s*", "", f["caption"]).replace(" Instruction", "").replace(" Opcode", "").replace(" Syntax", "").strip()


def rebased(pattern, msb):
    """bit -> char, with 16/32-bit forms numbered 31..0 or 15..0 moved to the top of the 48-bit frame."""
    off = 47 - msb
    return {msb - i + off: ch for i, ch in enumerate(pattern)}


report = []
for f in prm:
    name = prm_name(f)
    if name not in PAIRS:
        continue
    pb = rebased(f["pattern"], f["msb"])
    for key in PAIRS[name]:
        c = classic.get(key)
        if not c:
            report.append((name, key, "missing in classic", []))
            continue
        cb = rebased(c["pattern"], c["msb"])
        conflicts, notes = [], {}
        for bit in sorted(set(pb) & set(cb), reverse=True):
            p, q = pb[bit], cb[bit]
            pk = "fixed" if p in "01" else {"x": "field", "-": "unused", "?": "unmarked"}[p]
            qk = "fixed" if q in "01" else {"x": "field", ".": "blank"}[q]
            if pk == qk == "fixed":
                if p != q:
                    conflicts.append(bit)
                continue
            if pk == qk:
                continue
            notes.setdefault(f"prm-{pk}/classic-{qk}", []).append(bit)
        prm_vals = "".join(pb[b] for b in conflicts)
        cls_vals = "".join(cb[b] for b in conflicts)
        report.append((name, key, conflicts, notes, prm_vals, cls_vals))

agree = 0
for r in report:
    if len(r) == 4:
        print(f"{r[0]:18s} {r[1]:12s} {r[2]}")
        continue
    name, key, conflicts, notes, pv, cv = r
    if not conflicts and not notes:
        agree += 1
        print(f"{name:18s} {key:12s} identical")
        continue
    parts = []
    if conflicts:
        parts.append(f"VALUE-CONFLICT bits {conflicts[0]}..{conflicts[-1]} ({len(conflicts)}): prm {pv} vs classic {cv}")
    for k, bits in notes.items():
        parts.append(f"{k} {bits[0]}..{bits[-1]} ({len(bits)})")
    print(f"{name:18s} {key:12s} " + "; ".join(parts))
print(f"\n{agree} of {len(report)} comparisons identical")
