#!/usr/bin/env python3
"""Emulator proof of SETTINGS > DIGIKIT (modules/core/digikit-menu, docs/findings/28-digikit-menu.md).

    uv run python -m tools.coldfire.digikit_menu_proof run [--image dt2-1.16] [--update] [--jobs N] [--regress] [--only NAMES]

Patches the stock firmware with core/digikit-menu (which requires core/runtime), builds a card from a `plus_drive`-style
folder with `digikit/modules/dyn-demo.dkm` (packed by `patchkit pack`), `bad-sha.dkm` and `bad-import.dkm`, boots in the
native emulator (regions_probe) and presses the panel keys of tools/coldfire/panel_script.py. The gate script ticks dyn-demo,
LOADs it, reads its counter rising (`--peek-at`), shows ABOUT with one module loaded, comes back to the list (the row is marked
loaded), unticks and LOADs again (unload), reads the counter stopped and the marker `fini` left, shows ABOUT with none. Judged
per run:

  * the firmware's stores and DMA never touch the module RAM `cf.expansion`, except the module's own code (the
    write-watch with ALLOW_PC = the module; the module's read buffers come from the firmware heap), and no flush hit
    (DIGI_FLUSH_CHECK=1);
  * the OLED frames named by the script have the SHA-256 pinned in tests/golden/digikit_menu_frames.json
    (`--update` writes them after a look at the PNGs);
  * the menu's state after the run, read back with `--peek`: the store was read (STORE=1, three entries), the ticks and the
    loaded flags, the text of the last LOAD answer; the gate's counter samples;
  * `--regress` also runs the stock menu scripts of finding 25 on the patched image against their pinned frames.

PNGs of the DIGIKIT screens go to out/runs/lanes/r1-shots/ (a contact sheet per script).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys

from tools import python_env
from tools.coldfire import arena_safety, panel_script

ROOT = pathlib.Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "digikit_menu_frames.json"
OUT = ROOT / "out" / "runs" / "lanes" / "r1-run"
SHOTS = ROOT / "out" / "runs" / "lanes" / "r1-shots"
CARDS = ROOT / "out" / "runs" / "lanes" / "r1-card"

DEVICE = {
    "dt2-1.16": ("dt2", "Digitakt_II_OS1.16.syx"),
    "dt2-1.17": ("dt2", "Digitakt_II_OS1.17.syx"),
    "dn2-1.11": ("dn2", "Digitone_II_OS1.11.syx"),
    "dn2-1.12": ("dn2", "Digitone_II_OS1.12.syx"),
}
MENU = "core/digikit-menu"
RUNTIME = "core/runtime"
DYN_MAGIC = 0x44594E4D  # "DYNM"
FINI_MARK = 0xF1F10001


REGRESS_SCRIPTS = [
    "save_project", "save_quick", "load_project", "new_project", "manage_delete", "manage_rename", "format_pool",
    "os_update", "check_filesystem", "pattern_temp",
]  # fmt: skip


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(
        cmd, cwd=ROOT, env=python_env(), capture_output=True, text=True, **kw
    )
    if r.returncode:
        raise SystemExit(
            "command failed: %s\n%s%s"
            % (" ".join(cmd), r.stdout[-2000:], r.stderr[-2000:])
        )
    return r


def patch(
    image: str, modules: list[str], name: str
) -> tuple[pathlib.Path, pathlib.Path]:
    _, fw = DEVICE[image]
    syx = ROOT / "out" / "patch" / f"{name}.syx"
    sh(
        ["uv", "run", "python", "-m", "tools.patchkit.patchkit", "patch", "--firmware",
         f"firmware/{fw}", "--modules", ",".join(modules), "-o", str(syx.relative_to(ROOT))]
    )  # fmt: skip
    return syx, syx.with_suffix(".plan.json")


def make_card(image: str) -> pathlib.Path:
    """A card with digikit/modules/{bad-import,bad-sha,dyn-demo}.dkm and digikit/readme.txt (not a module)."""
    import tempfile

    from tools.patchkit import pk_dkrt

    dev, fw = DEVICE[image]
    folder = CARDS / dev
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "digikit" / "modules").mkdir(parents=True)
    tools = pk_dkrt.Tools()
    pk_dkrt.pack(
        tools, "test/dyn-demo", folder / "digikit" / "modules" / "dyn-demo.dkm"
    )
    with tempfile.TemporaryDirectory(prefix="dkmenu-") as work:
        pk_dkrt.broken_variants(
            pathlib.Path(work), tools, "test/dyn-demo", folder / "digikit"
        )
    good = (folder / "digikit" / "modules" / "dyn-demo.dkm").read_bytes()
    other = (
        2 if dev == "dt2" else 1
    )  # the other device's id (dkrt-sdk DEVICE_DT2 = 1, DEVICE_DN2 = 2)
    (folder / "digikit" / "modules" / "foreign.dkm").write_bytes(
        forge(good, device=other)
    )
    (folder / "digikit" / "modules" / "future.dkm").write_bytes(
        forge(good, runtime_min=99)
    )
    (folder / "digikit" / "readme.txt").write_bytes(b"readme")
    img = CARDS / f"{dev}.img"
    img.unlink(missing_ok=True)
    sh(
        ["cargo", "run", "--release", "--locked", "--offline", "-p", "plusdrive-format", "--bin",
         "plusdrive", "--", "build", str(folder), "-o", str(img), "--syx", f"firmware/{fw}", "--device", dev]
    )  # fmt: skip
    return img


def forge(
    good: bytes, device: int | None = None, runtime_min: int | None = None
) -> bytes:
    """A copy of a good `.dkm` with a header field changed and the file sha256 made right again: register records for another
    device (an empty `DKRG` record, 16 bytes appended) or a higher `runtime_min`. The menu reads only these header fields."""
    import hashlib
    import struct

    b = bytearray(good)
    if device is not None:
        b += b"\0" * (-len(b) % 16)
        struct.pack_into(">I", b, 0x90, len(b))
        struct.pack_into(">I", b, 0x94, 16)
        b += struct.pack(">4sIII", b"DKRG", 1, device, 0)
    if runtime_min is not None:
        struct.pack_into(">I", b, 0x30, runtime_min)
    struct.pack_into(">I", b, 0x08, len(b))
    b[0x10:0x30] = bytes(32)
    b[0x10:0x30] = hashlib.sha256(bytes(b)).digest()
    return bytes(b)


class Plan:
    def __init__(self, path: pathlib.Path) -> None:
        self.syms = json.loads(path.read_text())["symbols"]

    def addr(self, name: str) -> int:
        """A module symbol by its exact name or, for a Rust static, by the end of its mangled name."""
        for k, v in self.syms.items():
            if k.endswith(":" + name) or re.search(r"\d%s$" % re.escape(name), k):
                return v
        raise KeyError(name)


def peek(facts_dir: pathlib.Path, addr: int) -> int | None:
    for line in (facts_dir / "probe.stdout").read_text().splitlines():
        m = re.match(r'\{"peek":"(0x[0-9a-f]+)","value":(?:Some\((\d+)\)|None)\}', line)
        if m and int(m.group(1), 16) == addr:
            return None if m.group(2) is None else int(m.group(2))
    return None


def peek_ats(facts_dir: pathlib.Path) -> list[int | None]:
    """The values of the `--peek-at` reads, in time order."""
    out = []
    for line in (facts_dir / "probe.stdout").read_text().splitlines():
        m = re.match(
            r'\{"peek_at":"0x[0-9a-f]+","t":\d+,"value":(?:Some\((\d+)\)|None)\}', line
        )
        if m:
            out.append(None if m.group(1) is None else int(m.group(1)))
    return out


def peek_ats_bytes(facts_dir: pathlib.Path) -> bytes:
    """The longwords of the `--peek-at` reads of a text, in time order, as bytes (digikit_errors reads the first popup)."""
    return b"".join((v or 0).to_bytes(4, "big") for v in peek_ats(facts_dir))


def peek_bytes(d: pathlib.Path, addr: int, n: int) -> bytes:
    out = b""
    for k in range(0, n, 4):
        v = peek(d, addr + k)
        out += (v or 0).to_bytes(4, "big")
    return out[:n]


def cstr(b: bytes) -> str:
    return b.split(b"\0")[0].decode("latin1")


def pin(image: str, script: str, facts: dict, goldens: dict, update: bool) -> list[str]:
    bad = []
    for f, sha in facts["frame_sha256"].items():
        key = f"{image}/{script}/{f}"
        if update:
            goldens[key] = sha
        elif goldens.get(key) != sha:
            bad.append(
                f"frame {f}: {sha[:12]} is not the pinned {str(goldens.get(key))[:12]}"
            )
    return bad


def shots(image: str, script: str, d: pathlib.Path, frames: list[str]) -> None:
    from PIL import Image

    SHOTS.mkdir(parents=True, exist_ok=True)
    ims = []
    for f in frames:
        p = d / f"{f}.png"
        if p.exists():
            shutil.copy(p, SHOTS / f"{image}-{script}-{f}.png")
            ims.append(Image.open(p))
    if ims:
        w, h = ims[0].size
        sheet = Image.new("RGB", (w * 2, h * ((len(ims) + 1) // 2)))
        for i, im in enumerate(ims):
            sheet.paste(im, ((i % 2) * w, (i // 2) * h))
        sheet.save(SHOTS / f"{image}-{script}-sheet.png")


def stack_env(image: str, plan: Plan) -> dict[str, str]:
    """DIGI_STACK_WATCH on the UI task's stack, and epochs ended by the menu's MARK words (the start and the end of a LOAD or UNLOAD)."""
    from tools.coldfire import task_stacks

    _, ts, pop = task_stacks.tasks(image)
    ui = task_stacks.ui_task(ts, pop)
    mark = plan.addr("MARK")
    return {
        "DIGI_STACK_WATCH": "0x%x-0x%x" % (ui.base, ui.top),
        "DIGI_STACK_WATCH_EPOCH": "0x%x+8" % mark,
    }


def run_one(
    image,
    name,
    script,
    syx,
    plan,
    card,
    goldens,
    update,
    peeks,
    watch=None,
    env_extra=None,
):
    out = OUT / image / name
    extra = []
    for a in peeks:
        extra += ["--peek", hex(a)]
    facts = arena_safety.run(
        image,
        script,
        out,
        card=card,
        syx=syx,
        allow_pc=arena_safety.plan_allow_pc(plan),
        watch=watch,
        extra=extra,
        env_extra=env_extra,
    )
    facts["name"] = name
    facts["bad"] = pin(image, name, facts, goldens, update)
    if not facts["pass"]:
        facts["bad"].append(
            "run failed: errors %s write events %s flush hits %s"
            % (facts["errors"], facts["write_events"], facts["flush_hits"])
        )
    return facts, out


def stack_summary(facts: dict, mark: int) -> dict:
    """The UI stack's high-water by phase: the whole run, the epochs ended by the end-of-request MARK word (a LOAD or UNLOAD
    request: the menu action, dkrt_load or dkrt_unload and the popup), and every other epoch (idle, navigation, rebuilds)."""
    st = facts["stacks"][0]
    end = "%#010x" % (mark + 4)
    req = [e for e in facts["stack_epochs"] if e["trigger"] == end]
    rest = [e for e in facts["stack_epochs"] if e["trigger"] != end]
    return {
        "size": st["size"],
        "total": st["depth"],
        "total_pc": st["pc"],
        "request": max((e["depth"] for e in req), default=None),
        "request_pc": max(req, key=lambda e: e["depth"])["pc"] if req else None,
        "other": max((e["depth"] for e in rest), default=None),
        "other_pc": max(rest, key=lambda e: e["depth"])["pc"] if rest else None,
        "requests": len(req),
    }


def report_stack(name: str, facts: dict, mark: int) -> None:
    sm = stack_summary(facts, mark)
    print(
        f"  stack of {sm['size']} bytes: deepest {sm['total']} (pc {sm['total_pc']}); "
        f"outside requests {sm['other']} (pc {sm['other_pc']}); "
        f"in {sm['requests']} requests {sm['request']} (pc {sm['request_pc']}); "
        f"margin {sm['size'] - sm['total']}"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--image", default="dt2-1.16")
    ap.add_argument("--update", action="store_true", help="pin the frames of this run")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument(
        "--regress",
        action="store_true",
        help="also the stock menu scripts, stock against patched image",
    )
    ap.add_argument(
        "--stack",
        action="store_true",
        help="also measure the UI task's stack high-water (DIGI_STACK_WATCH)",
    )
    ap.add_argument(
        "--quick",
        action="store_true",
        help="the quick tier: only digikit_errors and digikit_gate, no regression scripts",
    )
    ap.add_argument(
        "--stack-stock",
        action="store_true",
        help="the UI stack high-water of the stock SETTINGS and project scripts (save, load, new, manage, format) on the patched image",
    )
    ap.add_argument("--only", help="comma-separated script names to run (default: all)")
    ap.add_argument("--only-regress", action="store_true", help="skip the DIGIKIT runs")
    a = ap.parse_args(argv)
    image = a.image
    syx, plan_path = patch(image, [MENU], f"dkmenu-{image}")
    plan = Plan(plan_path)
    card = make_card(image)
    goldens = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if a.update and not a.only_regress:
        goldens = {k: v for k, v in goldens.items() if not k.startswith(image + "/")}
    state = [
        plan.addr(n)
        for n in (
            "NENT",
            "STORE",
            "TICK",
            "RESULT",
            "LOADED",
            "RESULT2",
            "POPUP",
            "POPUPS",
            "NOK",
        )
    ]
    st = plan.addr("DKRT_STATE")
    # the whole of cf.expansion except the runtime's staging buffer: the card read lands there by DMA, by design
    lo, hi = arena_safety.expansion(image)
    stage = plan.addr("DKRT_STAGE")
    watch = "0x%x-0x%x,0x%x-0x%x" % (lo, stage, stage + 64 * 1024, hi)
    runtime_state = [st + 4 * k for k in range(0, 12)]
    jobs = [
        ("digikit_main", panel_script.SCRIPTS["digikit_main"]()),
        ("digikit_about", panel_script.digikit_about_script()),
        ("digikit_errors", panel_script.digikit_errors_script(plan.addr("POPUP"))),
        ("digikit_gate", panel_script.digikit_gate_script(st)),
    ]
    if a.quick:
        jobs = [j for j in jobs if j[0] in ("digikit_errors", "digikit_gate")]
    if a.only:
        jobs = [j for j in jobs if j[0] in a.only.split(",")]
    from concurrent.futures import ThreadPoolExecutor

    def go(j):
        name, script = j
        peeks = []
        for sym_addr in state:
            peeks += [sym_addr + 4 * k for k in range(0, 12)]
        peeks += runtime_state
        return (
            j,
            *run_one(
                image,
                name,
                script,
                syx,
                plan_path,
                card,
                goldens,
                a.update,
                peeks,
                watch,
                stack_env(image, plan) if a.stack else None,
            ),
        )

    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        results = [] if a.only_regress else list(pool.map(go, jobs))
    ok = True
    for j, facts, out in results:
        name, script = j
        bad = list(facts["bad"])
        nent = peek(out, plan.addr("NENT"))
        store = peek(out, plan.addr("STORE"))
        tick = list(peek_bytes(out, plan.addr("TICK"), 8)[:5])
        loaded = list(peek_bytes(out, plan.addr("LOADED"), 8)[:5])
        result = cstr(peek_bytes(out, plan.addr("RESULT"), 48))
        popup = cstr(peek_bytes(out, plan.addr("POPUP"), 48))
        popups = peek(out, plan.addr("POPUPS"))
        info = {
            "entries": nent,
            "store": store,
            "ticks": tick,
            "loaded": loaded,
            "result": result,
            "popup": popup,
            "popups": popups,
        }
        if name != "digikit_main" and "about" not in name and (nent != 5 or store != 1):
            bad.append(f"store read: entries {nent}, state {store}")
        want = {
            # (ticks, loaded, last answer) after the script
            "digikit_errors": ([0, 1, 0, 0, 0], [0] * 5, "NOTHING TO DO"),
            "digikit_gate": ([0] * 5, [0] * 5, "UNLOADED 1"),
        }.get(name)
        if want and (tick, loaded, result) != want:
            bad.append(f"final state {(tick, loaded, result)}, expected {want}")
        if name == "digikit_errors":
            # the firmware's message popup was given the file name and the reason, once per failed LOAD
            first = cstr(peek_ats_bytes(out))
            nok = list(peek_bytes(out, plan.addr("NOK"), 8)[:5])
            info["not_ok"] = nok
            if nok != [0, 0, 0, 1, 1]:
                bad.append(f"greyed rows {nok}, expected foreign and future")
            info["first_popup"] = first
            if popups != 2 or popup != "BAD-SHA.DKM: BAD SHA":
                bad.append(f"popup: {popups} shown, last {popup!r}")
            if first != "BAD-IMPORT.DKM: NO IMPORT":
                bad.append(f"first popup {first!r}")
        if name == "digikit_gate" and popups != 0:
            bad.append(f"popup shown by a good load: {popup!r}")
        if name == "digikit_gate":
            n = peek_ats(out)
            info["counter"] = n
            if len(n) != 8 or None in n:
                bad.append(f"counter samples {n}")
            else:
                if n[0] != DYN_MAGIC:
                    bad.append(f"counter magic {n[0]:#x}")
                if not n[1] < n[2] < n[3]:
                    bad.append(f"counter does not rise while loaded: {n[1:4]}")
                if (
                    n[4] != n[6]
                    or n[4] < n[3]
                    or n[5] != FINI_MARK
                    or n[7] != FINI_MARK
                ):
                    bad.append(f"counter after unload {n[4:8]}")
            loads = peek(out, st + 16)
            slot0 = peek(out, st + 40)
            info.update(loads=loads, slot0=slot0)
            if loads != 1 or slot0 != 0:
                bad.append(f"runtime state: loads {loads}, slot 0 {slot0:#x}")
        if a.stack:
            report_stack(name, facts, plan.addr("MARK"))
        shots(image, name, out, [f for f, _ in script.frames])
        print(f"{name:18s} {'PASS' if not bad else 'FAIL'}  {info}")
        for b in bad:
            print("    " + b)
        ok &= not bad
    if a.update:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(dict(sorted(goldens.items())), indent=1) + "\n")
    if a.stack_stock:
        env = stack_env(image, plan)
        env.pop("DIGI_STACK_WATCH_EPOCH")
        names = [
            n
            for n in REGRESS_SCRIPTS
            if n != "check_filesystem" or image.startswith("dt2")
        ]
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            runs = list(
                pool.map(
                    lambda n: arena_safety.run(
                        image,
                        n,
                        OUT / image / ("stack-" + n),
                        card=card,
                        syx=syx,
                        allow_pc=arena_safety.plan_allow_pc(plan_path),
                        env_extra=env,
                    ),  # fmt: skip
                    names,
                )
            )
        for n, f in zip(names, runs, strict=True):
            m = f["stacks"][0]
            print(
                f"stack {n:16s} deepest {m['depth']} of {m['size']} bytes (pc {m['pc']})"
            )
    if a.regress:
        # the stock SETTINGS scripts of finding 25 (project, system and format menus, saves): the same card, stock image
        # against patched image; every dumped frame and the card sectors the firmware wrote must be equal
        names = [
            n
            for n in REGRESS_SCRIPTS
            if n != "check_filesystem" or image.startswith("dt2")
        ]
        stock = arena_safety.suite(
            image, names, OUT / image / "regress-stock", card, a.jobs, False
        )
        patched = arena_safety.suite(
            image, names, OUT / image / "regress-patched", card, a.jobs, False,
            run_kw={"syx": syx, "allow_pc": arena_safety.plan_allow_pc(plan_path)},
        )  # fmt: skip
        for st, pa in zip(stock, patched, strict=True):
            same_frames = st["frame_sha256"] == pa["frame_sha256"]
            same_card = st["card_ranges"] == pa["card_ranges"]
            good = (
                same_frames
                and same_card
                and pa["write_events"] == 0
                and not pa["flush_hits"]
                and not pa["errors"]
            )
            print(
                "regress %-16s %s  frames %d equal=%s  card ranges equal=%s  writes %s flush %s"
                % (st["script"], "PASS" if good else "FAIL", len(pa["frame_sha256"]), same_frames, same_card,
                   pa["write_events"], pa["flush_hits"])
            )  # fmt: skip
            ok &= good
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
