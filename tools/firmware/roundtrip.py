#!/usr/bin/env python3
"""Acceptance gate for the repack chain: rebuild a firmware .syx from
extracted sections, then re-extract the rebuilt container with tools/firmware/elz.py
and prove every section comes back byte-identical.

What this proves: the container write path -- tools/firmware/build.py's packing,
its ELE3 table, the per-section storage rules, 8-in-7 encoding, SysEx
framing -- reproduces content that tools/firmware/elz.py, the decoder verified against the
device's own depacker, decodes to exactly the bytes it started from.

The preamble's 32-bit content checksum, the container's 32-byte
HMAC-SHA256 trailer, and each SysEx message's byte-125 checksum are all
now recovered and computed by the write path (tools/firmware/build.py, tools/firmware/authcode.py);
this gate verifies all three against the rebuilt image.

What this does NOT prove: that the rebuilt .syx is acceptable to a real
device. No rebuilt image has ever been flashed to hardware. The final
chunk's zero padding remains an inference, since no sample firmware has a
partial final chunk to confirm it against. And the store-only packer in
tools/firmware/aplib.py makes the image roughly three times its original size; the
receive code imposes no size limit, but the physical DDR and flash
capacities on the device are not established. Flashing the output of this
tool is untested -- proceed at your own risk.

--quick skips MAIN OS (section 3, ~3.1 MB); its result is a partial gate,
not a pass on the real thing.

    uv run python -m tools.firmware.roundtrip FIRMWARE.syx
        [--sections DIR]      default: out/sections/<image key>/ of FIRMWARE.syx
        [--out FILE]          also write the rebuilt .syx here
        [--accept-sections]   skip the provenance check against FIRMWARE.syx
        [--quick]             skip section 3 (MAIN OS); partial gate only
"""

import argparse
import hashlib
import os
import struct
import sys
import tempfile

from tools.firmware import build, config
from tools.firmware.authcode import DEVICE_BY_ID, DEVICES, compute_trailer, derive_key
from tools.firmware.container import container as C
from tools.firmware.container import decode_syx
from tools.firmware.elz import depack_section
from tools.firmware.extract import NAMES, SOURCE_MARKER, classify

TABLE_OFF = 0x20


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_provenance(syx, sections_dir, accept):
    """Refuse to run against sections that were extracted from a different
    .syx -- see unicorn-reference-final:emu/run.py's need_matching_sections, which this follows."""
    if accept:
        return
    marker_path = os.path.join(sections_dir, SOURCE_MARKER)
    digest = sha256(syx)
    if not os.path.exists(marker_path):
        raise SystemExit(
            "No %s in %s/, so it is not known which firmware these sections\n"
            "came from. Re-extract, or pass --accept-sections if you are\n"
            "certain they match %s." % (SOURCE_MARKER, sections_dir, syx)
        )
    was = open(marker_path).read().strip()  # noqa: SIM115
    if was != digest:
        raise SystemExit(
            "%s/ belongs to a different firmware image.\n\n"
            "  sections came from  %s\n"
            "  you asked for       %s\n\n"
            "Verifying one device's rebuild against another device's\n"
            "sections would produce a meaningless failure. Re-extract, or\n"
            "pass --accept-sections if you are certain they match."
            % (sections_dir, was[:16], digest[:16])
        )


def load_sections(sections_dir):
    """-> {section_id: decompressed bytes}, one per id in tools/firmware/extract.py's
    NAMES, read from the fixed section_<id>_<name>.bin filenames."""
    orig = {}
    for sid, name in NAMES.items():
        path = os.path.join(sections_dir, "section_%d_%s.bin" % (sid, name))
        if not os.path.exists(path):
            raise SystemExit("missing extracted section: %s" % path)
        with open(path, "rb") as fh:
            orig[sid] = fh.read()
    return orig


