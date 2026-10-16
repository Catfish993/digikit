#!/usr/bin/env python3
"""Emulator proof of SETTINGS > DIGIKIT (modules/core/digikit-menu, docs/findings/28-digikit-menu.md).

    uv run python -m tools.coldfire.digikit_menu_proof run [--image dt2-1.16] [--update] [--jobs N] [--regress]

Patches the stock firmware with core/digikit-menu (and test/digikit-rt-stub for the runtime-present runs), builds a card
from a `plus_drive`-style folder with `digikit/modules/a.dkm` and `b.dkm`, boots in the native emulator (regions_probe)
and presses the panel keys of tools/coldfire/panel_script.py. Judged per run:

  * the firmware's stores and DMA never touch the module RAM `cf.expansion`, except the module's own code (the
    write-watch with ALLOW_PC = the module; the module's read buffers come from the firmware heap), and no flush hit
    (DIGI_FLUSH_CHECK=1);
  * the OLED frames named by the script have the SHA-256 pinned in tests/golden/digikit_menu_frames.json
    (`--update` writes them after a look at the PNGs);
  * the module's state after the run, read back with `--peek`: the store was read (STORE=1, two entries), the ticks
    the keys made, the text of the last LOAD answer, and with the stand-in runtime the paths it was given;
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
STUB = "test/digikit-rt-stub"
DKRT_MAGIC = 0x444B5254


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
    """A card with digikit/modules/a.dkm and b.dkm (any bytes) and digikit/readme.txt (not a module)."""
    dev, fw = DEVICE[image]
    folder = CARDS / dev
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "digikit" / "modules").mkdir(parents=True)
    (folder / "digikit" / "modules" / "a.dkm").write_bytes(b"dummy module a\n")
    (folder / "digikit" / "modules" / "b.dkm").write_bytes(b"dummy module b, longer\n")
    (folder / "digikit" / "readme.txt").write_bytes(b"readme")
    img = CARDS / f"{dev}.img"
    img.unlink(missing_ok=True)
    sh(
        ["cargo", "run", "--release", "--locked", "--offline", "-p", "plusdrive-format", "--bin",
         "plusdrive", "--", "build", str(folder), "-o", str(img), "--syx", f"firmware/{fw}", "--device", dev]
    )  # fmt: skip
    return img


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


def run_one(image, name, script, syx, plan, card, goldens, update, peeks):
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
        extra=extra,
    )
    facts["name"] = name
    facts["bad"] = pin(image, name, facts, goldens, update)
    if not facts["pass"]:
        facts["bad"].append(
            "run failed: errors %s write events %s flush hits %s"
            % (facts["errors"], facts["write_events"], facts["flush_hits"])
        )
    return facts, out


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
    ap.add_argument("--only-regress", action="store_true", help="skip the DIGIKIT runs")
    a = ap.parse_args(argv)
    image = a.image
    syx, plan_path = patch(image, [MENU], f"dkmenu-{image}")
    syx_rt, plan_rt_path = patch(image, [MENU, STUB], f"dkmenu-rt-{image}")
    plan, plan_rt = Plan(plan_path), Plan(plan_rt_path)
    card = make_card(image)
    goldens = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    api = plan_rt.addr("DKRT_API")
    pokes = [
        (api + 4, 1), (api + 8, plan_rt.addr("stub_load")), (api + 16, plan_rt.addr("stub_status")),
        (api, DKRT_MAGIC),
    ]  # fmt: skip
    state = [plan.addr(n) for n in ("NENT", "STORE", "TICK", "RESULT")]
    state_rt = [
        plan_rt.addr(n)
        for n in ("NENT", "STORE", "TICK", "RESULT", "stub_count", "stub_log")
    ]
    jobs = [
        ("digikit_main", panel_script.SCRIPTS["digikit_main"](), syx, plan_path, plan, state),
        ("digikit_load", panel_script.digikit_load_script(), syx, plan_path, plan, state),
        ("digikit_about", panel_script.digikit_about_script(), syx, plan_path, plan, state),
        ("digikit_load_rt", panel_script.digikit_load_script(pokes), syx_rt, plan_rt_path, plan_rt, state_rt),
        ("digikit_about_rt", panel_script.digikit_about_script(pokes), syx_rt, plan_rt_path, plan_rt, state_rt),
    ]  # fmt: skip
    from concurrent.futures import ThreadPoolExecutor

    def go(j):
        name, script, s, pp, pl, st = j
        peeks = []
        for sym_addr in st:
            peeks += [sym_addr + 4 * k for k in range(0, 12)]
        return (j, *run_one(image, name, script, s, pp, card, goldens, a.update, peeks))

    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        results = [] if a.only_regress else list(pool.map(go, jobs))
    ok = True
    for j, facts, out in results:
        name, script, _s, _pp, pl, st = j
        bad = list(facts["bad"])
        nent = peek(out, pl.addr("NENT"))
        store = peek(out, pl.addr("STORE"))
        tick = peek_bytes(out, pl.addr("TICK"), 8)
        result = cstr(peek_bytes(out, pl.addr("RESULT"), 40))
        info = {
            "entries": nent,
            "store": store,
            "ticks": list(tick[:2]),
            "result": result,
        }
        if name != "digikit_main" and "about" not in name:
            if nent != 2 or store != 1:
                bad.append(f"store read: entries {nent}, state {store}")
            want_ticks = [0, 1]  # a ticked, b ticked, a unticked
            if list(tick[:2]) != want_ticks:
                bad.append(f"ticks {list(tick[:2])}, expected {want_ticks}")
            want_result = "LOADED 1 FAILED 0" if name.endswith("_rt") else "NO RUNTIME"
            if result != want_result:
                bad.append(f"last LOAD answer {result!r}, expected {want_result!r}")
        if name.endswith("_rt") and "load" in name:
            count = peek(out, pl.addr("stub_count"))
            log = peek_bytes(out, pl.addr("stub_log"), 48)
            paths = [cstr(log[16 * k : 16 * k + 16]) for k in range(3)]
            info.update(stub_calls=count, stub_paths=paths)
            if count != 3 or paths != [
                "modules/a.dkm",
                "modules/b.dkm",
                "modules/b.dkm",
            ]:
                bad.append(f"runtime stand-in saw {count} calls {paths}")
        shots(image, name, out, [f for f, _ in script.frames])
        print(f"{name:18s} {'PASS' if not bad else 'FAIL'}  {info}")
        for b in bad:
            print("    " + b)
        ok &= not bad
    if a.update:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(dict(sorted(goldens.items())), indent=1) + "\n")
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
