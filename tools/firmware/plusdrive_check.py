#!/usr/bin/env python3
"""The firmware's own view of a +Drive image built from a folder (DT2 1.16).

    uv run python -m tools.firmware.plusdrive_check [--syx FILE] [--keep DIR]

Builds a `plus_drive/dt2/`-style fixture (nested folders, a plain file
/digikit/modules/test.dkm, a directory of 700 entries that needs two content
pages, files of 2 and 10 extents (inline and indirect extent records), a
WAV that becomes a native sample), builds the card image with `plusdrive build`
(`--max-extent-pages 4`), boots the DT2 with it
(packages/emulator/boot/examples/plusdrive_check.rs, about 15 s) and calls the
firmware's mount flag, directory iterator, name lookup and file open, size and
read. The listings, sizes and SHA-256 sums must equal the folder's. Exit status 0
only when everything matches.

The former Python emulator ran the same functions on a snapshot
(REF:unicorn-reference-final:tools/plusdrive_check.py); this one boots the
native machine and calls them with `Emulator::call_function`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools import python_env  # noqa: E402
from tools.emulator import digi_env, native_target  # noqa: E402

MANY = 700


def write_wav(path: pathlib.Path) -> None:
    import struct
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(
            struct.pack("<2400h", *[(i * 37) % 6000 - 3000 for i in range(2400)])
        )


def make_fixture(folder: pathlib.Path) -> dict[str, bytes]:
    """Write the fixture and return the plain files' bytes by path."""
    rng = random.Random(16)
    plain = {
        "digikit/modules/test.dkm": rng.randbytes(3000),
        "ext/two.bin": rng.randbytes(5 * 0x8000 - 7),
        "ext/ten.bin": rng.randbytes(37 * 0x8000 + 11),
        "top.txt": b"hello +Drive\n",
    }
    for i in range(MANY):
        plain[f"many/entry-{i:04d}-{'x' * 50}.txt"] = f"{i}\n".encode()
    for path, data in plain.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (folder / "kicks").mkdir()
    write_wav(folder / "kicks" / "tick.wav")
    return plain


def cargo(args: list[str], env: dict[str, str]) -> None:
    done = subprocess.run(["cargo", *args], cwd=ROOT, env=env)
    if done.returncode:
        raise SystemExit(done.returncode)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--syx", type=pathlib.Path, help="DT2 1.16 .syx (default: the repo's)"
    )
    ap.add_argument(
        "--keep",
        type=pathlib.Path,
        help="build the fixture and image here and keep them",
    )
    args = ap.parse_args(argv)
    syx = args.syx or digi_env.firmware_for("dt2")
    if digi_env.device_for_syx(syx) != "dt2":
        raise SystemExit(f"{syx} is not a known DT2 firmware")
    env = digi_env.rust_env()
    target = native_target.target_dir("")
    cargo(
        [
            "build", "--release", "--locked", "--offline", "--target-dir", target,
            "-p", "plusdrive-format", "--bin", "plusdrive",
            "-p", "elektron-native-boot", "--example", "plusdrive_check",
        ],
        env,
    )  # fmt: skip
    release = pathlib.Path(target) / "release"
    work = args.keep or pathlib.Path(tempfile.mkdtemp(prefix="plusdrive-check-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        folder = work / "plus_drive"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        plain = make_fixture(folder)
        image = work / "dt2.img"
        image.unlink(missing_ok=True)
        subprocess.run(
            [
                str(release / "plusdrive"), "build", str(folder), "-o", str(image),
                "--device", "dt2", "--syx", str(syx), "--max-extent-pages", "4",
            ],
            cwd=ROOT, env=python_env(), check=True,
        )  # fmt: skip
        ops = [
            "mounted", "ls:/", "ls:/digikit", "ls:/digikit/modules", "ls:/ext", "ls:/many",
            "ls:/kicks", "lookup:2:digikit", "read:/kicks/tick",
            *(f"read:/{p}" for p in ("digikit/modules/test.dkm", "ext/two.bin", "ext/ten.bin", "top.txt")),
            f"read:/many/entry-0000-{'x' * 50}.txt", f"read:/many/entry-{MANY - 1:04d}-{'x' * 50}.txt",
        ]  # fmt: skip
        done = subprocess.run(
            [str(release / "examples" / "plusdrive_check"), str(syx), str(image), *ops],
            cwd=ROOT, env=python_env(), capture_output=True, text=True,
        )  # fmt: skip
        results = {}
        for line in done.stdout.splitlines():
            row = json.loads(line)
            results[row["op"]] = row
        if done.returncode and "boot" not in results:
            print(done.stdout + done.stderr)
            return 1
        failures = judge(results, plain)
        for op in ops:
            row = results.get(op, {})
            print(f"{op[:70]:72} {summary(row)}")
        if failures:
            print("\nFAIL")
            for f in failures:
                print("  " + f)
            return 1
        print(
            f"\nOK: {len(ops)} firmware calls match the folder (boot icount {results['boot']['icount']:,})"
        )
        return 0
    finally:
        if args.keep is None:
            shutil.rmtree(work, ignore_errors=True)


def summary(row: dict) -> str:
    if "entries" in row:
        return f"{len(row['entries'])} entries"
    if "sha256" in row:
        return f"{row['read']} bytes sha256 {row['sha256'][:12]}"
    if "found" in row:
        return f"found={row['found']} id={row['id']}"
    return str(row.get("value", row.get("error", "?")))


def names(row: dict) -> list[str]:
    return sorted(e["name"] for e in row.get("entries", []))


def judge(results: dict, plain: dict[str, bytes]) -> list[str]:
    bad = []

    def expect(label: str, got, want) -> None:
        if got != want:
            bad.append(f"{label}: got {got!r}, want {want!r}")

    expect("mount flag", results.get("mounted", {}).get("value"), 1)
    for op, row in results.items():
        if row.get("error"):
            bad.append(f"{op}: {row['error']}")
    root = names(results.get("ls:/", {}))
    expect(
        "ls /",
        root,
        sorted([".", "..", "digikit", "ext", "factory", "kicks", "many", "top.txt"]),
    )
    expect("ls /digikit", names(results.get("ls:/digikit", {})), [".", "..", "modules"])
    expect(
        "ls /digikit/modules",
        names(results.get("ls:/digikit/modules", {})),
        [".", "..", "test.dkm"],
    )
    expect(
        "ls /ext", names(results.get("ls:/ext", {})), [".", "..", "ten.bin", "two.bin"]
    )
    expect(
        "ls /kicks (a .wav is named without the extension)",
        names(results.get("ls:/kicks", {})),
        [".", "..", "tick"],
    )
    many = sorted(
        [".", "..", *(p.split("/", 1)[1] for p in plain if p.startswith("many/"))]
    )
    expect("ls /many (two content pages)", names(results.get("ls:/many", {})), many)
    expect("lookup digikit", results.get("lookup:2:digikit", {}).get("name"), "digikit")
    for path, data in plain.items():
        row = results.get(f"read:/{path}")
        if row is None:
            continue
        expect(f"size {path}", row.get("size"), len(data))
        expect(f"read {path}", row.get("read"), len(data))
        expect(f"sha256 {path}", row.get("sha256"), hashlib.sha256(data).hexdigest())
    tick = results.get("read:/kicks/tick", {})
    expect(
        "native sample size (0x40 header + 2400 frames * 2 + 0x10)",
        tick.get("size"),
        0x40 + 4800 + 0x10,
    )
    return bad


if __name__ == "__main__":
    sys.exit(main())
