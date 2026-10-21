#!/usr/bin/env python3
"""The `debug` part `capacity` of the digikit scenarios: every capacity limit of the runtime is filled and the next module refused with its
code (docs/plans/capacity-isolation.md; the sets are `pk_capacity.py`, the modules `patchkit synth-dkm`).

Each case is one boot on a ColdFire-only rig (about 6 s cold): a card with the case's modules in the store and a selection naming them in
order. A refusal is decided on the ColdFire before any DSP transfer, so no DSP is needed. After the boot the part checks, for each case:

- the boot report: every module has the result the set says (a loaded module: 0, or `LOADING` while its DSP part is still to come);
- MODULES: exactly the loaded modules are listed, their ColdFire blocks lie in the arena and do not overlap, their DSP ranges lie in the
  area and do not overlap, their voice rows are the ones the set takes;
- the runtime's table check never failed (`DKRT_TABLE_BAD` is 0, no `TABLE_BAD` event): a refused module left no range, row or owner behind;
- the figures of the full resource: the arena has no free byte, the DSP area no free granule, every voice row has an owner.

Not covered: a DSP transfer that fails (`E_DSP_XFER`) and anything that needs the DSP to run (the isolation scenarios do that).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from tools.device import dkupload as DK
from tools.patchkit import pk_capacity as P
from tools.patchkit import pk_digikit_debug as D
from tools.patchkit import pk_dkrt_boot as B
from tools.patchkit import pk_dkrt_machine as M

DK_SEL_MAX = (
    B.SEL_MAX
)  # the selection record's paths (sdk/sdk.toml [limits.max] modules)


def _result_ok(got: int, want: int | str) -> bool:
    if want == P.LOADED:
        return got in (0, B.E_LOADING)
    return got == want


def _ranges_ok(mods: list[dict], key_off: str, key_len: str, lo: int, hi: int) -> bool:
    """Every nonzero range [off, off + len) of MODULES lies in [lo, hi) and no two overlap."""
    rs = sorted((m[key_off], m[key_off] + m[key_len]) for m in mods if m[key_len])
    return all(lo <= a and b <= hi for a, b in rs) and all(
        rs[i][1] <= rs[i + 1][0] for i in range(len(rs) - 1)
    )


def verify(c: Any, rig: Any, g: P.Geometry, case: P.Case) -> dict[str, Any]:
    """The checks every case shares, and those of the case. Returns MODULES."""
    up = D.uploader(rig)
    info = rig.boot_info()
    got = list(info["results"])
    want = [m.want for m in case.mods]
    c.check(
        f"{case.name}: results of the boot report: {case.what}",
        info["count"] == len(want)
        and len(got) == len(want)
        and all(_result_ok(a, b) for a, b in zip(got, want, strict=True)),
        f"got {got}, want {want}",
    )
    m = up.modules()
    loaded = [
        mod.name
        for mod, r in zip(case.mods, got, strict=True)
        if _result_ok(r, P.LOADED) and mod.want == P.LOADED
    ]
    names = [x["name"] for x in m["modules"]]
    c.check(
        f"{case.name}: MODULES lists exactly the loaded modules, in order",
        names == loaded and m["total"] == len(loaded),
        f"{names} vs {loaded}",
    )
    mods = m["modules"]
    c.check(
        f"{case.name}: ColdFire blocks lie in the arena and do not overlap; DSP ranges lie in the area and do not overlap",
        _ranges_ok(mods, "cf_base", "cf_size", g.arena_lo, g.arena_hi)
        and _ranges_ok(mods, "dsp_off", "dsp_len", 0, g.dsp_area),
        str(
            [
                (x["name"], hex(x["cf_base"]), x["cf_size"], x["dsp_off"], x["dsp_len"])
                for x in mods
            ]
        ),
    )
    bad = up.peek(M.sym(rig.plan, "core/runtime", "DKRT_TABLE_BAD"), 1)[0]
    c.check(
        f"{case.name}: the runtime's table check never failed (a refused module left nothing behind)",
        bad == 0 and not D.events(rig, "TABLE_BAD"),
        f"DKRT_TABLE_BAD {bad}",
    )
    return m


def _by_name(m: dict, name: str) -> dict:
    return next(x for x in m["modules"] if x["name"] == name)


def check_case(c: Any, rig: Any, g: P.Geometry, case: P.Case, m: dict) -> None:
    """What only this case shows."""
    n = case.name
    mods = m["modules"]
    raw_blocks = sum(P.block(x["cf_size"]) for x in mods)
    if n == "modules":
        c.check(
            f"modules: all {g.modules} module records are in use",
            m["total"] == g.modules == len(mods)
            and sorted(x["index"] for x in mods) == list(range(g.modules)),
            str([x["index"] for x in mods]),
        )
        up = D.uploader(rig)
        # a ninth path is refused by SELECT (E_SEL_COUNT), so a ninth module never reaches the loader and E_TABLE_FULL stays a defence
        paths = [f"modules/n{i}.dkm" for i in range(DK_SEL_MAX + 1)]
        try:
            up._ok(
                DK.CMD_SELECT,
                "select",
                bytes((len(paths),)) + b"".join(p.encode() + b"\0" for p in paths),
            )
            status = 0
        except DK.UploadError as e:
            status = e.status
        c.check(
            f"modules: SELECT of {DK_SEL_MAX + 1} paths is E_SEL_COUNT (-41), so E_TABLE_FULL is unreachable",
            status == P.E_SEL_COUNT,
            str(status),
        )
        c.check(
            "modules: the module table is no larger than the selection record (the invariant that makes E_TABLE_FULL a defence)",
            g.modules <= DK_SEL_MAX,
            f"{g.modules} <= {DK_SEL_MAX}",
        )
        mailbox = rig.set_selection([chr(ord("a") + i) for i in range(DK_SEL_MAX + 1)])
        c.check(
            "modules: the mailbox select of a ninth path is E_SEL_PATH (-40: another code than SELECT's, test hook only)",
            mailbox == P.E_SEL_PATH,
            str(mailbox),
        )
    elif n == "files":
        exact = _by_name(m, "cap/f-exact")
        c.check(
            "files: the module whose file is the staging buffer's size is in the arena with its whole image",
            exact["cf_size"] == g.stage_bytes - P.DKM_HEADER,
            str(exact["cf_size"]),
        )
        c.check(
            "files: the arena holds the two loaded blocks and nothing of the refused files",
            m["arena_free"] + raw_blocks == g.arena_free,
            f"{m['arena_free']} + {raw_blocks} vs {g.arena_free}",
        )
    elif n == "arena":
        c.check(
            "arena: every byte of the arena is in use (nothing free, no free block)",
            m["arena_free"] == 0
            and m["arena_largest"] == 0
            and raw_blocks == g.arena_free,
            f"free {m['arena_free']}, largest {m['arena_largest']}, blocks {raw_blocks} of {g.arena_free}",
        )
        c.check(
            "arena: the refused DSP module left no range in the DSP area and no row",
            m["dsp_free"] == g.dsp_area
            and m["dsp_largest"] == g.dsp_area
            and all(x["rows"] == 0 for x in mods),
            f"dsp free {m['dsp_free']} of {g.dsp_area}",
        )
    elif n == "dsp-area":
        r, a = _by_name(m, "cap/d-r"), _by_name(m, "cap/d-a")
        c.check(
            "dsp-area: the two modules lie back to back from the first 8-aligned byte of the area to its last whole granule; less than a granule is left",
            (r["dsp_off"], r["dsp_len"]) == (g.dsp_first, P.DSP_GRANULE)
            and (a["dsp_off"], a["dsp_len"])
            == (g.dsp_first + P.DSP_GRANULE, g.dsp_tail)
            and m["dsp_largest"] < P.DSP_GRANULE
            and m["dsp_free"] == g.dsp_area - P.DSP_GRANULE - g.dsp_tail,
            f"{r['dsp_off']}+{r['dsp_len']}, {a['dsp_off']}+{a['dsp_len']}, free {m['dsp_free']}, largest {m['dsp_largest']}",
        )
    elif n == "rows-all":
        mask = (1 << g.machine_rows) - 1
        x = _by_name(m, "cap/r-all")
        c.check(
            "rows-all: the module owns every voice row, with the run-time types in order",
            x["rows"] == mask
            and x["types"] == list(range(g.type_first, g.type_first + g.machine_rows)),
            f"rows {x['rows']:#x}, types {x['types']}",
        )
    elif n == "rows-split":
        mask = (1 << g.machine_rows) - 1
        a, b = _by_name(m, "cap/s-a"), _by_name(m, "cap/s-b")
        c.check(
            "rows-split: the two modules own every voice row between them, each its own",
            a["rows"] | b["rows"] == mask
            and a["rows"] & b["rows"] == 0
            and a["rows"]
            and b["rows"],
            f"{a['rows']:#x} {b['rows']:#x}",
        )
    elif n == "rows-types":
        p = _by_name(m, "cap/t-p")
        c.check(
            "rows-types: the one loaded module owns one row; the refused ones took none",
            bin(p["rows"]).count("1") == 1 and m["total"] == 1,
            f"{p['rows']:#x}",
        )
    elif n == "machines":
        a = _by_name(m, "cap/m-a")
        c.check(
            "machines: the module holds every run-time machine type and every parameter row",
            sorted(a["types"])
            == list(range(g.type_first, g.type_first + g.machine_rows)),
            str(a["types"]),
        )


def run_capacity(c: Any) -> None:
    """The capacity cases of this scenario's device: pack every set, boot each on its own card in a thread, check."""
    img = c.image_for(B.RT, "run")
    g = P.Geometry.of(
        c.device,
        M.sym(img.plan, "core/runtime", "DKRT_ARENA"),
        M.sym(img.plan, "core/dsp-link", "_dl_area"),
    )
    cases = P.cases(g)
    packed: dict[str, list[str]] = {}
    for case in cases:
        packed[case.name] = [
            c.pack_synth(m.spec, f"{case.name}-{i}.dkm")
            for i, m in enumerate(case.mods)
        ]
    exact = Path(c.env.folder / "digikit" / "modules" / "files-2.dkm").stat().st_size
    c.check(
        "the 'files' fixture is exactly the staging buffer's size, the next one 4 bytes over",
        exact == g.stage_bytes
        and Path(c.env.folder / "digikit" / "modules" / "files-3.dkm").stat().st_size
        == g.stage_bytes + 4,
        str(exact),
    )
    c.report["geometry"] = vars(g)

    def one(case: P.Case) -> None:
        card = c.card(packed[case.name])
        with c.rig(img, card, case.name, cf_only=True) as rig:
            m = verify(c, rig, g, case)
            check_case(c, rig, g, case, m)
            c.report.setdefault("cases", {})[case.name] = {
                "results": rig.boot_info()["results"],
                "arena_free": m["arena_free"],
                "dsp_free": m["dsp_free"],
            }
            c.done(rig)

    c.parallel(*[functools.partial(one, case) for case in cases])
    c.common_checks_upload(project_writes_ok=True)
