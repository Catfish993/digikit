#!/usr/bin/env python3
"""Front end for the patchkit command line (native/patchkit): builds it when stale and runs it from the repo root.

    uv run python tools/patchkit.py bin                       print the path of the built binary
    uv run python tools/patchkit.py patch --firmware X.syx --modules a,b [-o OUT.syx]   apply; writes out/patch/
    uv run python tools/patchkit.py explain --firmware X.syx --modules a,b              site-by-site account
    uv run python tools/patchkit.py list|inspect|check|verify ...                       passed through
    uv run python tools/patchkit.py bundle [-o FILE]          ports + modules as one JSON (what patchkit.wasm reads)
    uv run python tools/patchkit.py inject [OUT.syx] [--hot]  load a patched image into the running emulator

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
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import native_target  # noqa: E402


def target_dir() -> Path:
    # patchkit has no generated SHARC core: the "no core" key keeps its build away from sharc-gen's
    return Path(native_target.target_dir(None)).parent / "nogen"


def binary(build: bool = True) -> Path:
    exe = target_dir() / "release" / "patchkit"
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
                str(target_dir()),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise SystemExit(f"cannot build patchkit:\n{r.stderr[-3000:]}")
    return exe


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


def bundle() -> dict:
    """Ports and modules as texts: the input of `pk_apply` in patchkit.wasm."""
    ports = [p.read_text() for p in sorted((ROOT / "devices" / "ports").glob("*.toml"))]
    modules = []
    for m in sorted((ROOT / "modules").rglob("module.toml")):
        d = tomllib.loads(m.read_text())
        files = {
            o["path"]: (m.parent / o["path"]).read_text() for o in d.get("object", [])
        }
        modules.append({"manifest": m.read_text(), "files": files})
    return {"ports": ports, "modules": modules}


def sharc_listing(syx: Path, plan: dict) -> list[str]:
    """Disassemble the DSP sites of a plan from the patched image's boot stream."""
    import sharc_asm
    import sharc_isa
    from dsp_patch import Stream, section_blob

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


def cmd_inject(args: list[str]) -> int:
    from digi_client import main as client_main

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
        out.write_text(json.dumps(bundle(), sort_keys=True))
        print(out)
        return 0
    if cmd == "patch":
        return cmd_patch(args)
    if cmd == "explain":
        return cmd_explain(args)
    if cmd == "inject":
        return cmd_inject(args)
    if cmd in ("list", "inspect", "check", "verify", "apply", "rebuild"):
        return run([cmd, *args]).returncode
    print(__doc__)
    return 2


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main(sys.argv[1:]))
