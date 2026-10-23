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
from tools.patchkit import pk_device
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


def _cap_ok(
    cap: dict,
    m: dict,
    g: P.Geometry,
    lim: Any,
    rows: int,
    selected: int,
    store_kib: int,
) -> bool:
    """The CAPACITY reply against MODULES (what is used) and the device file (what the limits are)."""
    arena = g.arena_hi - g.arena_lo
    want = {
        "MODULES": (g.modules, m["total"], 0),
        "SELECTION": (DK_SEL_MAX, selected, 0),
        "ARENA": (arena, arena - m["arena_free"], m["arena_largest"]),
        "STAGE": (g.stage_bytes, 0, g.stage_bytes),
        "DSP_AREA": (g.dsp_area, g.dsp_area - m["dsp_free"], m["dsp_largest"]),
        "VOICE_ROWS": (g.machine_rows, bin(rows).count("1"), 0),
        "MACHINE_ROWS": (g.machine_rows, None, 0),
        "PARAM_ROWS": (lim.param_rows, None, 0),
        "PAGE_ROWS": (lim.page_rows, None, 0),
        "FX_SLOTS": (lim.fx_slots, 0, 0),
        "MAP_ENTRIES": (pk_device.contract_max()["map_entries"], None, 0),
        "STORE": (store_kib, None, 0),  # the area in KiB (devices/*.toml [card.store])
    }
    return set(cap) == set(want) and all(
        (cap[k]["limit"], cap[k]["largest"]) == (v[0], v[2])
        and (v[1] is None or cap[k]["used"] == v[1])
        for k, v in want.items()
    )


