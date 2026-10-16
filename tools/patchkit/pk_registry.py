#!/usr/bin/env python3
"""Run-time machines and parameters (core/dynreg + core/runtime registry) in the emulator, DT2.

    uv run python -m tools.patchkit.pk_registry proof [--image dt2-1.16] [--keep DIR] [--control]

Patches `core/runtime` and `core/dynreg` (so `core/machines`, `core/params`), packs `test/dyn-xslice` (a data-only machine and
one parameter) and variants that must be refused, builds a card with them in the module store, boots the patched firmware with
that card and drives the runtime through its mailbox (docs/design/digikit-registry.md). The firmware's own functions show what
it sees: the machine list getter (`FUN_40052a08`, through a six-byte stub in free RAM that sets its result register), the machine
descriptor dispatch and the tables. Checks: refused files write nothing (the rows, names, permission, per-type and parameter
tables are byte for byte unchanged); a good file fills exactly its rows and the list gains the machine; a second file with the
same type is refused; unload gives back the rows (list and tables equal the start, but for the pointers of the string
representations); load and unload again. The write watch covers the rows and tables with the runtime as the only allowed
writer, and the machines framework's own lazy table builder is listed apart. Prints one JSON report; exit 0 when every check holds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.firmware import config  # noqa: E402
from tools.patchkit.pk_dkrt import (  # noqa: E402
    DEVICE_SYX,
    OK,
    ProofError,
    Tools,
    be32,
    build_card,
    cmd_ops,
    patch,
    run_script,
    runtime_ranges,
    sbe32,
)

E_FORMAT = -5
E_NO_REGISTRY, E_REG_DEVICE, E_TYPE_RANGE, E_TYPE_USED, E_PARAM_FULL = (
    -20,
    -21,
    -22,
    -23,
    -24,
)
H_SHA, H_REG_OFF = 0x10, 0x90
FIRST_DYN, N_DYN = 16, 8
ROW, PF, PREG = 64, 60, 0x1C
STUB = 0x47FD0000  # six-byte stub that loads a0 and calls the list getter (free RAM below the harness stack)
SRET = 0x47FD1000
IMAGES = ("dt2-1.16", "dt2-1.17")


def anchor(image: str, name: str) -> int:
    """The address of a port anchor (devices/ports/<image>.toml)."""
    import tomllib

    port = tomllib.loads((ROOT / "devices" / "ports" / f"{image}.toml").read_text())
    return next(a["addr"] for a in port["anchor"] if a["name"] == name)


def sym(plan: dict, owner: str, name: str) -> int:
    for k, v in plan["symbols"].items():
        if k.startswith(owner + "@") and k.split(":", 1)[1] == name:
            return v
    raise ProofError(f"no symbol {owner}:{name}")


def resha(f: bytes) -> bytes:
    """A .dkm with its sha256 field recomputed (for test files made by editing bytes)."""
    b = bytearray(f)
    b[H_SHA : H_SHA + 32] = bytes(32)
    b[H_SHA : H_SHA + 32] = hashlib.sha256(bytes(b)).digest()
    return bytes(b)


def variants(good: bytes) -> dict[str, bytes]:
    """Edited copies of the good file; the register section starts at the header's register offset."""
    reg = struct.unpack_from(">I", good, H_REG_OFF)[0]
    out = {}

    def edit(name: str, off: int, val: int) -> None:
        b = bytearray(good)
        struct.pack_into(">I", b, reg + off, val)
        out[name] = resha(bytes(b))

    edit("wrong-device", 8, 2)
    edit("bad-version", 4, 9)
    # first record is the PARAM (0x50), then the MACHINE: type at +16+0x50+8, clone_of +4, knob 0 at +20
    mach = 16 + 0x50 + 8
    edit("type-low", mach, 9)  # a built-in machine's number
    edit("type-high", mach, 24)
    edit("clone-dyn", mach + 4, 16)
    edit("knob-missing-param", mach + 20, 0x8000_0005)
    edit("knob-not-stock", mach + 20, 0x200)
    edit("position", mach + 8, 3)
    return out


