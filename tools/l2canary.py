#!/usr/bin/env python3
"""A canary over free SHARC+ L2: build a .syx with an appended boot-stream block of known words, and check the
words in a running or finished emulator.

    uv run python tools/l2canary.py ranges SYX
    uv run python tools/l2canary.py build SYX OUT.syx [--range ADDR:LEN] [--seed N]
    uv run python tools/l2canary.py coupled-args --range ADDR:LEN [--block N]   # the coupled-run flag
    uv run python tools/l2canary.py patchfile --range ADDR:LEN OUT.patch        # coupled-run --dsp-patch
    uv run python tools/l2canary.py check-report REPORT.json --range ADDR:LEN [--seed N]
    uv run python tools/l2canary.py compare REPORT.json OTHER.json              # PCM hashes per scenario
    uv run python tools/l2canary.py poke  --range ADDR:LEN [--seed N]    # control socket: write the pattern now
    uv run python tools/l2canary.py verify --range ADDR:LEN [--seed N]   # control socket: read it back

The word at byte address A (a multiple of 4) is `((A * 0x9E3779B1) ^ SEED) & 0xffffffff`, little endian, so a
copied or shifted block shows up as a mismatch. `build` appends one block (`dsp_patch.Stream.append_block`); with no
--range it takes the largest free L2 range from its start, as much as the boot routine's limit allows (depacked
section 7 at most 0xfffff bytes). `coupled-args` prints `--dsp-peek canary=ADDR:LEN`; `check-report` compares the
SHA-256 that coupled-run put in each scenario's report (`dsp_peek.canary`) with the pattern's (with --block, the
damaged ranges). `patchfile` writes the pattern as a DSP patch for `coupled-run --dsp-patch`: the canary is then
laid over a ready state with no stream size limit. `poke` and `verify`
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


def patch_text(addr: int, length: int, seed: int) -> str:
    """A dsp-patch file (`BYTES` lines) that writes the pattern; `coupled-run --dsp-patch` applies it after
    the ready state is restored, so the canary needs no stream space."""
    data = pattern(addr, length, seed)
    return "".join(
        f"BYTES {addr + off:x} {data[off : off + 4096].hex()}\n"
        for off in range(0, length, 4096)
    )


def damaged(
    block_hashes: list[str], addr: int, length: int, block: int, seed: int
) -> list[tuple[int, int]]:
    """Maximal [lo, hi) address ranges whose block hash (from `coupled-run --dsp-peek NAME=ADDR:LEN:BLOCK`) is not
    the pattern's."""
    data = pattern(addr, length, seed)
    out: list[tuple[int, int]] = []
    for k, have in enumerate(block_hashes):
        want = hashlib.sha256(data[k * block : (k + 1) * block]).hexdigest()[:16]
        if have != want:
            lo, hi = addr + k * block, addr + min((k + 1) * block, length)
            if out and out[-1][1] == lo:
                out[-1] = (out[-1][0], hi)
            else:
                out.append((lo, hi))
    return out


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


def report_rows(path: Path) -> list[dict]:
    report = json.loads(path.read_text())
    rows = report.get("scenarios", report)
    return rows if isinstance(rows, list) else [rows]


def check_report(a: argparse.Namespace) -> int:
    addr, length = a.rng
    want = pattern_sha256(addr, length, a.seed)
    failed = 0
    for row in report_rows(a.report):
        got = (row.get("dsp_peek") or {}).get("canary", {})
        ok = got.get("sha256") == want and got.get("unknown") == 0
        failed += not ok
        text = f"{row.get('name', '?')}: canary {'intact' if ok else 'DAMAGED'} unknown={got.get('unknown')}"
        if not ok and got.get("blocks"):
            bad = damaged(got["blocks"], addr, length, got["block"], a.seed)
            text += " damaged " + ", ".join(f"{lo:#x}..{hi:#x}" for lo, hi in bad)
        print(text)
    return 1 if failed else 0


def compare(a: argparse.Namespace) -> int:
    """PCM hashes of two coupled-run reports, scenario by scenario."""
    first = {r["name"]: r["pcm"]["sha256"] for r in report_rows(a.report)}
    second = {r["name"]: r["pcm"]["sha256"] for r in report_rows(a.other)}
    differ = 0
    for name in first:
        same = first[name] == second.get(name)
        differ += not same
        print(
            f"{name}: PCM {'equal' if same else 'DIFFERS'} {first[name][:12]} {second.get(name, '-')[:12]}"
        )
    return 1 if differ else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ranges").add_argument("syx", type=Path)
    names = ("build", "coupled-args", "patchfile", "check-report", "poke", "verify")
    for name in names:
        c = sub.add_parser(name)
        c.add_argument(
            "--range", dest="rng", type=parse_range, required=name != "build"
        )
        c.add_argument("--seed", type=lambda x: int(x, 0), default=DEFAULT_SEED)
        if name == "build":
            c.add_argument("syx", type=Path)
            c.add_argument("out", type=Path)
        if name == "coupled-args":
            c.add_argument("--block", type=lambda x: int(x, 0), default=0)
        if name == "patchfile":
            c.add_argument("out", type=Path)
        if name == "check-report":
            c.add_argument("report", type=Path)
        if name in ("poke", "verify"):
            c.add_argument("--socket")
            c.add_argument("--run-dir")
    c = sub.add_parser("compare")
    c.add_argument("report", type=Path)
    c.add_argument("other", type=Path)
    a = p.parse_args(argv)

    if a.cmd == "ranges":
        stream = dsp_patch.Stream(dsp_patch.section_blob(a.syx, 7))
        print(
            f"stream {len(stream.blob):#x} bytes; room under the boot limit "
            f"{dsp_patch.MAX_STREAM - len(stream.blob) - 16:#x}"
        )
        for lo, hi in stream.free_ranges():
            print(f"free L2 {lo:#010x}..{hi:#010x} ({hi - lo:#x} bytes)")
    elif a.cmd == "build":
        print(json.dumps(build(a.syx, a.out, a.rng, a.seed), indent=1))
    elif a.cmd == "coupled-args":
        addr, length = a.rng
        block = f":{a.block:#x}" if a.block else ""
        print(f"--dsp-peek canary={addr:#x}:{length:#x}{block}")
    elif a.cmd == "patchfile":
        a.out.write_text(patch_text(*a.rng, a.seed))
    elif a.cmd == "check-report":
        return check_report(a)
    elif a.cmd == "compare":
        return compare(a)
    else:
        emu = connect(a)
        addr, length = a.rng
        if a.cmd == "poke":
            poke(emu, addr, length, a.seed)
        print(json.dumps(verify(emu, addr, length, a.seed), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
