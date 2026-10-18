#!/usr/bin/env python3
"""Front end for the patchkit command line (packages/patchkit): builds it when stale and runs it from the repo root.

    uv run python -m tools.patchkit.patchkit bin                       print the path of the built binary
    uv run python -m tools.patchkit.patchkit patch --firmware X.syx --modules a,b [-o OUT.syx]   apply; writes out/patch/
    uv run python -m tools.patchkit.patchkit explain --firmware X.syx --modules a,b              site-by-site account
    uv run python -m tools.patchkit.patchkit list|inspect|check|verify|gen ...                     passed through
    uv run python -m tools.patchkit.patchkit pack MODULE --device dt2|dn2 [-o OUT.dkm]  pack a run-time module (default out/patch/<name>-<device>.dkm)
    uv run python -m tools.patchkit.patchkit bundle [-o FILE] [--modules-dir DIR]  ports + modules as one JSON (what patchkit.wasm reads)
    uv run python -m tools.patchkit.patchkit inject [OUT.syx] [--hot]  load a patched image into the running emulator

`patch` defaults the output to out/patch/<firmware stem>+<modules>.syx and writes plan.json next to it. `explain`
adds a SHARC+ listing of the DSP sites to the report of the Rust binary. Output files are firmware-derived: they stay
under out/ and are never committed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

from tools.emulator import native_target  # noqa: E402


def target_dir() -> Path:
    # patchkit has no generated SHARC core: the "no core" key keeps its build away from sharc-gen's
    return Path(native_target.target_dir(None)).parent / "nogen"


_BUILT: Path | None = None


def binary(build: bool = True) -> Path:
    """The patchkit binary to run: an immutable copy (tools/devtools/stablebin.py), never cargo's own
    `release/patchkit`, which every `cargo build` rewrites (a process that execs it meanwhile can hang)."""
    global _BUILT
    from tools.devtools import stablebin

    target = target_dir()
    exe = target / "release" / "patchkit"
    if build and _BUILT is not None:
        return _BUILT
    if not build and not exe.is_file():
        return exe
    with stablebin.build_lock(target):
        if build:
            r = subprocess.run(
                [
                    "cargo",
                    "build",
                    "-p",
                    "patchkit",
                    "--release",
                    "--offline",
                    "--target-dir",
                    str(target),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if r.returncode:
                raise SystemExit(f"cannot build patchkit:\n{r.stderr[-3000:]}")
        stable = stablebin.stable_copy(exe, target)
    if build:
        _BUILT = stable
    return stable


def run(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary()), "--root", str(ROOT), *args],
        cwd=ROOT,
        capture_output=capture,
        text=True,
    )


def flag(args: list[str], name: str) -> str | None:
    return (
        args[args.index(name) + 1]
        if name in args and args.index(name) + 1 < len(args)
        else None
    )


GALLERY = "build/gen/gallery.json"


def bundle(extra: list[Path] | None = None) -> dict:
    """Ports and modules as texts: the input of `pk_apply` in patchkit.wasm.

    Only committed modules/ by default; `extra` roots (e.g. out/modules for generated
    oracles) are for tests, never for the published page.
    """
    ports = [p.read_text() for p in sorted((ROOT / "devices" / "ports").glob("*.toml"))]
    modules = []
    roots = [ROOT / "modules", *(extra or [])]
    for m in (m for r in roots for m in sorted(r.rglob("module.toml"))):
        d = tomllib.loads(m.read_text())
        files = {
            o["path"]: (m.parent / o["path"]).read_text() for o in d.get("object", [])
        }
        for r in d.get("registry", []):  # the permanent-number tables of frameworks
            if r.get("index_table"):
                files[r["index_table"]] = (m.parent / r["index_table"]).read_text()
        gallery = (
            m.parent / GALLERY
        )  # `patchkit gen`: machines and parameters, for the page
        if gallery.is_file():
            files[GALLERY] = gallery.read_text()
        modules.append({"manifest": m.read_text(), "files": files})
    # the device files with a [modules] section: the knob models the typed module sections are lowered with
    devices = []
    for f in sorted((ROOT / "devices").glob("*.toml")):
        text = f.read_text()
        if "modules" in tomllib.loads(text):
            devices.append(text)
    return {"ports": ports, "modules": modules, "devices": devices}


def sharc_listing(syx: Path, plan: dict) -> list[str]:
    """Disassemble the DSP sites of a plan from the patched image's boot stream."""
    from tools.patchkit.dsp_patch import Stream, section_blob
    from tools.sharc_dev import sharc_asm, sharc_isa

    stream = Stream(section_blob(syx, 7))
    isa = sharc_isa.load_instruction_set()
    out: list[str] = []

    def listing(addr: int, length: int, title: str) -> None:
        out.append(f"  {title}")
        data = stream.read(addr, length)
        off = 0
        while off < len(data):
            r = isa.decode_bytes(data[off : off + 6])
            n = r.instruction.extent_bytes if r.instruction else 2
            parcels = [
                int.from_bytes(data[off + i : off + i + 2], "little")
                for i in range(0, n, 2)
            ]
            try:
                text = sharc_asm.decode_text(0, parcels)
            except Exception as e:  # noqa: BLE001
                text = f"?? ({e})"
            pc = (
                ((addr + off - 0x28000000) // 2)
                if addr >= 0x28000000
                else 0xB80000 + (addr + off - 0x20000000) // 2
            )
            out.append(
                f"    {pc:#08x}  {' '.join(f'{p:04x}' for p in parcels):<14} {text}"
            )
            off += n

    for w in plan["writes"]:
        if w["cpu"] != "sharc":
            continue
        if w["kind"] == "patch":
            listing(w["addr"], w["len"], f"[{w['owner']}] {w['what']} (after)")
        elif w["kind"] == "place" and w.get("code"):
            for lo, hi in w["code"]:
                listing(lo, hi - lo, f"[{w['owner']}] code at {lo:#x}")
    return out


def cmd_explain(args: list[str]) -> int:
    r = run(["explain", *args], capture=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    fw, mods = flag(args, "--firmware"), flag(args, "--modules")
    if r.returncode or not fw:
        return r.returncode
    tmp = ROOT / "out" / "patch" / "explain"
    tmp.mkdir(parents=True, exist_ok=True)
    out, plan = tmp / "out.syx", tmp / "plan.json"
    a = run(
        [
            "apply",
            "--firmware",
            fw,
            "--modules",
            mods or "",
            "-o",
            str(out),
            "--plan",
            str(plan),
        ],
        capture=True,
    )
    if a.returncode:
        return a.returncode
    lines = sharc_listing(out, json.loads(plan.read_text()))
    if lines:
        print("\nSHARC+ listing (patched image):")
        print("\n".join(lines))
    return 0


def cmd_patch(args: list[str]) -> int:
    fw, mods = flag(args, "--firmware"), flag(args, "--modules")
    if not fw or not mods:
        raise SystemExit("patch needs --firmware X.syx and --modules a,b")
    out = flag(args, "-o")
    if out is None:
        d = ROOT / "out" / "patch"
        d.mkdir(parents=True, exist_ok=True)
        out = str(d / f"{Path(fw).stem}+{re.sub(r'[^A-Za-z0-9.-]+', '_', mods)}.syx")
    plan = flag(args, "--plan") or str(Path(out).with_suffix(".plan.json"))
    rest = [a for a in args if a not in ("-o", out, "--plan", plan)]
    r = run(["apply", *rest, "-o", out, "--plan", plan])
    if r.returncode == 0:
        print(f"plan: {plan}")
    return r.returncode


def cmd_pack(args: list[str]) -> int:
    """`pack MODULE [--device dt2|dn2] [-o OUT.dkm] [--name N] [--runtime-min V]`: a run-time module as a .dkm."""
    module = next(
        (
            a
            for i, a in enumerate(args)
            if not a.startswith("-") and (i == 0 or args[i - 1] not in VALUE_FLAGS)
        ),
        None,
    )
    if module is None:
        raise SystemExit(
            "pack needs a module id, e.g. pack test/dyn-pluck --device dt2"
        )
    out = flag(args, "-o")
    if out is None:
        d = ROOT / "out" / "patch"
        d.mkdir(parents=True, exist_ok=True)
        device = flag(args, "--device")
        out = str(
            d / (module.rsplit("/", 1)[-1] + (f"-{device}" if device else "") + ".dkm")
        )
        args = [*args, "-o", out]
    r = run(["pack", *args])
    if r.returncode == 0:
        print(f"dkm: {out}")
    return r.returncode


VALUE_FLAGS = ("-o", "--device", "--name", "--runtime-min")


def cmd_inject(args: list[str]) -> int:
    from tools.control.digi_client import main as client_main

    syx = next((a for a in args if not a.startswith("-")), None)
    if syx is None:
        outs = sorted(
            (ROOT / "out" / "patch").glob("*.syx"), key=lambda p: p.stat().st_mtime
        )
        if not outs:
            raise SystemExit(
                "inject: no patched image under out/patch/; run `mise run patch` first"
            )
        syx = str(outs[-1])
    rest = ["inject", syx]
    if "--hot" in args:
        rest += ["--start", "snapshot"]
    return client_main(rest)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if cmd == "bin":
        print(binary())
        return 0
    if cmd == "bundle":
        out = Path(flag(args, "-o") or ROOT / "out" / "patch" / "bundle.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        extra = flag(args, "--modules-dir")
        out.write_text(
            json.dumps(bundle([Path(extra)] if extra else None), sort_keys=True)
        )
        print(out)
        return 0
    if cmd == "patch":
        return cmd_patch(args)
    if cmd == "explain":
        return cmd_explain(args)
    if cmd == "inject":
        return cmd_inject(args)
    if cmd == "pack":
        return cmd_pack(args)
    if cmd in ("list", "inspect", "check", "verify", "apply", "rebuild", "gen"):
        return run([cmd, *args]).returncode
    print(__doc__)
    return 2


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main(sys.argv[1:]))