def proof(
    image: str = "dt2-1.16", keep: Path | None = None, control: bool = False
) -> dict:
    if image not in IMAGES:
        raise ProofError(f"no registry proof for {image}")
    getter_fn, dispatch_fn = (
        anchor(image, "cf.machines.list_entry"),
        anchor(image, "cf.machines.dispatch"),
    )
    device = image[:3]
    syx_stock = config.stock(DEVICE_SYX[image])
    tools = Tools()
    work = keep or Path(tempfile.mkdtemp(prefix="pk-registry-"))
    work.mkdir(parents=True, exist_ok=True)
    folder = work / "plus_drive"
    mods = folder / "digikit" / "modules"
    mods.mkdir(parents=True, exist_ok=True)
    patched = work / f"{image}-registry.syx"
    plan = patch(tools, syx_stock, patched, "core/runtime,core/dynreg")
    rt = runtime_ranges(plan)
    good = mods / "xslice-d.dkm"
    tools.patchkit_run("pack", "test/dyn-xslice", "--device", device, "-o", good)
    tools.patchkit_run(
        "pack",
        "test/dyn-xslice",
        "--device",
        device,
        "--name",
        "test/dyn-xslice-2",
        "-o",
        mods / "dup.dkm",
    )
    tools.patchkit_run(
        "pack", "test/dyn-params", "--device", device, "-o", mods / "params.dkm"
    )
    pp = bytearray((mods / "params.dkm").read_bytes())
    reg_at = struct.unpack_from(">I", pp, H_REG_OFF)[0]
    struct.pack_into(
        ">I", pp, reg_at + 16 + 8 * 0x50 + 8, 18
    )  # the machine record of test/dyn-params: type 18
    (mods / "params18.dkm").write_bytes(resha(bytes(pp)))
    for name, data in variants(good.read_bytes()).items():
        (mods / f"{name}.dkm").write_bytes(data)
    card = work / f"{device}.img"
    build_card(tools, folder, card, syx_stock, device)

    st = rt["state"]
    o = "core/machines"
    rows, rows_n = sym(plan, o, "machines.rows"), sym(plan, o, "machines.count")
    names, permit, pertype = (
        sym(plan, o, "mf_names"),
        sym(plan, o, "mf_permit"),
        sym(plan, o, "mf_pertype"),
    )
    first = sym(plan, o, "machines.first")
    pf = sym(plan, "core/params", "pf_table")
    pfirst = sym(plan, "core/params", "params.first")
    preg = sym(plan, "core/params", "params.rows")
    reg_state = sym(plan, "core/runtime", "DKRT_REG")
    info = sym(plan, "core/dynreg", "dynreg_info")
    row16 = rows + (FIRST_DYN - first) * ROW
    dp0 = sym(plan, "core/dynreg", "params.dp0.index")
    assert dp0 == pfirst
    ntypes = first + rows_n  # types 0 .. ntypes-1
    code_ranges = {
        owner: [tuple(r) for w in plan["writes"] if w["kind"] == "place" and w["owner"].startswith(owner + "@")
                for r in w.get("code", [])]
        for owner in ("core/runtime", "core/machines", "core/params", "core/dynreg")
    }  # fmt: skip
    watch = [(rows, rows + rows_n * ROW), (names, names + 12 * ntypes), (permit, permit + 4 * ntypes),
             (pertype, pertype + ntypes), (pf + PF * pfirst, pf + PF * (pfirst + 16)),
             (preg, preg + PREG * 16)]  # fmt: skip
    allow = list(code_ranges["core/runtime"])
    env = {
        "DIGI_FLUSH_CHECK": "1",
        "DIGI_WRITE_WATCH": ",".join(f"{a:x}-{b:x}" for a, b in watch),
        "DIGI_WRITE_WATCH_ALLOW_PC": "40000000-40000001" if control else ",".join(f"{a:x}-{b:x}" for a, b in allow),
        "DIGI_WRITE_WATCH_AFTER": "cacr",
        "DIGI_WRITE_WATCH_OUT": str(work / "ww.jsonl"),
    }  # fmt: skip

    def peek(tag: str, addr: int, n: int) -> dict:
        return {"op": "peek", "addr": hex(addr), "len": n, "tag": tag}

    def snapshot(tag: str) -> list[dict]:
        return [
            peek(f"{tag}:rows", rows, rows_n * ROW),
            peek(f"{tag}:names", names, 12 * ntypes),
            peek(f"{tag}:permit", permit, 4 * ntypes),
            peek(f"{tag}:pertype", pertype, ntypes),
            peek(f"{tag}:pf", pf + PF * pfirst, PF * 16),
            peek(f"{tag}:preg", preg, PREG * 16),
            peek(f"{tag}:reg", reg_state, 16),
        ]

    def getter(tag: str) -> list[dict]:
        # lea SRET,a0 ; jsr getter ; rts   (the firmware's list getter returns the vector through a0)
        code = (
            bytes.fromhex("41f9")
            + struct.pack(">I", SRET)
            + bytes.fromhex("4eb9")
            + struct.pack(">I", getter_fn)
            + bytes.fromhex("4e75")
        )
        return [
            {"op": "poke", "addr": hex(STUB), "hex": code.hex(), "tag": f"{tag}:stub"},
            {"op": "call", "pc": hex(STUB), "args": [], "tag": f"{tag}:call"},
            peek(f"{tag}:vec", SRET, 12),
            {
                "op": "peek",
                "addr": hex(SRET),
                "len": 60,
                "ptr": True,
                "tag": f"{tag}:elems",
            },
        ]

    def dispatch(tag: str, t: int) -> list[dict]:
        return [
            {"op": "call", "pc": hex(dispatch_fn), "args": [t], "tag": f"{tag}:call"}
        ]

    script: list[dict] = [{"op": "run", "insns": 30_000_000, "tag": "settle"},
                          peek("info", info, 192), peek("state0", st, 16)]  # fmt: skip
    script += snapshot("s0") + getter("g0") + dispatch("d0", FIRST_DYN)
    refused = ["wrong-device", "bad-version", "type-low", "type-high", "clone-dyn", "knob-missing-param",
               "knob-not-stock", "position"]  # fmt: skip
    for n in refused:
        script += cmd_ops(rt, 1, f"modules/{n}.dkm", n)
    script += snapshot("s1")
    script += cmd_ops(rt, 1, "modules/xslice-d.dkm", "load")
    script += snapshot("s2") + getter("g2") + dispatch("d2", FIRST_DYN)
    script += cmd_ops(rt, 1, "modules/dup.dkm", "dup")
    script += snapshot("s3")
    script += cmd_ops(rt, 2, "test/dyn-xslice", "unload")
    script += snapshot("s4") + getter("g4") + dispatch("d4", FIRST_DYN)
    script += (
        cmd_ops(rt, 1, "modules/xslice-d.dkm", "load2") + snapshot("s5") + getter("g5")
    )
    script += (
        cmd_ops(rt, 2, "test/dyn-xslice", "unload2") + snapshot("s6") + getter("g6")
    )
    # eight parameters: all slots; then one more machine does not fit (9 + 8 > 16 reserved parameter rows)
    script += cmd_ops(rt, 1, "modules/xslice-d.dkm", "l3") + cmd_ops(
        rt, 1, "modules/params.dkm", "lp"
    )
    script += (
        snapshot("s7")
        + getter("g7")
        + cmd_ops(rt, 1, "modules/params18.dkm", "full")
        + snapshot("s8")
    )
    script += (
        cmd_ops(rt, 2, "test/dyn-params", "up")
        + snapshot("s9")
        + cmd_ops(rt, 2, "test/dyn-xslice", "ux")
    )
    script += snapshot("s10") + [peek("pfstock", pf, PF * pfirst)]
    script += [
        {"op": "run", "insns": 5_000_000, "tag": "end"},
        peek("stateE", st, 1256),
    ]
    script += [
        peek("sv", 0x4099CCD4, 12),
        {"op": "peek", "addr": "0x4099ccd4", "len": 64, "ptr": True, "tag": "sv_elems"},
        peek("lb", sym(plan, "core/machines", "mf_list_buf"), 64),
    ]
    out = run_script(
        tools, patched, plan["base"]["syx_sha256"], card, script, env, work
    )
    by = {r["tag"]: r for r in out if r.get("tag")}
    raw = lambda tag: bytes.fromhex(by[tag]["hex"])  # noqa: E731
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def snap(tag: str) -> dict[str, bytes]:
        return {
            k: raw(f"{tag}:{k}")
            for k in ("rows", "names", "permit", "pertype", "pf", "preg")
        }

    def same(a: dict[str, bytes], b: dict[str, bytes], mask: bool = False) -> bool:
        """Equal tables; `mask` ignores the pointers that legitimately differ after a give-back (string
        representations of the free machine row, name pointers of the free parameter rows)."""
        if not mask:
            return a == b
        ra, rb = bytearray(a["rows"]), bytearray(b["rows"])
        pa, pb = bytearray(a["pf"]), bytearray(b["pf"])
        ga, gb = bytearray(a["preg"]), bytearray(b["preg"])
        r0 = (FIRST_DYN - first) * ROW
        for buf in (ra, rb):
            for off in (
                0,
                4,
                56,
                60,
            ):  # long, short, name, short-name pointers of every reserved row
                for k in range(N_DYN):
                    buf[r0 + k * ROW + off : r0 + k * ROW + off + 4] = bytes(4)
        for buf in (pa, pb):
            for k in range(16):
                for off in (40, 48):
                    buf[k * PF + off : k * PF + off + 4] = bytes(4)
        for buf in (ga, gb):
            for k in range(16):
                buf[k * PREG : k * PREG + 8] = bytes(8)
        nm_a, nm_b = bytearray(a["names"]), bytearray(b["names"])
        for buf in (nm_a, nm_b):
            for t in range(FIRST_DYN, ntypes):
                buf[12 * t : 12 * t + 8] = bytes(8)
        return (ra, pa, ga, nm_a, a["permit"], a["pertype"]) == (
            rb,
            pb,
            gb,
            nm_b,
            b["permit"],
            b["pertype"],
        )

    def result(tag: str) -> int:
        return sbe32(raw(f"{tag}:result"), 0)

    def listing(tag: str) -> list[int]:
        """The machine type numbers the firmware's list getter returned."""
        v = raw(f"{tag}:vec")
        n = (be32(v, 4) - be32(v, 0)) // 4
        e = raw(f"{tag}:elems")
        return [be32(e, 4 * i) for i in range(n)]

    # the table of addresses
    inf = raw("info")
    check("dynreg_info present", be32(inf, 0) == 0x44594E52 and be32(inf, 12) == rows and be32(inf, 80) == pf,
          f"magic {be32(inf, 0):#x} rows {be32(inf, 12):#x}")  # fmt: skip
    check("runtime started", be32(raw("state0"), 0) == 0x444B5254)
    # refused files
    want = {"wrong-device": E_REG_DEVICE, "bad-version": E_FORMAT, "type-low": E_TYPE_RANGE, "type-high": E_TYPE_RANGE,
            "clone-dyn": E_TYPE_RANGE, "knob-missing-param": E_FORMAT, "knob-not-stock": E_FORMAT, "position": E_FORMAT}  # fmt: skip
    for n in refused:
        check(
            f"refused: {n}", result(n) == want[n], f"result {result(n)}, want {want[n]}"
        )
    check(
        "refused files wrote nothing to the rows and tables",
        same(snap("s0"), snap("s1")),
    )
    g = {t: listing(t) for t in ("g0", "g2", "g4", "g5", "g6", "g7")}
    check(
        "list getter ran",
        all(by[f"{t}:call"]["ok"] for t in g),
        str({t: by[f"{t}:call"]["ok"] for t in g}),
    )
    check("before: the list is the built-in machines only (no reserved type)", all(t < FIRST_DYN for t in g["g0"]) and len(g["g0"]) == first,
          f"{g['g0']}")  # fmt: skip
    check(
        "after load: the list gains type 16, last",
        g["g2"] == g["g0"] + [FIRST_DYN],
        f"{g['g2']}",
    )
    check(
        "after unload: the list is the start again",
        g["g4"] == g["g0"] and g["g6"] == g["g0"] and g["g5"] == g["g2"],
        f"{g['g4']}",
    )
    check("load ok", result("load") == OK, f"result {result('load')}")
    s0, s2 = snap("s0"), snap("s2")
    r0 = (FIRST_DYN - first) * ROW
    row = s2["rows"][r0 : r0 + ROW]
    check("row 16: clone_of 6, knobs, level", be32(row, 44) == 6 and [be32(row, 8 + 4 * i) for i in (0, 1, 2, 3, 4, 5, 7)] == [0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE]
          and be32(row, 8 + 32) == 10, f"{[hex(be32(row, 8 + 4 * i)) for i in range(9)]}")  # fmt: skip
    check(
        "row 16: knob 6 is the first reserved parameter code",
        be32(row, 8 + 24) == dp0,
        f"{be32(row, 8 + 24):#x}",
    )
    check("only row 16 of the machine rows changed",
          all(s0["rows"][i * ROW : (i + 1) * ROW] == s2["rows"][i * ROW : (i + 1) * ROW] for i in range(rows_n) if i != FIRST_DYN - first),
          "")  # fmt: skip
    check("names, permission and per-type tables: only entry 16 changed",
          all(s0["names"][12 * t : 12 * t + 12] == s2["names"][12 * t : 12 * t + 12] for t in range(ntypes) if t != FIRST_DYN)
          and s0["permit"][:4 * FIRST_DYN] == s2["permit"][:4 * FIRST_DYN] and s0["pertype"][:FIRST_DYN] == s2["pertype"][:FIRST_DYN]
          and s2["permit"][4 * FIRST_DYN : 4 * FIRST_DYN + 4] == s2["permit"][4 * 6 : 4 * 6 + 4]
          and s2["pertype"][FIRST_DYN] == s2["pertype"][6], "")  # fmt: skip
    check(
        "the dispatch descriptor of type 16 is its row",
        by["d2:call"]["d0"] == row16,
        f"{by['d2:call']['d0']:#x} vs {row16:#x}",
    )
    pfr = s2["pf"][:PF]
    check("parameter row 0 written (slot 6: mirror index 33, 0x80 + slot)", be32(pfr, 4) == 25 + 6 + 2 and be32(pfr, 28) == 0x86
          and be32(pfr, 8) == 0 and be32(pfr, 12) == 0x7F00 and be32(pfr, 16) == 0x2000, f"{[hex(be32(pfr, 4 * i)) for i in range(15)]}")  # fmt: skip
    check(
        "parameter registry row: template 0xd1",
        be32(s2["preg"], 8) == 0xD1 and be32(s2["preg"], 12) == 6,
    )
    check(
        "a second file with the same type is refused (E_TYPE_USED)",
        result("dup") == E_TYPE_USED,
        f"{result('dup')}",
    )
    check("the refused duplicate wrote nothing", same(s2, snap("s3")))
    check("unload ok", result("unload") == OK, f"{result('unload')}")
    check(
        "unload gives the rows back (tables equal the start, but for pointers)",
        same(s0, snap("s4"), mask=True),
    )
    check("reload works, second unload works", result("load2") == OK and result("unload2") == OK,
          f"{result('load2')} {result('unload2')}")  # fmt: skip
    check(
        "after the second unload the tables equal the start again",
        same(s0, snap("s6"), mask=True),
    )
    check(
        "the same rows after reload as after the first load",
        snap("s5")["rows"] == s2["rows"] and snap("s5")["pf"] == s2["pf"],
    )
    # eight parameters, one per slot: rows equal the formulas of core/params module.toml (the patch-time form)
    import tomllib  # noqa: E402

    from tools.patchkit.pk_expr import evaluate  # noqa: E402

    man = tomllib.loads((ROOT / "modules/core/params/module.toml").read_text())
    exprs = [
        sec
        for sec in man["section"]
        if sec["name"] == "pf_table" and "dt2" in sec.get("devices", ["dt2"])
    ][0]["append"]["row"]
    stock = raw("pfstock")
    s7 = snap("s7")
    pf7 = s7["pf"]
    ok_rows, bad_rows = 0, []
    for i in range(8):
        code = 1 + i  # the xslice-d parameter took row 0; dyn-params' parameters follow
        got = [be32(pf7, code * PF + 4 * w) for w in range(15)]
        fields = {
            "template": 0xD1,
            "slot": i,
            "min": i,
            "max": 0x7F00 - i * 0x100,
            "default": 0x1000 + i * 0x111,
            "name": 0,
            "short": 0,
        }
        img = lambda a, nb: stock[(a - (pf + 0)) : (a - pf) + nb]  # noqa: E731
        want = [
            evaluate(e, {"cf.params.t1": pf}, fields, img) & 0xFFFFFFFF for e in exprs
        ]
        if [got[w] for w in range(15) if w not in (10, 12)] == [
            want[w] for w in range(15) if w not in (10, 12)
        ]:
            ok_rows += 1
        else:
            bad_rows.append((i, [hex(x) for x in got], [hex(x) for x in want]))
    check(
        "eight parameter rows equal the patch-time formulas of core/params (every slot)",
        ok_rows == 8,
        f"{bad_rows[:1]}",
    )
    mrow17 = s7["rows"][(17 - first) * ROW : (18 - first) * ROW]
    check("machine 17 knobs name the eight parameter codes, in order", [be32(mrow17, 8 + 4 * i) for i in range(8)] == [dp0 + 1 + i for i in range(8)],
          f"{[hex(be32(mrow17, 8 + 4 * i)) for i in range(8)]}")  # fmt: skip
    check(
        "both loaded: list has 16 and 17",
        listing("g7") == g["g0"] + [16, 17],
        f"{listing('g7')}",
    )
    check("a ninth..sixteenth parameter does not fit (E_PARAM_FULL) and writes nothing", result("full") == E_PARAM_FULL and same(s7, snap("s8")),
          f"{result('full')}")  # fmt: skip
    check(
        "unload both: the tables equal the start",
        result("up") == OK and result("ux") == OK and same(s0, snap("s10"), mask=True),
    )
    summary = json.loads((work / "ww.summary.json").read_text())
    # writers
    per_pc = summary.get("per_pc", [])

    def owner_of(pc: int) -> str:
        for name, rs in code_ranges.items():
            if any(a <= pc < b for a, b in rs):
                return name
        return "other"

    by_owner: dict[str, int] = {}
    for p in per_pc:
        ow = owner_of(int(p["pc"], 16))
        by_owner[ow] = by_owner.get(ow, 0) + p["count"]
    foreign = {k: v for k, v in by_owner.items() if k != "core/machines"}
    if control:
        foreign = {}
    if control:
        check(
            "control: with the runtime not allowed, the watch sees its writes",
            by_owner.get("core/runtime", 0) > 0,
            f"{by_owner}",
        )
    else:
        check("write-watch: no write into the rows by anything but the runtime and the machines table builder",
              not foreign, f"{foreign}")  # fmt: skip
    check(
        "flush check: 0 hits",
        summary["flush_hits"] == [],
        f"{summary['flush_hits'][:2]}",
    )
    se = raw("stateE")
    log = [
        (
            se[808 + 28 * i : 808 + 28 * i + 24].split(b"\0")[0].decode("latin-1"),
            be32(se, 808 + 28 * i + 24),
        )
        for i in range(16)
    ]
    return {
        "log": log,
        "image": image, "work": str(work), "ok": all(c[1] for c in checks),
        "checks": [{"name": a, "ok": b, "detail": c} for a, b, c in checks],
        "write_watch": {"events": summary["events"], "by_owner": by_owner, "flush_hits": summary["flush_hits"], "cpushl": summary["cpushl"]},
        "lists": g,
        "static_vector": [hex(be32(raw("sv"), 4 * i)) for i in range(3)],
        "static_elems": [be32(raw("sv_elems"), 4 * i) for i in range(16)],
        "list_buf": [be32(raw("lb"), 4 * i) for i in range(16)],
        "work_dir": str(work),
    }  # fmt: skip