def boot_checks(
    c: Any,
    rig: Any,
    g: P.Geometry,
    tag: str,
    names: list[str],
    wants: list[int | str],
    what: str,
) -> dict[str, Any]:
    """The checks every boot of a set shares: the results WANTS of the boot report, MODULES lists exactly the loaded modules, ranges inside
    their areas and disjoint, the table check never failed. Returns MODULES."""
    up = D.uploader(rig)
    info = rig.boot_info()
    got = list(info["results"])
    c.check(
        f"{tag}: results of the boot report: {what}",
        info["count"] == len(wants)
        and len(got) == len(wants)
        and all(_result_ok(a, b) for a, b in zip(got, wants, strict=True)),
        f"got {got}, want {wants}",
    )
    m = up.modules()
    mods = m["modules"]
    loaded = [n for n, w in zip(names, wants, strict=True) if w == P.LOADED]
    c.check(
        f"{tag}: MODULES lists exactly the loaded modules, in order",
        [x["name"] for x in mods] == loaded and m["total"] == len(loaded),
        f"{[x['name'] for x in mods]} vs {loaded}",
    )
    c.check(
        f"{tag}: ColdFire blocks lie in the arena and do not overlap; DSP ranges lie in the area and do not overlap",
        _ranges_ok(mods, "cf_base", "cf_size", g.arena_lo, g.arena_hi)
        and _ranges_ok(mods, "dsp_off", "dsp_len", 0, g.dsp_area),
        str(
            [
                (x["name"], hex(x["cf_base"]), x["cf_size"], x["dsp_off"], x["dsp_len"])
                for x in mods
            ]
        ),
    )
    cap = up.capacity()
    m["cap"] = cap
    lim = pk_device.limits(c.device)
    rows = 0
    for x in mods:
        rows |= x["rows"]
    c.check(
        f"{tag}: CAPACITY agrees with MODULES (modules, arena, DSP area, voice rows, selection) and with the profile (every limit)",
        _cap_ok(cap, m, g, lim, rows, len(names), c.store.limit // 2),
        str(cap),
    )
    bad = up.peek(M.sym(rig.plan, "core/runtime", "DKRT_TABLE_BAD"), 1)[0]
    c.check(
        f"{tag}: the runtime's table check never failed (a refused module left nothing behind)",
        bad == 0 and not D.events(rig, "TABLE_BAD"),
        f"DKRT_TABLE_BAD {bad}",
    )
    return m


def verify(c: Any, rig: Any, g: P.Geometry, case: P.Case) -> dict[str, Any]:
    """The checks every case shares. Returns MODULES."""
    return boot_checks(
        c,
        rig,
        g,
        case.name,
        [m.name for m in case.mods],
        [m.want for m in case.mods],
        case.what,
    )


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
            "modules: the mailbox select of a ninth path is E_SEL_COUNT (-41), as SELECT's",
            mailbox == P.E_SEL_COUNT,
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
        used = {k: m["cap"][k]["used"] for k in ("MACHINE_ROWS", "PARAM_ROWS")}
        c.check(
            "machines: CAPACITY counts every machine type and parameter row as used",
            used == {"MACHINE_ROWS": g.machine_rows, "PARAM_ROWS": g.param_rows},
            str(used),
        )
    elif n == "pages":
        c.check(
            "pages: CAPACITY counts every page row as used",
            m["cap"]["PAGE_ROWS"]["used"] == g.page_rows,
            str(m["cap"]["PAGE_ROWS"]),
        )
    if n in ("rows-all", "rows-split"):
        c.check(
            "rows: CAPACITY counts every voice row as used",
            m["cap"]["VOICE_ROWS"]["used"] == g.machine_rows,
            str(m["cap"]["VOICE_ROWS"]),
        )
    if n == "arena":
        c.check(
            "arena: CAPACITY says the arena is used up",
            m["cap"]["ARENA"]["used"] == m["cap"]["ARENA"]["limit"]
            and m["cap"]["ARENA"]["largest"] == 0,
            str(m["cap"]["ARENA"]),
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


# ---- declared needs: what a module's header says it takes is what the loader takes ---------------------------------------------


def run_needs(c: Any) -> None:
    """The needs a packed module declares in its header (`[dkm.needs]` in sdk/sdk.toml; the menu and the hosts say from them whether a module
    fits) equal what the runtime takes of each resource when it loads the module. Every module that packs for the device (`dktransfer.candidates`,
    the repository's run-time modules and the fixtures, less `[debug] exclude`: the fixtures that fault on purpose; `[debug] static` names the core modules patched in for the ones that import from them) is loaded alone on a
    ColdFire-only rig and CAPACITY and MODULES are read: the arena block of image and bss (and, with DSP code, the block of the DSP image, which
    stays until a transfer that a ColdFire-only rig never makes), the DSP range, the voice rows, and for a module with no DSP code the machine,
    parameter and page rows. The registry rows of a DSP module come after the DSP acknowledges: `isolation-*` checks them on a coupled boot."""
    from tools.device import dktransfer as T
    from tools.device import dkupload as DKU

    skip = set(c.table().get("exclude", []))
    # `static`: core modules patched in besides the runtime, for the modules that import from them (core/input)
    img = c.image_for(",".join([B.RT, *c.table().get("static", [])]), "run")
    found: list[tuple[str, str]] = []
    packs: list[str] = []
    for mid in T.candidates():
        if mid in skip:
            continue
        file = mid.replace("/", "-") + ".dkm"
        try:
            found.append((mid, c.pack(mid, file)))
        except Exception:  # noqa: BLE001  a module that has no object for this device does not pack
            continue
        packs.append(mid)
    c.check(
        "modules pack for this device (the run-time modules and the fixtures, less the excluded)",
        len(found) >= 8,
        str(packs),
    )
    c.report["needs"] = {}

    def one(mid: str, path: str) -> None:
        data = (c.env.folder / "digikit" / path).read_bytes()
        n = DKU.dkm_needs(data)
        card = c.card([path])
        with c.rig(img, card, "needs-" + mid.replace("/", "-"), cf_only=True) as rig:
            res = rig.boot_info()["results"]
            up = D.uploader(rig)
            cap = up.capacity()
            mods = up.modules()["modules"]
            used = {k: v["used"] for k, v in cap.items()}
            loaded = bool(res) and res[0] in (0, B.E_LOADING)
            dsp = n["DSP_AREA"] != 0
            want = {
                "ARENA": n["ARENA"] + n["ARENA_XFER"],
                "DSP_AREA": n["DSP_AREA"],
                "VOICE_ROWS": n["VOICE_ROWS"],
            }
            if (
                not dsp
            ):  # a DSP module's registry rows come after the DSP's acknowledgement
                want |= {
                    "MACHINE_ROWS": n["MACHINE_ROWS"],
                    "PARAM_ROWS": n["PARAM_ROWS"],
                    "PAGE_ROWS": n["PAGE_ROWS"],
                }
            got = {k: used[k] for k in want}
            rec = mods[0] if mods else None
            rec_ok = (
                rec is not None
                and P.block(rec["cf_size"]) == n["ARENA"]
                and rec["dsp_len"] == n["DSP_AREA"]
                and bin(rec["rows"]).count("1") == n["VOICE_ROWS"]
            )
            c.check(
                f"{mid}: loaded, and the resources taken (CAPACITY, MODULES) are its declared needs {n}",
                loaded and got == want and rec_ok,
                f"result {res}, taken {got}, declared {want}, record {rec}",
            )
            c.report["needs"][mid] = {"declared": n, "taken": got}
            c.done(rig)

    c.parallel(*[functools.partial(one, mid, path) for mid, path in found])
    c.common_checks_upload(project_writes_ok=True)


# ---- the MODULES page: size and a mark for a module that will not fit ---------------------------------------------------------


def fit_rows(
    names: list[str], files: list[bytes], ticked: set[int], cap: dict
) -> list[str]:
    """The module rows of SETTINGS > DIGIKIT > MODULES for a store (`fill_load`, `refit` of modules/core/digikit-menu): the tick, `x` for a module that does
    not fit next to the ticked ones (the next boot loads them into an empty runtime), the name (without `.dkm`, the first 8 characters, upper case) and the memory it
    takes in K. CAP is the CAPACITY reply."""
    from tools.device import dkfit as F
    from tools.device import dkupload as DKU

    needs = [{**DKU.dkm_needs(f), "file_size": len(f)} for f in files]
    left = F.empty_caps(cap)
    wont: set[int] = set()
    for i in sorted(ticked):  # the ticked ones in list order, then the others
        if F.fits_now(needs[i], left):
            wont.add(i)
        else:
            left = F.take(needs[i], left)
    for i in range(len(files)):
        if i not in ticked and F.fits_now(needs[i], left):
            wont.add(i)
    rows = []
    for i, (name, n) in enumerate(zip(names, needs, strict=True)):
        total = n["ARENA"] + n["DSP_AREA"]
        k = -(-total // 1024)
        size = "<1K" if total < 1024 else f"{k}K"
        row = ("[X]" if i in ticked else "[ ]") + ("x" if i in wont else " ")
        row += name.removesuffix(".dkm").upper()[:8]
        rows.append(row.ljust(13) + size.rjust(4))
    return rows


def run_fits(c: Any) -> None:
    """The MODULES page tells before a selection whether a module will load: a store of four modules, one small, two that each take a little over
    half of the DSP area, one larger than the area. Opened with nothing ticked: only the largest has the `x` mark. With the first half-size one ticked:
    the second one has it too, and selecting it ticks nothing, shows a popup `NO FIT: DSP 164K > 155K FREE` and the short form in the RESULT row. The rows
    (DK_LOAD) equal `fit_rows`, which uses the Python rule on the CAPACITY reply and the packed headers."""
    from tools.coldfire import digikit_menu_proof as DP
    from tools.device import dkfit as F
    from tools.device import dkupload as DKU

    img = c.image_for(f"{B.RT},core/digikit-menu", "menu")
    plan = DP.Plan(img.plan)
    g = P.Geometry.of(
        c.device,
        M.sym(img.plan, "core/runtime", "DKRT_ARENA"),
        M.sym(img.plan, "core/dsp-link", "_dl_area"),
    )
    half = g.dsp_area // 2 + 4096
    specs = {
        "half-a": P.spec("fit/half-a", dsp={"image": 64, "bss": half - 64}),
        "half-b": P.spec("fit/half-b", dsp={"image": 64, "bss": half - 64}),
        "huge": P.spec("fit/huge", dsp={"image": 64, "bss": g.dsp_area}),
        "small": P.spec("fit/small", pad=64),
    }
    paths = [c.pack_synth(sp, f"{k}.dkm") for k, sp in specs.items()]
    files = [(c.env.folder / "digikit" / p).read_bytes() for p in paths]
    names = [Path(p).name for p in paths]
    order = sorted(
        range(len(names)), key=lambda i: names[i]
    )  # the menu lists in path order
    names = [names[i] for i in order]
    files = [files[i] for i in order]
    card = c.card(None)

    def w(rig: Any, name: str, k: int = 0) -> int:
        return rig.w(plan.addr(name) + 4 * k)

    def text(rig: Any, name: str, i: int) -> str:
        raw = b"".join(
            rig.w(plan.addr(name) + 24 * i + 4 * k).to_bytes(4, "big") for k in range(6)
        )
        return raw.split(b"\0")[0].decode("latin-1")

    def tap(rig: Any, key: str, gap_ms: int = 350) -> None:
        rig.s.do_tap(key)
        rig.s.run_ms(gap_ms)

    def rows(rig: Any) -> list[str]:
        return [text(rig, "DK_LOAD", i) for i in range(w(rig, "DK_LOAD_N"))][
            : len(names)
        ]

    def shot(rig: Any, name: str) -> None:
        """The OLED as a PNG in the scenario's work folder (`--keep`): to look at the rows."""
        from tools.control.digi_client import oled_png

        (c.work / f"fits-{name}.png").write_bytes(oled_png(rig.s.frame()[1]))

    with c.rig(img, card, "fits") as rig:
        up = D.uploader(rig)
        cap = up.capacity()
        tap(rig, "NO", 400)  # SETTINGS > DIGIKIT (the ninth row) > MODULES
        tap(rig, "SETTINGS", 600)
        for _ in range(8):
            tap(rig, "DOWN", 200)
        tap(rig, "YES", 700)
        tap(rig, "YES", 900)
        shot(rig, "open")
        want = fit_rows(names, files, set(), cap)
        got = rows(rig)
        c.check(
            "MODULES, nothing ticked: size and the mark for the module larger than the DSP area only",
            got == want
            and [r[3] for r in got].count("x") == 1
            and got[names.index("huge.dkm")][3] == "x",
            f"{got} vs {want}",
        )
        first = names.index("half-a.dkm")
        second = names.index("half-b.dkm")
        for _ in range(first):
            tap(rig, "DOWN", 200)
        tap(rig, "YES", 500)  # tick half-a
        shot(rig, "ticked")
        got = rows(rig)
        want = fit_rows(names, files, {first}, cap)
        c.check(
            "MODULES, one half-size module ticked: the other one has the mark",
            got == want and got[first][:4] == "[X] " and got[second][:4] == "[ ]x",
            f"{got} vs {want}",
        )
        for _ in range(second - first):
            tap(rig, "DOWN", 200)
        shots = w(rig, "DK_POPUPS")
        tap(rig, "YES", 700)  # select half-b: refused
        shot(rig, "refused")

        def need_of(k: int) -> dict[str, int]:
            return {**DKU.dkm_needs(files[k]), "file_size": len(files[k])}

        short = F.fits_now(need_of(second), F.take(need_of(first), F.empty_caps(cap)))
        line = F.menu_line(short, compact=True) if short else ""
        full = f"NO FIT: {F.menu_line(short)}" if short else ""
        got_rows = [text(rig, "DK_LOAD", i) for i in range(w(rig, "DK_LOAD_N"))]
        popup = (
            bytes(
                b
                for k in range(12)
                for b in rig.w(plan.addr("DK_POPUP") + 4 * k).to_bytes(4, "big")
            )
            .split(b"\0")[0]
            .decode("latin-1")
        )
        c.check(
            "selecting it ticks nothing, shows a popup and the RESULT row says which resource is short",
            w(rig, "DK_POPUPS") == shots + 1
            and popup == full
            and got_rows[-1] == line
            and rows(rig) == fit_rows(names, files, {first}, cap),
            f"popup {popup!r}, last row {got_rows[-1]!r}, want {line!r}",
        )
        c.checks += rig.checks
        c.done(rig)
    c.common_checks()


# ---- the full set: every limit used at once, behind a reference module ---------------------------------------------------------


def check_full(
    c: Any,
    rig: Any,
    g: P.Geometry,
    tag: str,
    wants: list[int | str],
    names: list[str],
    exact_arena: bool = True,
) -> dict:
    """The boot of a full set: the shared checks, then every resource is used up (no whole free DSP granule, every voice row and machine
    type owned, and with EXACT_ARENA no free arena byte: a coupled boot frees the DSP images' arena copies after the transfers)."""
    m = boot_checks(
        c,
        rig,
        g,
        tag,
        names,
        wants,
        "every module of the full set loaded, the three behind it refused (E_NOMEM, E_DSP_ROWS, E_DSP_NOSPACE)",
    )
    mods = m["modules"]
    owned = 0
    for x in mods:
        owned |= x["rows"]
    types = sorted({t for x in mods for t in x["types"]})
    c.check(
        f"{tag}: every voice row has an owner and every machine type is in use",
        owned == (1 << g.machine_rows) - 1
        and types == list(range(g.type_first, g.type_first + g.machine_rows)),
        f"rows {owned:#x}, types {types}",
    )
    full = {
        "MACHINE_ROWS": g.machine_rows,
        "VOICE_ROWS": g.machine_rows,
        "PARAM_ROWS": g.param_rows,
        "PAGE_ROWS": g.page_rows,
    }
    c.check(
        f"{tag}: CAPACITY counts every machine, voice, parameter and page row as used",
        all(m["cap"][k]["used"] == v for k, v in full.items()),
        str({k: m["cap"][k] for k in full}),
    )
    c.check(
        f"{tag}: the DSP area has no whole granule left"
        + (" and the arena no byte" if exact_arena else ""),
        m["dsp_largest"] < P.DSP_GRANULE
        and (not exact_arena or (m["arena_free"] == 0 and m["arena_largest"] == 0)),
        f"dsp free {m['dsp_free']} largest {m['dsp_largest']}, arena free {m['arena_free']}",
    )
    return m


def run_isolation(c: Any) -> None:
    """A reference module (`[isolation] module`, a sound) loaded first keeps its sound, its ranges and its DSP bytes when the full set is
    loaded behind it and three modules are refused: boot 1 has the reference alone, boot 2 the reference and the full set. Both are
    coupled boots (the DSP runs); each plays the reference machine on track 1. Checked: the reference's MODULES record equal in both boots;
    its relocated DSP image read back from L2 equals the packed image (relocated at its range) in both boots, after the note; the note's PCM
    equal from the onset (a pairing: the one thing that differs between the boots is what is loaded behind the reference)."""
    import numpy as np

    t = c.table()
    ref, machine = t["module"], t["machine"]
    img = c.image_for(B.RT, "run")
    plan = img.plan
    g = P.Geometry.of(
        c.device,
        M.sym(plan, "core/runtime", "DKRT_ARENA"),
        M.sym(plan, "core/dsp-link", "_dl_area"),
    )
    ref_path = c.pack(ref, "ref.dkm")
    ref_bytes = (c.env.folder / "digikit" / "modules" / "ref.dkm").read_bytes()
    fill = P.full_set(g, [P.usage(ref_bytes)])
    fill_paths = [c.pack_synth(m.spec, f"fill-{i}.dkm") for i, m in enumerate(fill)]
    names = [ref] + [m.name for m in fill]
    cards = {"alone": c.card([ref_path]), "full": c.card([ref_path, *fill_paths])}
    wants: dict[str, list[int | str]] = {
        "alone": [P.LOADED],
        "full": [P.LOADED, *(m.want for m in fill)],
    }
    trig = t.get("trig_frame", M.TRIG_FRAME if c.dt2 else M.TRIG_FRAME_DN2)
    hint = t.get("hint", 7 if c.dt2 else 5)
    sec = M.parse_dsp_section(ref_bytes)
    out: dict[str, Any] = {}

    def boot(key: str) -> None:
        with c.rig(img, cards[key], key) as rig:
            if key == "full":
                m = check_full(c, rig, g, "full", wants[key], names, exact_arena=False)
            else:
                m = boot_checks(
                    c, rig, g, key, [ref], wants[key], "the reference alone"
                )
            rec = _by_name(m, ref)
            base = M.sym(plan, "core/dsp-link", "_dl_area") + rec["dsp_off"]
            imports = M.run_imports(rig, plan)
            want = M.relocate(sec, base, imports)
            before = rig.dsp_bytes(base, len(want))
            if c.dt2:
                rig.select_type(rig.dyn_type(ref, machine), hint)
            else:
                rig.select(hint)
            pcm = rig.note(c.work / f"{key}.wav", trig)
            after = rig.dsp_bytes(base, len(want))
            if key == "alone":
                # the DSP part is done: the arena holds the module's block only (the DSP image's copy is freed) and every row is in the registry
                n = DK.dkm_needs(ref_bytes)
                kinds = (
                    "ARENA",
                    "DSP_AREA",
                    "MACHINE_ROWS",
                    "VOICE_ROWS",
                    "PARAM_ROWS",
                    "PAGE_ROWS",
                    "FX_SLOTS",
                )
                cap = D.uploader(rig).capacity()
                took = {k: cap[k]["used"] for k in kinds}
                c.check(
                    "alone: after the load the runtime holds exactly the reference's declared needs (arena block, DSP range, machine, voice, parameter, page rows, FX slots)",
                    took == {k: n[k] for k in kinds},
                    f"taken {took}, declared {n}",
                )
            c.check(
                f"{key}: the reference's DSP bytes in L2 equal its packed image relocated at its range, before and after the note",
                before == want and after == want,
                f"range {rec['dsp_off']:#x}+{rec['dsp_len']:#x}, {sum(a != b for a, b in zip(before, want, strict=True))} and {sum(a != b for a, b in zip(after, want, strict=True))} bytes differ",
            )
            c.check(
                f"{key}: the reference note is audible",
                float(abs(pcm).max()) > 0.01,
                f"peak {float(abs(pcm).max()):.3f}",
            )
            out[key] = {
                "pcm": pcm,
                "record": {
                    k: rec[k]
                    for k in ("cf_size", "dsp_off", "dsp_len", "rows", "types")
                },
                "rig": rig,
            }
            c.done(rig)

    c.parallel(functools.partial(boot, "alone"), functools.partial(boot, "full"))
    c.check(
        "the reference's record (size, DSP range, rows, types) is the same with and without the full set behind it",
        out["alone"]["record"] == out["full"]["record"],
        f"{out['alone']['record']} vs {out['full']['record']}",
    )
    rr = out["full"]["rig"]
    # the onset may differ by a few blocks: the full boot has loaded more modules before the key reaches the sequencer (128 samples measured on
    # the Digitone II, 64 on the Digitakt II); from the onset on, not one sample may differ
    M.pcm_check(
        rr,
        out["alone"]["pcm"],
        out["full"]["pcm"],
        c.report,
        "reference note with the full set loaded",
        max_shift=256,
    )
    c.checks += rr.checks
    c.report["isolation"] = {
        "modules": names,
        "peak": float(np.abs(out["full"]["pcm"]).max()),
    }
    c.common_checks()
