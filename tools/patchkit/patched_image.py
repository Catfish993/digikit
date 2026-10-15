#!/usr/bin/env python3
"""Build a harmless test image: stock Digitakt II firmware plus one visible string change in MAIN OS.

    uv run python -m tools.patchkit.patched_image [--syx Digitakt_II_OS1.16.syx] [--string OLD=NEW]
        [--out out/patch/dt2-1.16-harmless.syx] [--png FRAME.png] [--no-boot] [--no-bootstrap]

The string must occur exactly once in section 3, NUL-delimited, and keep its length. The default turns the default
project name UNTITLED, shown at the top of the main screen of a new project, into PATCHKIT. Every other section is
re-stored from its depacked bytes (sections 2, 4 and 8 are never changed; patchimg refuses them).

The image is made by tools.firmware.inplace: the stock compressed stream of section 3 is decoded into tokens, the edit is applied
and only the tokens it touches are re-encoded, so the container keeps (within bytes) its stock size and the other
sections keep their stock stored bytes. (tools.firmware.build.rebuild stores every section literals-only: 4.29 MB, three times
the stock container, which is not a size to flash. See docs/findings/01-container-and-patching.md.)

Checks, all reported in OUT.json next to the image:
  * roundtrip.check_authenticity (preamble checksum, HMAC trailer, framing count and per-message checksums) and a
    re-extraction: only section 3 differs, and only at the patched bytes;
  * the container size against the stock one;
  * the real bootstrap (tools/emulator/bootstrap_run.py) accepts the image over emulated MIDI DIN: content checksum and HMAC
    pass, the flash is written, the reboot loads the patched MAIN OS;
  * a cold ColdFire boot of the image in the emulator (packages/emulator/boot example boot_frame) reaches the main screen, and
    the saved frame is written as a PNG.

Output goes under out/ and is never committed. Flashing it is Em's decision.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.control import digi_client  # noqa: E402
from tools.emulator import bootstrap_run  # noqa: E402
from tools.firmware import (  # noqa: E402
    container,
    inplace,
    roundtrip,  # noqa: E402
)
from tools.firmware.elz import depack_section  # noqa: E402
from tools.firmware.extract import classify  # noqa: E402
from tools.patchkit import patchimg  # noqa: E402

MAIN_LOAD = 0x4000_0400
DEFAULT_STRING = ("UNTITLED", "PATCHKIT")


def sections_of(syx: pathlib.Path | str) -> dict[int, bytes]:
    """Every section of a .syx, depacked."""
    c, secs = container.sections(syx)
    out = {}
    for sid, off, clen, _dest in secs:
        kind, payload = classify(bytes(c[off : off + clen]))
        out[sid] = depack_section(payload) if kind == "packed" else payload
    return out


def locate(image: bytes, old: str) -> int:
    """Guest address of the single NUL-delimited occurrence of `old` in section 3."""
    needle = b"\x00" + old.encode("ascii") + b"\x00"
    first = image.find(needle)
    if first < 0:
        raise ValueError(f"{old!r} is not in section 3 as a NUL-delimited string")
    if image.find(needle, first + 1) >= 0:
        raise ValueError(f"{old!r} occurs more than once in section 3")
    return MAIN_LOAD + first + 1


def make(syx: pathlib.Path, old: str, new: str, out: pathlib.Path) -> dict:
    """Write the patched .syx to `out`; return the build part of the report."""
    if len(old) != len(new):
        raise ValueError("the replacement must keep the string's length")
    stock = sections_of(syx)
    main = stock[3]
    patches = [
        patchimg.parse_str_patch(f"{locate(main, old):#x}={old}:{new}", MAIN_LOAD)
    ]
    errors = patchimg.validate(main, patches)
    if errors:
        raise ValueError("; ".join(errors))
    patched_main = patchimg.apply_patches(main, patches)
    edits = {
        i: b for i, (a, b) in enumerate(zip(main, patched_main, strict=True)) if a != b
    }
    data, stats = inplace.patch_section(syx, 3, edits)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    with contextlib.redirect_stdout(io.StringIO()):
        authentic = roundtrip.check_authenticity(data, str(out))
    got = sections_of(out)
    changed = sorted(sid for sid in stock if got.get(sid) != stock[sid])
    diff = [i for i, (a, b) in enumerate(zip(main, got[3], strict=True)) if a != b]
    return {
        "string": [old, new],
        "address": f"{patches[0]['addr']:#x}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "base_sha256": hashlib.sha256(syx.read_bytes()).hexdigest(),
        "check_authenticity": authentic,
        "sections_changed": changed,
        "section_3_bytes_changed": len(diff),
        "section_3_changed_only_at_patch": got[3] == patched_main,
        "container_bytes": len(bootstrap_run.container_of(out)),
        "stock_container_bytes": len(bootstrap_run.container_of(syx)),
        "tokens": stats,
    }


def boot_frame(out: pathlib.Path, base_sha256: str, png: pathlib.Path | None) -> dict:
    """Cold boot `out` in the emulator (ColdFire only) and save the main screen."""
    exe = bootstrap_run.build_example("elektron-native-boot", "boot_frame")
    with tempfile.TemporaryDirectory() as tmp:
        frame = pathlib.Path(tmp) / "frame.bin"
        done = subprocess.run(
            [str(exe), str(out), base_sha256, str(frame)],
            capture_output=True,
            text=True,
            check=False,
        )
        report = {
            "returncode": done.returncode,
            "stdout": done.stdout.strip().splitlines(),
            "stderr": done.stderr[-300:],
        }
        if done.returncode == 0 and png is not None:
            png.write_bytes(digi_client.oled_png(frame.read_bytes()))
            report["png"] = str(png)
        report["reached_main_screen"] = any(
            "main_ui=true" in line for line in report["stdout"]
        )
        return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--syx", default=str(ROOT / "Digitakt_II_OS1.16.syx"))
    ap.add_argument(
        "--string", default="=".join(DEFAULT_STRING), help="OLD=NEW, same length"
    )
    ap.add_argument("--out", default=str(ROOT / "out/patch/dt2-1.16-harmless.syx"))
    ap.add_argument("--png", help="main-screen picture (default: next to --out)")
    ap.add_argument("--no-boot", action="store_true", help="skip the emulator boot")
    ap.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="skip the emulated bootstrap acceptance",
    )
    args = ap.parse_args(argv)
    old, _, new = args.string.partition("=")
    syx, out = pathlib.Path(args.syx), pathlib.Path(args.out)
    report = make(syx, old, new, out)
    ok = (
        report["check_authenticity"]
        and report["sections_changed"] == [3]
        and report["section_3_changed_only_at_patch"]
    )
    if not args.no_bootstrap:
        verdict = bootstrap_run.receive(out, base=syx)
        report["bootstrap"] = verdict.summary()
        ok = ok and verdict.accepted
    if not args.no_boot:
        png = pathlib.Path(args.png) if args.png else out.with_suffix(".png")
        report["emulator_boot"] = boot_frame(out, report["base_sha256"], png)
        ok = ok and report["emulator_boot"]["reached_main_screen"]
    report["ok"] = bool(ok)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
