#!/usr/bin/env python3
"""ColdFire ISA check: refuse any instruction a MCF54418 (V4e: ISA_A/B/C + EMAC, no FPU) does not have.

    uv run python -m tools.coldfire.isacheck FILE...          # .dko.json (arch cf), ELF .o, or .a archive
    uv run python -m tools.coldfire.isacheck --committed      # every committed modules/**/build/*.dko.json

Every executable section is decoded with our own decoder (`cfdis`, generated from tools/coldfire/cfisa/coldfire.json). A
68020+ form (`divs.l` 64-bit, `bfextu`, `cmp2`, ...), an FPU form, a privileged form other than `cpushl` and an
illegal word are refused. A 68020-only word that the trap handler of a V4e meets is an illegal-instruction exception,
so a module with one crashes the device when the code runs, not when it loads. The same check runs inside the module
build (tools/patchkit/pk_link.py `check_isa`) and over the libgcc helper archive (tools/patchkit/pk_toolchains.py).
Only text sections are decoded: data in a text section (jump tables) would be decoded as code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_UNITS = {"isa_a", "isa_b", "isa_c", "emac"}
# Privileged forms a module may use: cache line push-and-invalidate (the digikit runtime flushes loaded code) and the status
# register moves (the runtime masks interrupts for the soft reset of `dkrt_restart`; the tasks run in supervisor mode).
PRIVILEGED_OK = {"cpushl", "move_from_sr", "move_to_sr"}


class IsaError(RuntimeError):
    pass


def _cfdis() -> Path:
    exe = ROOT / "target" / "release" / "cfdis"
    if not exe.exists():
        r = subprocess.run(
            ["cargo", "build", "--release", "-p", "coldfire", "--bin", "cfdis"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise IsaError(f"cannot build cfdis:\n{r.stderr[-2000:]}")
    return exe


def decode(
    image: bytes, base: int, ranges: list[tuple[int, int]]
) -> tuple[dict[str, int], list[str]]:
    """(form histogram, refused instructions) for the code `ranges` (absolute addresses) of `image` loaded at `base`."""
    if not ranges:
        return {}, []
    cfdis = _cfdis()
    units = {}
    for line in subprocess.run(
        [str(cfdis), "--forms"], capture_output=True, text=True, check=True
    ).stdout.splitlines():
        form, unit, priv, _page = line.split("\t")
        units[form] = (unit, priv == "true")
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(image)
        f.flush()
        spec = "".join(f"{lo:x} {hi:x}\n" for lo, hi in ranges)
        out = subprocess.run(
            [str(cfdis), f.name, f"{base:x}", "--ranges"],
            input=spec,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    hist: dict[str, int] = {}
    bad: list[str] = []
    for line in out.splitlines():
        if line.startswith("R "):
            continue
        addr, length, form, _text = line.split(" ", 3)
        if length == "0":
            bad.append(f"{addr}: illegal encoding")
            continue
        unit, priv = units[form]
        hist[form] = hist.get(form, 0) + 1
        if unit not in ALLOWED_UNITS or (priv and form not in PRIVILEGED_OK):
            bad.append(f"{addr}: {form} ({unit}{', privileged' if priv else ''})")
    return hist, bad


def check_sections(
    sections: list[tuple[str, bytes]], label: str = ""
) -> dict[str, int]:
    """Decode text blobs placed one after the other (each 4-byte aligned); raise IsaError listing every refused form."""
    image = bytearray()
    ranges = []
    names = []
    for name, blob in sections:
        image += b"\0" * (-len(image) % 4)
        ranges.append((len(image), len(image) + len(blob)))
        names.append((len(image), name))
        image += blob
    hist, bad = decode(bytes(image), 0, ranges)
    if bad:

        def where(line: str) -> str:
            a = int(line.split(":")[0], 16)
            sec = max((o for o in names if o[0] <= a), default=(0, "?"))
            return f"{label}{sec[1]}+{a - sec[0]:#x} {line.split(': ', 1)[1]}"

        raise IsaError(
            "instructions outside ISA_A/B/C+EMAC:\n  " + "\n  ".join(map(where, bad))
        )
    return hist


def _ar_members(blob: bytes) -> list[tuple[str, bytes]]:
    """Members of a GNU/BSD `ar` archive (ELF objects only; the symbol table and long-name table are skipped)."""
    if blob[:8] != b"!<arch>\n":
        raise IsaError("not an ar archive")
    out, pos, longnames = [], 8, b""
    while pos + 60 <= len(blob):
        hdr = blob[pos : pos + 60]
        name = hdr[:16].decode().strip()
        size = int(hdr[48:58].decode().strip())
        body = blob[pos + 60 : pos + 60 + size]
        pos += 60 + size + (size & 1)
        if name == "//":
            longnames = body
        elif name in ("/", "/SYM64/"):
            continue
        else:
            if name.startswith("/") and name[1:].isdigit():
                i = int(name[1:])
                name = longnames[i : longnames.index(b"/\n", i)].decode()
            out.append((name.rstrip("/"), body))
    return out


def elf_text(blob: bytes) -> list[tuple[str, bytes]]:
    from tools.patchkit import pk_elf

    elf = pk_elf.parse(blob)
    if elf.machine != 4 or elf.little:
        raise IsaError("not a big-endian m68k object")
    return [
        (s.name, s.data)
        for s in elf.sections
        if s.flags & pk_elf.SHF_EXECINSTR and s.type == 1 and s.size
    ]


def check_file(path: Path) -> dict[str, int]:
    """Check one `.dko.json` (cf), ELF object or archive; return the form histogram."""
    import json

    label = f"{path.name}: "
    if path.suffix == ".json":
        obj = json.loads(path.read_text())
        if obj.get("arch") != "cf":
            return {}
        secs = [
            (s["name"], bytes.fromhex(s["hex"]))
            for s in obj["sections"]
            if s["kind"] == "text"
        ]
        return check_sections(secs, label)
    blob = path.read_bytes()
    members = _ar_members(blob) if blob[:8] == b"!<arch>\n" else [(path.name, blob)]
    hist: dict[str, int] = {}
    bad = []
    for name, body in members:
        try:
            h = check_sections(elf_text(body), f"{label}{name}:")
        except IsaError as e:
            bad.append(str(e).split("\n", 1)[1].strip())
            continue
        for k, v in h.items():
            hist[k] = hist.get(k, 0) + v
    if bad:
        raise IsaError("instructions outside ISA_A/B/C+EMAC:\n  " + "\n  ".join(bad))
    return hist


def committed_objects(root: Path = ROOT) -> list[Path]:
    return sorted((root / "modules").glob("**/build/*.dko.json"))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--committed", action="store_true")
    a = ap.parse_args()
    files = [*a.files, *(committed_objects() if a.committed else [])]
    if not files:
        ap.error("no files")
    rc = 0
    for f in files:
        try:
            h = check_file(f)
        except IsaError as e:
            rc = 1
            print(f"FAIL {f}\n  {e}")
            continue
        print(
            f"ok   {f}: {sum(h.values())} instructions"
            if h
            else f"ok   {f}: no cf code"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
