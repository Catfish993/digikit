#!/usr/bin/env python3
"""Parse a Snoize MIDI Monitor .mmon capture into timestamped MIDI messages.

    uv run python -m tools.device.mmon Untitled.mmon            # list all
    uv run python -m tools.device.mmon Untitled.mmon --sysex    # SysEx only, decoded

A .mmon file is a binary plist whose `messageData` is a nested
NSKeyedArchiver of SM*Message objects. Each carries its `originatingEndpoint`
name, so this also tells request (host->device) from reply (device->host):
a spy-on-output capture tags outgoing messages with the destination name.
SysEx bodies are stored without the leading 0xF0 (kept in `statusByte`).
"""
import argparse
import plistlib
import sys
from plistlib import UID

ELEKTRON = bytes((0xF0, 0x00, 0x20, 0x3C))
TYPES = {0x01: "Ping", 0x02: "SoftwareVersion", 0x03: "DeviceUID",
         0x05: "StorageSpace", 0x07: "TempoWrite", 0x09: "Query",
         0x10: "FsSampleReadDir/List", 0x53: "DataList"}


def load(path):
    """Return [(ts_nanos, endpoint_name, full_bytes)], time-sorted."""
    arch = plistlib.loads(plistlib.load(open(path, "rb"))["messageData"])
    objs = arch["$objects"]
    out = []
    for o in objs:
        if not (isinstance(o, dict) and "$class" in o and "data" in o):
            continue
        cls = objs[o["$class"].data].get("$classname", "")
        if "Message" not in cls:
            continue
        ep = objs[o["originatingEndpoint"].data] if isinstance(
            o.get("originatingEndpoint"), UID) else "?"
        body = objs[o["data"].data]
        sb = o.get("statusByte", 0)
        full = bytes([sb]) + bytes(body) if isinstance(body, (bytes, bytearray)) else b""
        out.append((o.get("timeStampInNanos", 0), ep, full))
    out.sort()
    return out


def ascii_of(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def describe_elektron(f):
    if len(f) < 12 or f[:4] != ELEKTRON:
        return None
    cmd, type_byte = f[6], f[11]
    name = TYPES.get(type_byte, f"type 0x{type_byte:02x}")
    return f"dev=0x{f[4]:02x} cmd=0x{cmd:02x} {name}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--sysex", action="store_true", help="SysEx only")
    ap.add_argument("--endpoints", action="store_true",
                    help="just summarise endpoints (direction)")
    ap.add_argument("--max-hex", type=int, default=48)
    args = ap.parse_args()

    msgs = load(args.file)
    if args.endpoints:
        from collections import Counter
        for ep, n in Counter(ep for _, ep, _ in msgs).most_common():
            print(f"{n:5d}  {ep}")
        return 0

    t0 = msgs[0][0] if msgs else 0
    for ts, ep, f in msgs:
        if args.sysex and (not f or f[0] != 0xF0):
            continue
        rel = (ts - t0) / 1e9
        head = f"[{rel:8.3f}s] {ep}"
        d = describe_elektron(f)
        if d:
            head += f"  {d}"
        hexs = f.hex(" ")
        if len(f) > args.max_hex:
            hexs = " ".join(f"{b:02x}" for b in f[:args.max_hex]) + f" ... ({len(f)}B)"
        print(f"{head}\n    {hexs}")
        body = f[12:-1] if d else f
        if any(32 <= c < 127 for c in body):
            print(f"    ascii: {ascii_of(f[:args.max_hex + 12])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