# ---- the panel: the machine list as the firmware draws it, a track, unload -------------------------------------------------------

TRACK_OBJ_WORD = 0x4265338C  # the type byte is the third byte of this word (finding 02)
FRAMES = ROOT / "tests" / "patchkit" / "registry_frames.json"


def ui_proof(
    image: str = "dt2-1.16", keep: Path | None = None, update: bool = False
) -> dict:
    """FUNC+SRC on a coupled daemon: the list gains the machine after the load, YES puts it on track 1 (type byte 16), the
    unload hides it, the track then behaves as on a gap row, and the reload brings the machine back. Frames are pinned in
    tests/patchkit/registry_frames.json (sha256 of the raw OLED bytes)."""

    from tools.control.digi_client import Emu, oled_png  # noqa: E402,F401
    from tools.emulator import card_check  # noqa: E402
    from tools.patchkit import pk_harness as H  # noqa: E402

    device, fw = image[:3], image[4:]
    cell = H.Cell(device, fw, ["core/runtime", "core/dynreg"])
    tools = Tools()
    work = keep or Path(tempfile.mkdtemp(prefix="pk-registry-ui-"))
    work.mkdir(parents=True, exist_ok=True)
    folder = work / "plus_drive"
    mods = folder / "digikit" / "modules"
    mods.mkdir(parents=True, exist_ok=True)
    samples = work / "samples"
    card_check.make_samples(samples)
    for f in samples.iterdir():
        (folder / f.name).write_bytes(f.read_bytes())
    syx_stock = config.stock(DEVICE_SYX[image])
    patched = work / f"{image}-registry.syx"
    plan = patch(tools, syx_stock, patched, "core/runtime,core/dynreg")
    rt = runtime_ranges(plan)
    mb = rt["mailbox"]
    rows = sym(plan, "core/machines", "machines.rows")
    first = sym(plan, "core/machines", "machines.first")
    row16 = rows + (FIRST_DYN - first) * ROW
    tools.patchkit_run(
        "pack", "test/dyn-xslice", "--device", device, "-o", mods / "xslice-d.dkm"
    )

    class UiSession(H.Session):
        def open(
            self, image=None
        ):  # same as Session.open, but the card is built from the plus_drive folder
            self.syx, self.ready = H.firmware_inputs(self.cell)
            exe = H.daemon_binary(self.cell.device, self.syx)
            run_dir = H.OUT / f"run-{self.run_name}"
            self.emu = Emu.spawn_coupled(
                self.syx,
                self.ready,
                timeout=300,
                binary=exe,
                run_dir=run_dir,
                env=self.env,
            )
            self.emu.subscribe()
            self.card = self.emu.call("card.load", dir=str(folder))["card"]
            self.load(image)

    goldens = H.Goldens(FRAMES)
    s = UiSession(cell, goldens, update=update, run_name=f"registry-ui-{image}")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def tword(addr: int) -> int:
        return s.emu.peek32(addr) or 0

    def mailbox(cmd: int, arg: str) -> int:
        b = arg.encode() + b"\0"
        b += bytes(-len(b) % 4)
        for i in range(0, len(b), 4):
            s.emu.poke32(mb + 12 + i, struct.unpack(">I", b[i : i + 4])[0])
        seq0 = tword(mb + 4)
        s.emu.poke32(mb, cmd)
        for _ in range(100):
            s.run_ms(50)
            if tword(mb) == 0 and tword(mb + 4) != seq0:
                break
        r = tword(mb + 8)
        return r - (1 << 32) if r >= 1 << 31 else r

    def type_now() -> int:
        return (tword(TRACK_OBJ_WORD) >> 8) & 0xFF

    def row_name() -> int:
        return tword(row16 + 56)

    def open_list(downs: int) -> None:
        s.do_tap(["FUNC", "SRC"])
        s.run_ms(250)
        for _ in range(downs):
            s.do_tap("DOWN")
            s.run_ms(80)

    def close_list(commit: bool) -> None:
        s.do_tap("YES" if commit else "NO")
        s.run_ms(300)
        if commit:
            s.do_tap("YES")
            s.run_ms(300)

    def fresh() -> None:
        s.reset()
        s.run_ms(1500)

    try:
        s.open(patched)
        s.segment = "ui"
        fresh()
        stock_type = type_now()
        open_list(7)
        s.expect_frame("list-before-load")
        close_list(False)
        check("start: track 1 is type 0", stock_type == 0, f"{stock_type}")
        r = mailbox(1, "modules/xslice-d.dkm")
        check("load through the mailbox", r == 0, f"{r}")
        check(
            "row 16 published (name pointer set)",
            tword(tword(row16 + 56)) >> 24 == ord("X"),
            f"{tword(row16 + 56):#x}",
        )
        s.run_ms(200)
        open_list(7)
        s.expect_frame("list-with-xslice-d", differs_from="list-before-load")
        close_list(True)
        check(
            "YES selects it: track 1 has type 16",
            type_now() == FIRST_DYN,
            f"{type_now()}",
        )
        s.run_ms(300)
        s.expect_frame("src-page-xslice-d")
        s.do_hold("TRIG 1", 300)
        s.run_ms(500)
        s.health("audition of the run-time machine")
        # unload while a track has the machine
        r = mailbox(2, "test/dyn-xslice")
        check("unload through the mailbox", r == 0, f"{r}")
        check(
            "row 16 free again (empty name), clone of type 0",
            tword(tword(row16 + 56)) >> 24 == 0 and tword(row16 + 44) == 0,
        )
        check(
            "the track keeps type 16 (a project would store it)",
            type_now() == FIRST_DYN,
            f"{type_now()}",
        )
        s.run_ms(300)
        s.health("after unload")
        s.expect_frame("src-page-after-unload")
        open_list(6)
        s.expect_frame("list-after-unload")
        close_list(False)
        # a gap row for comparison: type 11 was never filled
        word = tword(TRACK_OBJ_WORD)
        s.emu.poke32(TRACK_OBJ_WORD, (word & ~0xFF00) | (11 << 8))
        s.run_ms(300)
        s.health("track on a gap row")
        s.expect_frame("src-page-gap-row")
        check("the unloaded machine looks like a gap row on the SRC page", s.frames.get("ui/src-page-after-unload") == s.frames.get("ui/src-page-gap-row"),
              f"{s.frames.get('ui/src-page-after-unload', '')[:12]} {s.frames.get('ui/src-page-gap-row', '')[:12]}")  # fmt: skip
        # load again: the machine is back, the track with type 16 uses it
        s.emu.poke32(TRACK_OBJ_WORD, (word & ~0xFF00) | (FIRST_DYN << 8))
        r = mailbox(1, "modules/xslice-d.dkm")
        check("reload", r == 0, f"{r}")
        s.run_ms(300)
        s.expect_frame("src-page-reloaded")
        check("after the reload the SRC page is the machine's again", s.frames.get("ui/src-page-reloaded") == s.frames.get("ui/src-page-xslice-d"),
              f"{s.frames.get('ui/src-page-reloaded', '')[:12]} {s.frames.get('ui/src-page-xslice-d', '')[:12]}")  # fmt: skip
        s.health("end")
        for c in s.checks:
            checks.append((f"harness/{c.name}", c.ok, c.detail))
    finally:
        s.close()
        if update:
            goldens.save()
    return {"image": image, "work": str(work), "ok": all(c[1] for c in checks),
            "checks": [{"name": a, "ok": b, "detail": c} for a, b, c in checks],
            "frames": s.frames, "frame_dir": str(H.OUT / "frames" / cell.slug)}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("proof")
    p.add_argument("--image", default="dt2-1.16")
    p.add_argument("--keep", type=Path)
    p.add_argument("--control", action="store_true")
    u = sub.add_parser("ui", help="the panel: list, select, unload (coupled daemon)")
    u.add_argument("--image", default="dt2-1.16")
    u.add_argument("--keep", type=Path)
    u.add_argument("--update", action="store_true", help="record the frame hashes")
    a = ap.parse_args(argv)
    if a.cmd == "ui":
        rep = ui_proof(a.image, a.keep, a.update)
        print(json.dumps(rep, indent=1))
        return 0 if rep["ok"] else 1
    rep = proof(a.image, a.keep, a.control)
    print(json.dumps(rep, indent=1))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
