#!/usr/bin/env python3
"""Print the `[[anchor_rule]]` blocks of modules/core/dynreg (the run-time registry's ColdFire sites).

    uv run python -m tools.patchkit.pk_dynreg_rules [--device dn2] [--check IMAGE ...]

The only site (Digitone II; the Digitakt II rule is written by hand) is the copy that ends the machine list getter (`cf.dynreg.list_copy`). (The machine selection view's item builder,
`cf.dynreg.msv_build`, and the three functions of the unload refusal, `cf.dynreg.project`, `kit_of` and `track_type`, were archived
with hot reload: docs/plans/digikit-hot-reload.md.) Each rule
names its function by the cfdb `h_strict` hash (address operands masked; no firmware bytes), the ordinal among functions
that share the hash, and the offset and length of the site (same scheme as tools/patchkit/pk_machine_rules.py).
Reference addresses: Digitakt II 1.17 and Digitone II 1.12 (findings 30 and 33). `--check` resolves every rule on other
images through the same cfdb query and fails when a rule does not match a function.
"""

from __future__ import annotations

import argparse
import sys

from tools.patchkit import pk_machine_rules as base

# (anchor name, byte address, length) per device, on the reference image
SITES: dict[str, tuple[str, tuple[tuple[str, int, int], ...]]] = {
    "dn2": (
        "dn2-1.12",
        (
            # the final `jsr (A3); move.l D0,(4,A2)` of the list getter: copy(static list) -> the returned vector
            ("cf.dynreg.list_copy", 0x4004D994, 6),
        ),
    ),
}


def build_rules(device: str) -> list[dict]:
    ref, sites = SITES[device]
    img = base.Img(ref)
    rules = []
    for name, addr, ln in sites:
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
    ap.add_argument("--device", default="dn2", choices=sorted(SITES))
    ap.add_argument("--check", nargs="*", default=[])
    a = ap.parse_args()
    rules = build_rules(a.device)
    errs: list[str] = []
    for o in a.check:
        errs += base.check(rules, o)
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if not a.check:
        for r in rules:
            print(
                f"# {r['name']}: {r['shared']} function(s) with the hash, ordinal {r['ordinal']}",
                file=sys.stderr,
            )
        print(base.render(rules, a.device))
    return 0


if __name__ == "__main__":
    sys.exit(main())
