#!/usr/bin/env python3
"""Classify an address-level DSP profile (tools/devtools/sharc_prof_addrs.py ->
X.xml.lines.json) by what the source line is: JIT kernels, memory, address
map checks, mask (V{b,m}) handling, flags, loop/stack bookkeeping, undo and
budget, float arithmetic, and the rest.

    uv run python -m tools.devtools.sharc_prof_classify X.xml.lines.json [--unknown N]
"""

import argparse
import collections
import glob
import json
import re

RULES = [
    ("memory (page lookup, bitmaps, read/write, watch)", r"fast_read|fast_write|plain_read|plain_address|note_write|self\.eff\[|\.present\[|\.dirty\[|self\.watch|_dm_(read|write)|load_normal_ureg|access_address|s\.mem\.|canonical|_dm_write_nolog"),
    ("address-map checks (normal_word_to_byte, access scale)", r"normal_word_to_byte|_access_modifier_scale|concrete|0x90000|589824|402653184|VI::|contains\(&address"),
    ("flags (ASTATX/sticky)", r"astatx|_sticky|flag_update|define_mask|forget|_apply_flag|PartialConst"),
    ("float arithmetic", r"is_nan|is_infinite|f32::|_float_|f32\.rs|f64"),
    ("loop/stack/pc bookkeeping", r"_sync_empty_loop|stk_at|loop_depth|loop_slots|call_stack|pc_stack|pending|_advance|\.loops|bank_|stack_model"),
    ("undo/budget/rf copies (region own)", r"rf\.o\[|\bsv\d+|s\.log\(|Undo|UNDO_CAP|short|s\.limit|ic \+= |rf\.pc|Rf::default|rf\.r\[\d+\] = "),
    ("mask ops (V{b,m} construct/check)", r"\.is_c\(\)|\.m\b|V \{|V::c|_add\(|_field|\.b\b|is_some\(\)|unwrap|Some\(|Err\(|\?;"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lines_json")
    ap.add_argument("--unknown", type=int, default=0)
    a = ap.parse_args()
    d = json.load(open(a.lines_json))
    cache: dict[str, list[str] | None] = {}

    def src(f: str) -> list[str] | None:
        if f not in cache:
            c = glob.glob("out/native/dn2-audio/gen/" + f) + glob.glob("native/**/" + f, recursive=True)
            cache[f] = open(c[0], errors="replace").read().splitlines() if c else None
        return cache[f]

    tot = sum(n for _, _, n in d)
    cat: collections.Counter = collections.Counter()
    unk: collections.Counter = collections.Counter()
    for f, ln, n in d:
        if f == "?":
            cat["JIT kernels (cranelift)"] += n
            continue
        lines = src(f)
        text = lines[ln - 1].strip() if lines and 0 < ln <= len(lines) else ""
        if f.startswith("mem.rs"):
            cat[RULES[0][0]] += n
            continue
        for name, rx in RULES:
            if re.search(rx, text):
                cat[name] += n
                break
        else:
            if ln == 0 or not text:
                cat["no line (compiler glue, moves)"] += n
            elif f.startswith(("blocks_", "spec_")):
                cat["AOT other statements"] += n
                unk[(f, ln, text[:90])] += n
            else:
                cat["other (std, engine)"] += n
    for c, n in cat.most_common():
        print(f"{100 * n / tot:5.1f}%  {c}")
    for (f, ln, t), n in unk.most_common(a.unknown):
        print(f"   {100 * n / tot:4.1f}% {f}:{ln} {t}")


if __name__ == "__main__":
    main()
