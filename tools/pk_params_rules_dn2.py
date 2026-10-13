#!/usr/bin/env python3
"""Print the Digitone II `[[anchor_rule]]` blocks a core/params + own-parameter-pages framework needs (finding 02,
"DN2 parameter pages").

    uv run python tools/pk_params_rules_dn2.py [--ref dn2-1.12] [--check dn2-1.11 ...]

Three rule families:
  * `cf_params_t1` and `cf_params_sites` (existing kinds of tools/port_resolve.py; the reader signature and the
    scan are the Digitakt's, they resolve unchanged on the Digitone II: table 0x401f83a4, 321 rows, 56 references and
    55 clamp bounds in 1.12);
  * `cf_func` rules for the sites of the DN2 parameter-page layer that core/machines does not already anchor: the
    value-class accessor (T2, `cmpi.l #0x141,D0`), and the three per-type table functions whose entry
    sites are the `cf.machines.typemap.0..2` anchors (they stay the shim points of the own-pages hooks);
  * `--check IMAGE` resolves every rule on another image (1.11) through the same cfdb query and the T1 scan.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pk_machine_rules as base  # noqa: E402
import port_resolve as pr  # noqa: E402

# (anchor name, byte address in DN2 1.12, length)
SITES_112: tuple[tuple[str, int, int], ...] = (
    # FUN_400c257c(code): `move.l 4(sp),D0; cmpi.l #0x141,D0; scs D1b; ...; addi.l #0x4243425c,D0`: T2 object of a code
    ("cf.params.t2", 0x400C257C, 10),
    # FUN_400c25a0(code, ..): calls T1[code] + 0x34 (the formatter) with (arg, 0x4243398c); a user of the same bound
    ("cf.params.fmt", 0x400C25A0, 10),
    # FUN_400dc634: the start-up builder of the slot -> code tables (`clr` of six bss tables, then a loop over 0x141 rows)
    ("cf.params.slotbuild", 0x400DC634, 4),
)

T1_SIGNATURE_ROWS = 0x141


def rules_for(ref: str) -> list[dict]:
    img = base.Img(ref)
    out = []
    for name, addr, ln in SITES_112:
        entry = img.function_at(addr)
        h = img.hash_of(entry)
        same = img.with_hash(h)
        out.append(
            {
                "name": name,
                "hash": h,
                "ordinal": same.index(entry),
                "shared": len(same),
                "offset": addr - entry,
                "len": ln,
            }
        )
    return out


def check_image(rules: list[dict], image: str) -> list[str]:
    errs = base.check(rules, image)
    syx = {"dn2-1.11": "Digitone_II_OS1.11.syx", "dn2-1.12": "Digitone_II_OS1.12.syx"}[
        image
    ]
    main = pr.sections_of(ROOT / syx)[3]

    class F:  # the two members the T1 helpers use
        pass

    cf = F()
    cf.main, cf.base = main, 0x40000400  # type: ignore[attr-defined]
    t1, n = pr.param_table(cf, {"name": "cf.params.t1"})  # type: ignore[arg-type]
    refs, bounds = pr.scan_param_sites(main, 0x40000400, t1, n)
    print(
        f"{image} cf.params.t1 {t1:#x} rows {n} sites {len(refs)} refs + {len(bounds)} bounds, digest {pr.params_sites_digest(refs, bounds)[:16]}"
    )
    if n != T1_SIGNATURE_ROWS:
        errs.append(f"{image}: {n} rows")
    return errs


def render_all(rules: list[dict]) -> str:
    head = "\n".join(
        [
            "[[claim]]",
            'resource = "cf.params.sites"',
            "",
            "[[anchor_rule]]",
            'name = "cf.params.t1"',
            'devices = ["dn2"]',
            'cpu = "cf"',
            'kind = "cf_params_t1"',
            'mark = "D"',
            "",
            "[[anchor_rule]]",
            'name = "cf.params.sites"',
            'devices = ["dn2"]',
            'cpu = "cf"',
            'kind = "cf_params_sites"',
            'mark = "D"',
            "",
        ]
    )
    return head + "\n" + base.render(rules, "dn2")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="dn2-1.12")
    ap.add_argument("--check", nargs="*", default=[])
    a = ap.parse_args()
    rules = rules_for(a.ref)
    errs: list[str] = []
    for image in [a.ref, *a.check]:
        errs += check_image(rules, image)
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if not a.check:
        print(render_all(rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
