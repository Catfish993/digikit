#!/usr/bin/env python3
"""Turn the guest's writes to the emulated eMMC card into a card image another firmware can mount.

    uv run python tools/card_overlay.py apply BASE.img OVERLAY OUT.img

The daemon method `card.overlay {path}` (`Emu.call("card.overlay", path=...)`) writes the sectors the guest wrote
since the card was mounted: per sector a little-endian u64 sector number, a 64-byte mask (bit i of byte i // 8 names
data byte i) and 512 data bytes. `apply` copies BASE.img (a copy-on-write clone where the file system has one, so
the 3.7 GB DT2 card costs nothing) to OUT.img and writes the masked bytes over it. Mount OUT.img with
`firmware.load ... card=OUT.img` to run the same project on another firmware. Card images derive from the user's
samples and firmware: they stay under out/ and are never committed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

RECORD = 8 + 64 + 512
SECTOR = 512


def records(blob: bytes):
    """(sector, mask, data) for every record of an overlay file."""
    if len(blob) % RECORD:
        raise ValueError(f"overlay length {len(blob)} is not a multiple of {RECORD}")
    for at in range(0, len(blob), RECORD):
        sector = int.from_bytes(blob[at : at + 8], "little")
        yield sector, blob[at + 8 : at + 72], blob[at + 72 : at + 72 + SECTOR]


SOUND_HEAD = bytes.fromhex(
    "beefbace00000003"
)  # soundStorage_v3: magic, version 3 (finding 02, "Persistence")
SOUND_TYPE_AT = 0xD2  # DT2: the machine type byte of a sound record


def sound_types(blob: bytes) -> dict[int, int]:
    """Machine type -> number of DT2 sound records of that type among the written sectors (runs of adjacent sectors
    are joined, so records that cross a sector boundary are found)."""
    secs = {sector: data for sector, _mask, data in records(blob)}
    out: dict[int, int] = {}
    run: list[int] = []
    for sector in [*sorted(secs), None]:
        if run and (sector is None or sector != run[-1] + 1):
            data = b"".join(secs[k] for k in run)
            at = data.find(SOUND_HEAD)
            while at >= 0:
                if at + SOUND_TYPE_AT < len(data):
                    t = data[at + SOUND_TYPE_AT]
                    out[t] = out.get(t, 0) + 1
                at = data.find(SOUND_HEAD, at + 1)
            run = []
        if sector is not None:
            run.append(sector)
    return out


def clone(src: Path, dst: Path) -> None:
    """Copy-on-write clone where available (APFS `cp -c`, reflink), else a plain copy."""
    if dst.exists():
        dst.unlink()
    for cmd in (
        ["cp", "-c", str(src), str(dst)],
        ["cp", "--reflink=auto", str(src), str(dst)],
    ):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return
    shutil.copyfile(src, dst)


def apply(base: Path, overlay: Path, out: Path) -> int:
    """Write the overlay over a copy of BASE at OUT; returns the number of sectors touched."""
    clone(base, out)
    n = 0
    fd = os.open(out, os.O_RDWR)
    try:
        for sector, mask, data in records(overlay.read_bytes()):
            at = sector * SECTOR
            cur = bytearray(os.pread(fd, SECTOR, at).ljust(SECTOR, b"\0"))
            for i in range(SECTOR):
                if mask[i >> 3] & (1 << (i & 7)):
                    cur[i] = data[i]
            os.pwrite(fd, bytes(cur), at)
            n += 1
    finally:
        os.close(fd)
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("base", type=Path)
    a.add_argument("overlay", type=Path)
    a.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    n = apply(args.base, args.overlay, args.out)
    print(f"{args.out}: {n} sectors written over {args.base.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