def check_authenticity(out, syx_path):
    """Verify the three fields the write path computes -- the preamble
    content checksum, the HMAC-SHA256 trailer, and the per-message byte-125
    checksum -- against the rebuilt .syx `out` (already written to
    `syx_path`). Prints one result line per check, returns True iff all
    four pass (the framing message count is checked at both ends)."""
    dec = decode_syx(syx_path)
    total_len, stored_checksum = struct.unpack_from(">II", dec, 0)
    container = dec[8:]
    computed_checksum = build.content_checksum(container[:total_len])
    ok_preamble = (
        stored_checksum == computed_checksum
        and total_len % 16 == 0
        and total_len <= len(container)
    )

    messages = []
    i = 0
    while i < len(out):
        if out[i] != 0xF0:
            raise ValueError("expected F0 at offset %d" % i)
        j = out.index(0xF7, i)
        messages.append(out[i : j + 1])
        i = j + 1
    first_framing, last_framing = messages[0], messages[-1]
    data_msgs = [m for m in messages if len(m) == 128]

    device_id = first_framing[1:-1][3]
    device = DEVICE_BY_ID.get(device_id)
    trailer = container[total_len - 32 : total_len]
    ok_hmac = (
        device is not None
        and compute_trailer(dec, derive_key(**DEVICES[device])) == trailer
    )

    def framing_count(msg):
        body = msg[1:-1]
        return body[11] * 16384 + body[12] * 128 + body[13]

    ok_count = (
        len(data_msgs) > 0
        and framing_count(first_framing) == len(data_msgs)
        and framing_count(last_framing) == len(data_msgs)
    )

    k = first_framing[1:-1][7]
    passed = sum(
        1 for m in data_msgs if m[1:-1][125] == build.packet_checksum(m[1:-1], k)
    )
    ok_packets = len(data_msgs) > 0 and passed == len(data_msgs)

    print()
    print("%-20s %s" % ("preamble checksum", "OK" if ok_preamble else "MISMATCH"))
    print("%-20s %s" % ("HMAC trailer", "OK" if ok_hmac else "MISMATCH"))
    print("%-20s %s" % ("framing count", "OK" if ok_count else "MISMATCH"))
    print("%-20s %d/%d" % ("packet checksums", passed, len(data_msgs)))

    return ok_preamble and ok_hmac and ok_count and ok_packets


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("syx", help="the firmware .syx to round-trip")
    ap.add_argument(
        "--sections",
        help="directory of extracted sections (default: out/sections/<image key>/)",
    )
    ap.add_argument("--out", help="also write the rebuilt .syx here")
    ap.add_argument(
        "--accept-sections",
        action="store_true",
        help="skip the provenance check against FIRMWARE.syx",
    )
    ap.add_argument(
        "--quick",
        action="store_true",
        help="skip section 3 (MAIN OS); partial gate only, a few seconds",
    )
    args = ap.parse_args(argv)

    args.sections = args.sections or str(config.sections_for(args.syx))
    check_provenance(args.syx, args.sections, args.accept_sections)
    orig = load_sections(args.sections)

    out = build.rebuild(args.syx, replacements=dict(orig))

    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(out)

    fd, tmp_path = tempfile.mkstemp(suffix=".syx")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(out)

        c = C(tmp_path)
        n = struct.unpack_from(">I", c, 0x1C)[0]

        print(
            "%-4s %-10s %-8s %12s %12s  %s"
            % ("id", "name", "kind", "decomp len", "dest", "result")
        )
        all_ok = True
        for k in range(n):
            sid, off, clen, dest = struct.unpack_from(">IIII", c, TABLE_OFF + 16 * k)
            name = NAMES.get(sid, "?")
            if args.quick and sid == 3:
                print(
                    "%-4d %-10s %-8s %12s %#12x  %s"
                    % (sid, name, "-", "-", dest, "SKIPPED (quick)")
                )
                continue
            kind, payload = classify(bytes(c[off : off + clen]))
            got = depack_section(payload) if kind == "packed" else payload
            ok = got == orig[sid]
            all_ok = all_ok and ok
            print(
                "%-4d %-10s %-8s %12d %#12x  %s"
                % (sid, name, kind, len(got), dest, "OK" if ok else "MISMATCH")
            )
        all_ok = check_authenticity(out, tmp_path) and all_ok
    finally:
        os.remove(tmp_path)

    src_size = os.path.getsize(args.syx)
    out_size = len(out)
    print()
    print(
        "source %d bytes, rebuilt %d bytes (%.2fx)"
        % (src_size, out_size, out_size / src_size)
    )

    if args.quick:
        print()
        print("PARTIAL GATE (--quick): section 3 (MAIN OS) was not verified.")

    print()
    if all_ok:
        print("PASS" + (" (partial)" if args.quick else ""))
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
