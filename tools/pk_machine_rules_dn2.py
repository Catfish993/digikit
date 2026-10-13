#!/usr/bin/env python3
"""Print the Digitone II `[[anchor_rule]]` blocks of modules/core/machines (the machine framework's ColdFire sites).

    uv run python tools/pk_machine_rules_dn2.py [--ref dn2-1.12] [--check dn2-1.11 ...]

The Digitone II sibling of tools/pk_machine_rules.py: the same rule kind (`cf_func`: the cfdb `h_strict` hash of the
containing function, the ordinal among functions sharing it, an offset and a length; no firmware bytes), with the DN2
1.12 site addresses of finding 02 ("core.machines on Digitone II"). `--check` resolves every rule against another image
(1.11) through the same cfdb query and fails when a rule does not match a function.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pk_machine_rules as base  # noqa: E402

# (anchor name, byte address in DN2 1.12, length). Sites that are not a whole function start at the first
# instruction the framework replays.
SITES_112: tuple[tuple[str, int, int], ...] = (
    ("cf.machines.dispatch", 0x400C25F2, 6),
    ("cf.machines.list", 0x4004D900, 16),
    ("cf.machines.list_alloc", 0x4004D8E0, 4),
    ("cf.machines.group", 0x4005927A, 6),
    ("cf.machines.name.0", 0x400DC496, 0x18),
    ("cf.machines.name.1", 0x400DC4BC, 0x18),
    ("cf.machines.name.2", 0x400DC4E2, 0x18),
    ("cf.machines.permit", 0x400DC2FE, 0x1E),
    ("cf.machines.persist", 0x400DD3EA, 2),
    # the vector-191 frame builder: move.b (0xd8c,A0),D0b; ext.w D0w (the type word of TX frame 0x94 + 2t)
    ("cf.machines.frametype", 0x4002757C, 6),
    # functions that index a per-type table with the type: the type is mapped to the clone's inside them
    ("cf.machines.typemap.0", 0x400C2636, 6),  # page count of a type
    ("cf.machines.typemap.1", 0x400C2656, 8),  # page descriptor of a type
    ("cf.machines.typemap.2", 0x400DC196, 8),  # parameter code of (parameter, type)
    # the type reader FUN_4004b7f2 returns the clone's original; `mvs.b (0xde,A0),D0; bra.b`
    ("cf.machines.reader", 0x4004B816, 6),
    ("cf.machines.reader_fn", 0x4004B7F2, 0x30),
    # FUN_4003134e ends in `jmp FUN_4004b7f2`: the machine views' and names' reader call
    ("cf.machines.reader_tail", 0x40031394, 6),
    # SoundParameterSet::vfunc_21: `mvs.b (0xde,A0),D3; move.l D2,-(SP)` (type == the parameter's owner machine)
    ("cf.machines.owner", 0x40036C24, 6),
    # callers of the reader that need the machine's own type: list stepping (2), preset (1), name of the machine (1),
    # clear track (1), sound snapshot (1), kit settings events (2), undo snapshot (1)
    ("cf.machines.reader_call.0", 0x4004D9D8, 4),
    ("cf.machines.reader_call.1", 0x4004DAA6, 4),
    ("cf.machines.reader_call.2", 0x4002DB2C, 6),
    ("cf.machines.reader_call.3", 0x40071912, 6),
    ("cf.machines.reader_call.4", 0x40071EB6, 6),
    ("cf.machines.reader_call.5", 0x4005CD62, 6),
    ("cf.machines.reader_call.6", 0x400D5BD6, 6),
    ("cf.machines.reader_call.7", 0x400D68C2, 6),
    ("cf.machines.reader_call.8", 0x400D730C, 6),
)


def build_rules(ref: str) -> list[dict]:
    img = base.Img(ref)
    rules = []
    for name, addr, ln in SITES_112:
        entry = img.function_at(addr)
        h = img.hash_of(entry)
        same = img.with_hash(h)
        rules.append(
            {
                "name": name,
                "hash": h,
                "ordinal": same.index(entry),
                "shared": len(same),
                "offset": addr - entry,
                "len": ln,
            }
        )
    return rules


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="dn2-1.12")
    ap.add_argument("--check", nargs="*", default=[])
    a = ap.parse_args()
    rules = build_rules(a.ref)
    errs: list[str] = []
    for o in a.check:
        errs += base.check(rules, o)
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if not a.check:
        print(base.render(rules, "dn2"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
