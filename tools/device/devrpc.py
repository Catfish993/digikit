#!/usr/bin/env python3
r"""Talk to a live Digitakt II over its USB MIDI port using the device-0x10
runtime RPC (the protocol Elektron Transfer uses; see docs/MIDI-SYSEX-RPC.md).

Frame (device 0x10 path, verified: no checksum, no length, body is raw bytes):

    F0 00 20 3C 10 00 <cmd> <seq_hi> <seq_lo> 09 <ctr> <type> <payload...> F7
    \___________/  |  |     \_______________/  \____/  |     |
      literal    dev sep       MidiRpc seq16    field16 |   payload
                                                        type (RPC family)

`cmd` is ignored by the receiver (all 0x00-0x7f route to one handler); `seq`
is caller-chosen; the device assigns its own seq/ctr in the reply, so replies
are matched on devid=0x10 and the type byte, not on any echo.

    # send a read-only Ping and print the reply
    uv run --with python-rtmidi python -m tools.device.devrpc --ping
    uv run --with python-rtmidi python -m tools.device.devrpc --version --uid
    uv run --with python-rtmidi python -m tools.device.devrpc --type 0x05   # StorageSpace

This transmits to the device. Only the read-only queries (ping/version/uid/
storage/query) are wired up; there is deliberately no write/upgrade command.
"""

import argparse
import sys
import time

HDR = bytes((0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00))
DEV = 0x10

TYPES = {
    0x01: "Ping",
    0x02: "SoftwareVersion",
    0x03: "DeviceUID",
    0x05: "StorageSpace",
    0x09: "Query",
    0x10: "ReadDir",
    0x53: "DataList",
}


def build_request(type_byte, seq=1, cmd=0x00, payload=b""):
    """A device-0x10 request frame, matching what Elektron Transfer sends:
    header after the literal is cmd · seq_hi seq_lo (u16) · len_hi len_lo (u16
    body length) · type · body. Requests use cmd 0x00 (the device stamps 0x24
    on replies); the reply echoes seq at bytes 9-10."""
    # bytes 9-10 after seq: Elektron Transfer always sends 0x00 0x00 here,
    # whatever the payload; the device reads the body up to F7. A non-zero
    # byte 9 (we once sent 0x09) is rejected, so keep it 0x00 0x00.
    seq_hi, seq_lo = (seq >> 8) & 0x7F, seq & 0x7F
    return bytes((*HDR, cmd, seq_hi, seq_lo, 0x00, 0x00, type_byte, *payload, 0xF7))


def parse_reply(msg):
    """Decode a device-0x10 reply. Returns (type_byte, payload_bytes) or None."""
    if len(msg) < 13 or msg[0] != 0xF0 or msg[1:4] != HDR[1:4] or msg[4] != DEV:
        return None
    if msg[-1] != 0xF7:
        return None
    type_byte = msg[11]
    payload = msg[12:-1]
    return type_byte, bytes(payload)


def _ascii(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def describe(type_byte, payload):
    name = TYPES.get(type_byte, f"type 0x{type_byte:02x}")
    lines = [
        f"  {name}: {len(payload)}B  "
        + " ".join(f"{c:02x}" for c in payload[:48])
        + (" ..." if len(payload) > 48 else "")
    ]
    if payload:
        lines.append(f"    ascii: {_ascii(payload[:64])}")
    if type_byte == 0x02:  # SoftwareVersion: NUL-terminated strings
        strs = [s.decode("latin1") for s in payload.split(b"\x00") if s]
        lines.append(f"    strings: {strs}")
    elif type_byte == 0x03 and len(payload) >= 4:  # DeviceUID: u32 BE
        uid = int.from_bytes(payload[:4], "big")
        lines.append(f"    uid: 0x{uid:08x}")
    return lines


def open_ports(sub):
    import rtmidi

    mi, mo = rtmidi.MidiIn(), rtmidi.MidiOut()
    pi = next(
        (i for i, n in enumerate(mi.get_ports()) if sub.lower() in n.lower()), None
    )
    po = next(
        (i for i, n in enumerate(mo.get_ports()) if sub.lower() in n.lower()), None
    )
    if pi is None or po is None:
        raise SystemExit(
            f"no MIDI port matching {sub!r}: in={mi.get_ports()} out={mo.get_ports()}"
        )
    mi.open_port(pi)
    mo.open_port(po)
    mi.ignore_types(sysex=False, timing=True, active_sense=True)
    return mi, mo


def request(mi, mo, type_byte, seq=1, wait=1.5, payload=b""):
    """Send one request, collect device-0x10 replies for `wait` seconds."""
    while mi.get_message():  # drain
        pass
    frame = build_request(type_byte, seq=seq, payload=payload)
    print(
        f"-> {TYPES.get(type_byte, hex(type_byte))}: "
        + " ".join(f"{b:02x}" for b in frame)
    )
    mo.send_message(list(frame))
    replies, end = [], time.time() + wait
    while time.time() < end:
        m = mi.get_message()
        if not m:
            time.sleep(0.003)
            continue
        parsed = parse_reply(bytes(m[0]))
        if parsed:
            replies.append(parsed)
    return replies


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", default="Digitakt", help="port name substring")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--uid", action="store_true")
    ap.add_argument("--storage", action="store_true")
    ap.add_argument("--query", action="store_true")
    ap.add_argument(
        "--readdir",
        metavar="PATH",
        default=None,
        help="list a device directory, e.g. --readdir /",
    )
    ap.add_argument(
        "--type",
        type=lambda s: int(s, 0),
        default=None,
        help="send an arbitrary read-only type byte",
    )
    ap.add_argument("--wait", type=float, default=1.5)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the frames that WOULD be sent, transmit nothing",
    )
    args = ap.parse_args()

    plan = []  # (type_byte, payload)
    if args.ping:
        plan.append((0x01, b""))
    if args.version:
        plan.append((0x02, b""))
    if args.uid:
        plan.append((0x03, b""))
    if args.storage:
        plan.append((0x05, b"\x01"))
    if args.query:
        plan.append((0x09, b""))
    if args.readdir is not None:
        plan.append((0x10, args.readdir.encode() + b"\x00"))
    if args.type is not None:
        plan.append((args.type, b""))
    if not plan:
        plan = [(0x01, b"")]  # default: ping

    if args.dry_run:
        for i, (t, pl) in enumerate(plan, 1):
            f = build_request(t, seq=i, payload=pl)
            print(
                f"would send {TYPES.get(t, hex(t))}: " + " ".join(f"{b:02x}" for b in f)
            )
        return 0

    mi, mo = open_ports(args.port)
    try:
        for i, (t, pl) in enumerate(plan, 1):
            replies = request(mi, mo, t, seq=i, wait=args.wait, payload=pl)
            if not replies:
                print("   <no reply>")
            for type_byte, payload in replies:
                for line in describe(type_byte, payload):
                    print(line)
    finally:
        mi.close_port()
        mo.close_port()
    return 0


if __name__ == "__main__":
    sys.exit(main())
