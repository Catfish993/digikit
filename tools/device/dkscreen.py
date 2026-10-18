#!/usr/bin/env python3
r"""Screenshot of the OLED through the firmware's own MidiRpc `Screenshot` (type 0x04), written as PNG. Read-only; no flash, no runtime needed.

    uv run --with python-rtmidi python -m tools.device.dkscreen --port Digitakt screen.png
    uv run --with python-rtmidi python -m tools.device.dkscreen --port Digitakt --every 2 --count 5 shots/screen.png   # shots/screen-000.png, ...
    uv run python -m tools.device.dkscreen --emu screen.png                                                              # the running emulator

The reply carries a 9-byte header (format 1, width 128, height 64, length 1024) and the 1024 bytes of the firmware's OLED shadow buffer (docs/MIDI-SYSEX-RPC.md sections 5 and 11): 128x64, 1 bit per pixel,
the layout of the emulator's own frame capture (`framebuffer.get`; byte (7 - y/8) + 8 x, bit y mod 8). The PNG is scaled up (`--scale`, default 4).
The framing is `tools.device.devrpc` (device 0x10, 8-in-7 packed body); the emulator transport is `midi.usb.in` / `midi.usb.out`, as for dkupload.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from tools.control.digi_client import oled_png
from tools.device import devrpc
from tools.device.dkupload import split_messages

SCREENSHOT = 0x04
SHADOW_BYTES = 1024


def request_frame(seq: int = 1) -> bytes:
    return devrpc.build_request(SCREENSHOT, seq=seq)


HEADER = bytes.fromhex(
    "01 0080 0040 00000400".replace(" ", "")
)  # format 1, u16 width 128, u16 height 64, u32 length 1024 (big endian)


def decode(msg: bytes) -> bytes:
    """The 1024 shadow-buffer bytes of one Screenshot reply message (F0 ... F7): the reply is a 9-byte header, then the buffer."""
    parsed = devrpc.parse_reply(msg)
    if parsed is None or parsed[0] != SCREENSHOT:
        raise ValueError("not a Screenshot reply")
    payload = parsed[1]
    if payload[: len(HEADER)] != HEADER or len(payload) != len(HEADER) + SHADOW_BYTES:
        raise ValueError(
            f"unexpected Screenshot reply ({len(payload)} bytes): {payload[:16].hex()}"
        )
    return payload[len(HEADER) :]


def emu_screenshot(
    emu: Any, step: int = 400_000, limit: int = 4_000_000_000, seq: int = 1
) -> bytes:
    """Ask a running emulator over its USB MIDI path; time passes only while stepping."""
    emu.usb_out()  # drop what is queued
    emu.usb_in(request_frame(seq))
    buf = bytearray()
    spent = 0
    while spent < limit:
        buf += emu.usb_out()
        for m in split_messages(buf):
            if devrpc.parse_reply(m) and devrpc.parse_reply(m)[0] == SCREENSHOT:
                return decode(m)
        emu.step(step)
        spent += step
    raise TimeoutError("no Screenshot reply from the emulator")


def rtmidi_screenshot(mi: Any, mo: Any, wait: float = 3.0, seq: int = 1) -> bytes:
    while mi.get_message():
        pass
    mo.send_message(list(request_frame(seq)))
    end = time.monotonic() + wait
    while time.monotonic() < end:
        m = mi.get_message()
        if not m:
            time.sleep(0.003)
            continue
        msg = bytes(m[0])
        p = devrpc.parse_reply(msg)
        if p and p[0] == SCREENSHOT:
            return decode(msg)
    raise TimeoutError("no Screenshot reply from the device")


def png_of(shadow: bytes, scale: int = 4) -> bytes:
    return oled_png(shadow, scale)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--emu", action="store_true", help="the running emulator")
    where.add_argument(
        "--port", help="MIDI port name substring (a real device; python-rtmidi)"
    )
    ap.add_argument("--run-dir", type=Path, help="with --emu: a daemon's run directory")
    ap.add_argument(
        "out",
        type=Path,
        help="PNG file (with --every/--count: NAME-000.png, NAME-001.png, ...)",
    )
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--every", type=float, help="seconds between shots (needs --count)")
    ap.add_argument("--count", type=int, default=1, help="number of shots (default 1)")
    args = ap.parse_args(argv)
    if args.every is not None and args.count < 2:
        ap.error("--every needs --count 2 or more")

    mi = mo = emu = None
    if args.emu:
        from tools.device.dkupload import _emu

        emu = _emu(args.run_dir)
    else:
        mi, mo = devrpc.open_ports(args.port)
    try:
        for i in range(args.count):
            try:
                shadow = (
                    emu_screenshot(emu) if emu else rtmidi_screenshot(mi, mo, seq=1 + i)
                )
            except (TimeoutError, ValueError) as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            path = (
                args.out
                if args.count == 1
                else args.out.with_name(f"{args.out.stem}-{i:03d}{args.out.suffix}")
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png_of(shadow, args.scale))
            print(path, flush=True)
            if i + 1 < args.count:
                time.sleep(args.every or 0)
    finally:
        if mi is not None:
            mi.close_port()
            mo.close_port()
    return 0


if __name__ == "__main__":
    sys.exit(main())
