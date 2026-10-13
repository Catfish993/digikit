#!/usr/bin/env python3
"""A canary over free SHARC+ L2: build a .syx with an appended boot-stream block of known words, and check the
words in a running or finished emulator.

    uv run python tools/l2canary.py ranges SYX
    uv run python tools/l2canary.py build SYX OUT.syx [--range ADDR:LEN] [--seed N]
    uv run python tools/l2canary.py coupled-args --range ADDR:LEN        # the matching coupled-run flag
    uv run python tools/l2canary.py check-report REPORT.json --range ADDR:LEN [--seed N]
    uv run python tools/l2canary.py poke  --range ADDR:LEN [--seed N]    # control socket: write the pattern now
    uv run python tools/l2canary.py verify --range ADDR:LEN [--seed N]   # control socket: read it back

The word at byte address A (a multiple of 4) is `((A * 0x9E3779B1) ^ SEED) & 0xffffffff`, little endian, so a
copied or shifted block shows up as a mismatch. `build` appends one block (`dsp_patch.Stream.append_block`); with no
--range it takes the largest free L2 range from its start, as much as the boot routine's limit allows (depacked
section 7 at most 0xfffff bytes). `coupled-args` prints `--dsp-peek canary=ADDR:LEN`; `check-report` compares the
SHA-256 that coupled-run put in each scenario's report (`dsp_peek.canary`) with the pattern's. `poke` and `verify`
use `dsp.patch` and `dsp.peek` of a running daemon (`mise run emu:serve`), which have no stream size limit.

Output is firmware-derived: keep it outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import digi_client
    import dsp_patch
finally:
    sys.path.pop(0)

DEFAULT_SEED = 0xC4A9_0000
CHUNK = 0x10000


def pattern(addr: int, length: int, seed: int = DEFAULT_SEED) -> bytes:
    """The canary bytes of [ADDR, ADDR+LENGTH); ADDR and LENGTH are multiples of 4."""
    if addr % 4 or length % 4:
        raise ValueError("canary ranges are word aligned")
    return b"".join(
        struct.pack("<I", ((a * 0x9E37_79B1) ^ seed) & 0xFFFF_FFFF)
        for a in range(addr, addr + length, 4)
    )


def pattern_sha256(addr: int, length: int, seed: int = DEFAULT_SEED) -> str:
    return hashlib.sha256(pattern(addr, length, seed)).hexdigest()


def parse_range(text: str) -> tuple[int, int]:
    addr, length = text.rsplit(":", 1)
    return int(addr, 0), int(length, 0)


def largest_free(stream: dsp_patch.Stream) -> tuple[int, int]:
    """(address, length) of the block `build` appends by default."""
    free = stream.free_ranges()
    lo, hi = max(free, key=lambda r: r[1] - r[0])
    room = dsp_patch.MAX_STREAM - len(stream.blob) - 16
    return lo, min(hi - lo, room) & ~3


def build(
    syx: Path, out: Path, rng: tuple[int, int] | None, seed: int
) -> dict[str, object]:
    sys.path.insert(0, str(ROOT))
    try:
        from dt2.build import FLASH_CONTAINER_MAX, rebuild
    finally:
        sys.path.pop(0)
    stream = dsp_patch.Stream(dsp_patch.section_blob(syx, 7))
    stock_len = len(stream.blob)
    addr, length = rng or largest_free(stream)
    stream.append_block(addr, pattern(addr, length, seed))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        rebuild(
            str(syx),
            {7: bytes(stream.blob)},
            pack="aplib",
            max_container=FLASH_CONTAINER_MAX,
        )
    )
    return {
        "out": str(out),
        "addr": addr,
        "length": length,
        "seed": seed,
        "stream_bytes": len(stream.blob),
        "stock_stream_bytes": stock_len,
        "sha256": pattern_sha256(addr, length, seed),
        "container_bytes": out.stat().st_size,
    }


def read_back(emu, addr: int, length: int) -> tuple[bytes, int]:
    """(bytes, unknown count) of DSP memory through `dsp.peek`; unknown bytes read as 0."""
    out = bytearray()
    unknown = 0
    for off in range(0, length, CHUNK):
        n = min(CHUNK, length - off)
        got = emu.call("dsp.peek", addr=addr + off, len=n)
        hexed = got["hex"]
        unknown += got["unknown"]
        out += bytes(
            0 if hexed[i : i + 2] == "??" else int(hexed[i : i + 2], 16)
            for i in range(0, 2 * n, 2)
        )
    return bytes(out), unknown


def mismatches(have: bytes, want: bytes, base: int) -> list[tuple[int, int]]:
    """Maximal [lo, hi) byte ranges (absolute addresses, word granular) where HAVE differs from WANT."""
    out: list[tuple[int, int]] = []
    start = None
    for i in range(0, len(want), 4):
        bad = have[i : i + 4] != want[i : i + 4]
        if bad and start is None:
            start = i
        elif not bad and start is not None:
            out.append((base + start, base + i))
            start = None
    if start is not None:
        out.append((base + start, base + len(want)))
    return out


def verify(emu, addr: int, length: int, seed: int) -> dict[str, object]:
    have, unknown = read_back(emu, addr, length)
    bad = mismatches(have, pattern(addr, length, seed), addr)
    return {
        "addr": addr,
        "length": length,
        "intact": not bad and unknown == 0,
        "unknown_bytes": unknown,
        "bad_ranges": [[lo, hi] for lo, hi in bad],
        "bad_bytes": sum(hi - lo for lo, hi in bad),
    }


def poke(emu, addr: int, length: int, seed: int) -> None:
    data = pattern(addr, length, seed)
    for off in range(0, length, CHUNK):
        chunk = data[off : off + CHUNK]
        emu.patch(text=f"BYTES {addr + off:x} {chunk.hex()}\n")


def connect(args: argparse.Namespace):
    return digi_client.Emu.connect(args.socket, args.run_dir)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ranges")
    r.add_argument("syx", type=Path)
    b = sub.add_parser("build")
    b.add_argument("syx", type=Path)
    b.add_argument("out", type=Path)
    for name in ("build", "coupled-args", "check-report", "poke", "verify"):
        c = b if name == "build" else sub.add_parser(name)
        c.add_argument(
            "--range", dest="rng", type=parse_range, required=name != "build"
        )
        c.add_argument("--seed", type=lambda x: int(x, 0), default=DEFAULT_SEED)
        if name == "check-report":
            c.add_argument("report", type=Path)
        if name in ("poke", "verify"):
            c.add_argument("--socket")
            c.add_argument("--run-dir")
    a = p.parse_args(argv)

    if a.cmd == "ranges":
        stream = dsp_patch.Stream(dsp_patch.section_blob(a.syx, 7))
        free = stream.free_ranges()
        print(
            f"stream {len(stream.blob):#x} bytes; room under the boot limit {dsp_patch.MAX_STREAM - len(stream.blob) - 16:#x}"
        )
        for lo, hi in free:
            print(f"free L2 {lo:#010x}..{hi:#010x} ({hi - lo:#x} bytes)")
    elif a.cmd == "build":
        print(json.dumps(build(a.syx, a.out, a.rng, a.seed), indent=1))
    elif a.cmd == "coupled-args":
        addr, length = a.rng
        print(f"--dsp-peek canary={addr:#x}:{length:#x}")
    elif a.cmd == "check-report":
        addr, length = a.rng
        want = pattern_sha256(addr, length, a.seed)
        report = json.loads(a.report.read_text())
        rows = report.get("scenarios", report)
        failed = 0
        for row in rows if isinstance(rows, list) else [rows]:
            got = (row.get("dsp_peek") or {}).get("canary", {})
            ok = got.get("sha256") == want and got.get("unknown") == 0
            failed += not ok
            print(
                f"{row.get('name', '?')}: canary {'intact' if ok else 'DAMAGED'} unknown={got.get('unknown')}"
            )
        return 1 if failed else 0
    else:
        emu = connect(a)
        addr, length = a.rng
        if a.cmd == "poke":
            poke(emu, addr, length, a.seed)
        print(json.dumps(verify(emu, addr, length, a.seed), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
