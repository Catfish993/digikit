#!/usr/bin/env python3
"""Print the `[[anchor_rule]]` blocks of modules/core/digikit-menu (the SETTINGS menu sites and the UI helpers it calls).

    uv run python -m tools.patchkit.pk_menu_rules [--ref dt2-1.16] [--check dn2-1.11 ...]

Each site is a function of the MAIN OS (or a place inside one). The rule names the function by its cfdb `h_strict` hash
(address operands masked, so the rule holds on every image that has the same code), the ordinal among functions that
share the hash, and the offset and length of the site. The same hashes match on all four images (finding 28).
`--check` resolves every rule on other images through the cfdb. Reference addresses are DT2 1.16 (finding 23, S2).
"""

from __future__ import annotations

import argparse
import sys

from tools.patchkit import pk_machine_rules as pmr

REBUILD = 0x400A59A0  # DigisharcSettingsMenu item list rebuild
# (name, address, length, kind): kind "code" is a cf_func rule, "ptr" a cf_ptr rule (the 4 bytes at the address are an
# absolute address literal; the anchor is the object it points to)
SITES = [
    # before the last step of the rebuild: `movea.l (a2),a0; pea 2.w; move.l d4,-(sp)` (restore the selection)
    ("cf.menu.settings_tail", REBUILD + 0x3A8, 8, "code"),
    # the SYSTEM item's icon: `pea (icon).l` at 0x400a5d22; the icon is an object built at run time (bss), so the anchor
    # is the instruction and the module reads its operand (u16 at +2, +4)
    ("cf.menu.icon_ref", REBUILD + 0x382, 6, "code"),
    ("cf.menu.fn.new", 0x4011E674, 4, "code"),
    ("cf.menu.fn.item_builder", 0x401158B0, 4, "code"),
    ("cf.menu.fn.view_ctor", 0x40119C8C, 4, "code"),
    ("cf.menu.fn.make_sp", 0x40119DDA, 4, "code"),
    ("cf.menu.fn.push", 0x4011A2B2, 4, "code"),
    ("cf.menu.fn.str_ctor", 0x401E7A64, 4, "code"),
    ("cf.menu.fn.str_dtor", 0x401E5FB4, 4, "code"),
    ("cf.menu.fn.blk_read", 0x4012DEDA, 4, "code"),
    # View::invalidate(View*): sets the view's and the view manager's dirty flags (322 callers); the View sub-object
    # is at +0x38 of a VerticalMenuView
    ("cf.menu.fn.invalidate", 0x4011AE46, 4, "code"),
    # The message popup, as `FUN_400a4832` (the "LOAD OK" / "LOAD ERROR" popup after a project load) builds it:
    # make_shared<PopupWindow>(const char *const &) with the result shared_ptr in A0; move it into a shared_ptr<View>;
    # add it to the view manager at Brain + 0x40 (exclusive flag 0); release both pointers. The helpers are templates shared
    # by many callers (their code hashes repeat), so the anchors are the call operands in that one function.
    ("cf.menu.fn.popup_make", 0x400A4862 + 2, 4, "ptr"),
    ("cf.menu.fn.sp_move", 0x400A4870 + 2, 4, "ptr"),
    ("cf.menu.fn.brain", 0x400A4876 + 2, 4, "ptr"),
    (
        "cf.menu.fn.mgr_of",
        0x400A487E + 2,
        4,
        "ptr",
    ),  # Brain* -> its view manager (Brain + 0x40)
    ("cf.menu.fn.view_add", 0x400A4890 + 2, 4, "ptr"),
    ("cf.menu.fn.sp_release", 0x400A4888 + 2, 4, "ptr"),
]


def build_rules(ref: str) -> list[dict]:
    img = pmr.Img(ref)
    rules = []
    for name, addr, ln, kind in SITES:
        entry = img.function_at(addr)
        h = img.hash_of(entry)
        same = img.with_hash(h)
        rules.append(
            {
                "name": name,
                "kind": kind,
                "hash": h,
                "ordinal": same.index(entry),
                "shared": len(same),
                "offset": addr - entry,
                "len": ln,
            }
        )
    return rules


def render(rules: list[dict]) -> str:
    out = []
    for r in rules:
        out += [
            "[[anchor_rule]]",
            f'name = "{r["name"]}"',
            'cpu = "cf"',
            f'kind = "{"cf_ptr" if r["kind"] == "ptr" else "cf_func"}"',
            f'hash = "{r["hash"]}"',
        ]
        if r["shared"] > 1:
            out.append(f"ordinal = {r['ordinal']}")
        out += [f"offset = {r['offset']:#x}", f"len = {r['len']}", 'mark = "D"', ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="dt2-1.17")
    ap.add_argument("--check", nargs="*", default=[])
    a = ap.parse_args()
    rules = build_rules(a.ref)
    errs: list[str] = []
    for o in a.check:
        errs += pmr.check(rules, o)
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if not a.check:
        print(render(rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
