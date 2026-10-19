#!/usr/bin/env python3
"""Print the `[[anchor_rule]]` blocks of modules/core/machines (the machine framework's ColdFire sites).

    uv run python -m tools.patchkit.pk_machine_rules [--ref dt2-1.16] [--check dt2-1.17 ...]

Each site of tools/patchkit/machineprofile.py (the nine machinepatch parts) lies in a function. The rule names that function
by its cfdb `h_strict` hash (a hash of the instructions with address operands masked; no firmware bytes), the
ordinal among functions sharing that hash, and the byte offset and length of the site inside it. `--check` resolves
every rule against other images through the same cfdb query tools/patchkit/port_resolve.py uses and fails when a rule does
not match a function. The rule list is the only place the sites are written down; modules/core/machines/module.toml
holds its output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.coldfire import cf  # noqa: E402
from tools.firmware import config  # noqa: E402
from tools.patchkit import machinepatch as mp  # noqa: E402
from tools.patchkit import machineprofile as mprof  # noqa: E402
from tools.patchkit import port_resolve as pr  # noqa: E402

SYX = {
    "dt2-1.16": "Digitakt_II_OS1.16.syx",
    "dt2-1.17": "Digitakt_II_OS1.17.syx",
}
# moveq #8,D2 in the sound loader (finding 02, "Persistence"); the byte after it is the immediate
PERSIST_SITE = 0x400DB2EA
FRAME_SLICE_SITE = 0x4002EC18
# FUN_400da23e(param, type, ..) indexes a 7 x 10 table of machine parameters (SoundParameterSet::vfunc_20, the
# parameter-change events): `move.l 8(sp),D0; movea.l 0xc(sp),A1` loads the type
TYPEARG_SITES = (0x400DA248,)
# SourcePageView::vfunc_4 (finding 50): the first test (tst.l 0x90(A2); beq.w T, function + 0x10), T, and FUN_4005f6bc (the
# track's type, called by the hook); the same addresses on 1.16 and 1.17
SRCPAGE_SITES = (
    ("cf.machines.srcpage", 0x40016EBC, 8),
    ("cf.machines.srcpage.alt", 0x400170AA, 2),
    ("cf.machines.srcpage.type", 0x4005F6BC, 6),
)


def sites(profile: dict, read) -> list[tuple[str, int, int]]:
    """(anchor name, byte address, length) for every site of the framework."""
    out: list[tuple[str, int, int]] = []
    out.append(("cf.machines.dispatch", profile["dispatch"], 6))
    out.append(("cf.machines.list", profile["list_end_site"], 16))
    # `pea (0x1c).w`: the list builder allocates room for the seven stock types
    out.append(("cf.machines.list_alloc", profile["list_builder"] + 0x24, 4))
    out.append(("cf.machines.group", profile["group_bound"], 6))
    for i, (addr, _table) in enumerate(profile["name_accessors"]):
        out.append((f"cf.machines.name.{i}", addr, 0x18))
    out.append(("cf.machines.rank", profile["rank_call"], 6))
    out.append(("cf.machines.permit", profile["permit_bound"] - 2, 0x1E))
    for i, (bound, lea) in enumerate(profile["pertype_sites"]):
        out.append((f"cf.machines.pertype.{i}", bound, lea + 6 - bound))
    for i, (_fn, site) in enumerate(profile["clone_sites"]):
        _kind, t = mp.decode_clone_site(read, site)
        out.append((f"cf.machines.clone.{i}", site, len(bytes.fromhex(t[1]))))
    # the vector-191 frame builder sends MANUAL SLICE's slice table only for `type == 6`: moveq #6,D1;
    # mvs.b (0x990,A0),D0; cmp.l D0,D1; bne.w (finding 02, "core.machines"; not in the machinepatch profile)
    out.append(
        (f"cf.machines.clone.{len(profile['clone_sites'])}", FRAME_SLICE_SITE, 12)
    )
    for i, addr in enumerate(TYPEARG_SITES):
        out.append((f"cf.machines.typearg.{i}", addr, 8))
    out.append(("cf.machines.persist", PERSIST_SITE, 2))
    out.extend(SRCPAGE_SITES)
    return out


class Img:
    def __init__(self, name: str):
        self.name = name
        self.image = f"{name}-emac"
        self.db = cf.load(self.image).db

    def function_at(self, addr: int) -> int:
        row = self.db.execute(
            "SELECT entry FROM functions WHERE image=? AND entry<=? ORDER BY entry DESC LIMIT 1",
            (self.image, addr),
        ).fetchone()
        return row[0]

    def hash_of(self, entry: int) -> str:
        return self.db.execute(
            "SELECT h_strict FROM func_hash WHERE image=? AND entry=?",
            (self.image, entry),
        ).fetchone()[0]

    def with_hash(self, h: str) -> list[int]:
        return [
            r[0]
            for r in self.db.execute(
                "SELECT entry FROM func_hash WHERE image=? AND h_strict=? ORDER BY entry",
                (self.image, h),
            )
        ]


def build_rules(ref: str) -> list[dict]:
    main = pr.sections_of(config.stock(SYX[ref]))[3]
    base = mprof.LOAD_ADDR

    def read(a: int, n: int) -> bytes:
        return main[a - base : a - base + n]

    img = Img(ref)
    rules = []
    for name, addr, ln in sites(mprof.DT2_116, read):
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


def render(rules: list[dict], device: str = "dt2") -> str:
    out = []
    for r in rules:
        out += [
            "[[anchor_rule]]",
            f'name = "{r["name"]}"',
            f'devices = ["{device}"]',
            'cpu = "cf"',
            'kind = "cf_func"',
            f'hash = "{r["hash"]}"',
        ]
        if r["shared"] > 1:
            out.append(f"ordinal = {r['ordinal']}")
        out += [f"offset = {r['offset']:#x}", f"len = {r['len']}", 'mark = "D"', ""]
    return "\n".join(out)


def check(rules: list[dict], other: str) -> list[str]:
    img = Img(other)
    errs = []
    for r in rules:
        hits = img.with_hash(r["hash"])
        if r["ordinal"] >= len(hits):
            errs.append(f"{other}: {r['name']}: {len(hits)} functions with the hash")
            continue
        print(
            f"{other} {r['name']:24s} entry {hits[r['ordinal']]:#x} +{r['offset']:#x} ({len(hits)} with hash)"
        )
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="dt2-1.16")
    ap.add_argument("--check", nargs="*", default=[])
    a = ap.parse_args()
    rules = build_rules(a.ref)
    errs: list[str] = []
    for o in a.check:
        errs += check(rules, o)
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 1
    if not a.check:
        print(render(rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
