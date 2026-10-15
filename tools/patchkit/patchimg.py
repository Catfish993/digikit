#!/usr/bin/env python3
"""Apply byte-exact patches to an extracted firmware section image.

This is the "patch" half of a patch-then-verify loop; `unicorn-reference-final:emu/checkpoint.py`
and `unicorn-reference-final:tools/bootcheck.py` are the "verify" half. This tool does not run
anything or claim a patch is correct -- it only applies bytes and reports
what it changed.

MAIN OS is position-dependent ColdFire code full of absolute addresses, so
bytes may be replaced but never inserted or deleted. Every patch here must
preserve length exactly; the tool refuses anything that would not.

Every patch must state the bytes it expects to find at its address, and is
refused if they are not there. A patch that lands on the wrong address is
the failure mode that matters: it will usually still boot, and be wrong in
a way no test catches. Verifying the original bytes before touching them is
what turns an address typo loud instead of silent.

Addresses are given as guest load addresses (default base 0x40000400), not
file offsets, because that is what Ghidra and `tools/coldfire/symbols.py` report. This
tool converts load address to file offset by subtracting the base.

Patching the extracted section is enough to boot the result in the
emulator. Getting a patched image onto real hardware additionally needs the
container repacked, which this tool does NOT do.

Sections 2 (DSP/bootstrap) and 4 (UPDATER) are refused unconditionally:
section 2 holds the bootstrap version word that gates the device's only
irreversible operation, and section 4 is the flash-programming stub.
Identification is by sha256 of the pristine extracted sections, falling
back to content signatures that also catch an already-patched image.
`--force` does not bypass this guard.

Usage:
    uv run python -m tools.patchkit.patchimg --image IN.bin --out OUT.bin \\
        --str 0x40201e46=EQUALIZER:EQUALISER \\
        --bytes 0x40123456=4e71:4e75 \\
        --load-addr 0x40000400 --json manifest.json
"""

import argparse
import hashlib
import json
import os
import sys

DEFAULT_LOAD_ADDR = 0x40000400

FORBIDDEN_SECTIONS = {
    2: (
        "DSP/bootstrap",
        "it holds the bootstrap version word that gates the "
        "device's only irreversible operation",
    ),
    4: ("UPDATER", "it is the flash-programming stub"),
}

KNOWN_SECTION_SHA256 = {
    "1cd6ec15e01275dd7094be8ba1235139f6e2b66606e9fac9948a3e67cd26a68a": 2,
    "6a6a887b0573a557b71badf32cd9392777c60b4d1f33dfae12bb8346a014a37b": 3,
    "dd7560d4bc6e75eaa5857b625cfce0e9980f4c2900e3f29acbb24916fc31215d": 4,
    "a35dd785e6d681c9ab47fbde5a38bf872b296ef5c2f93d36d27d58e5d5e115d4": 5,
    "6d4316cddd41edef7a136c10d270313882028a59b96cc8fe97a716b949a7d551": 7,
}


def identify_section(image):
    """Identify which firmware section `image` is, or return None.

    First tries an exact sha256 match against the pristine extracted
    sections (KNOWN_SECTION_SHA256). If that misses, falls back to content
    signatures that only cover the forbidden sections (2 and 4) -- these
    survive an already-patched image, so a patch-then-patch-again chain is
    still caught. An unrecognised image returns None; None is NOT a
    guarantee of safety, only an absence of a positive identification.
    """
    digest = hashlib.sha256(image).hexdigest()
    if digest in KNOWN_SECTION_SHA256:
        return KNOWN_SECTION_SHA256[digest]
    if b"READY TO RECEIVE" in image and b"STARTUP MENU" in image:
        return 2
    if (
        len(image) == 32768
        and image[:8] == b"\x00" * 8
        and image[8:12] == b"\x46\xfc\x27\x00"
    ):
        return 4
    return None


def check_section_allowed(image, declared_id=None):
    """Refuse to proceed if `image` is (or is declared to be) section 2 or 4.

    Returns the effective section id (detected, else declared, else None)
    if the image is allowed. Raises SystemExit with a multi-line message
    otherwise. --force does not override this check.
    """

    def _refuse(section_id, how):
        label, why = FORBIDDEN_SECTIONS[section_id]
        raise SystemExit(
            "refusing to patch section %d (%s)\n"
            "%s; %s\n"
            "--force does not override this." % (section_id, label, how, why)
        )

    if declared_id is not None and declared_id in FORBIDDEN_SECTIONS:
        _refuse(declared_id, "declared via --section-id %d" % declared_id)

    detected_id = identify_section(image)
    if detected_id in FORBIDDEN_SECTIONS:
        digest = hashlib.sha256(image).hexdigest()
        how = "sha256 match" if digest in KNOWN_SECTION_SHA256 else "content signature"
        _refuse(detected_id, "detected via %s" % how)

    if (
        declared_id is not None
        and detected_id is not None
        and declared_id != detected_id
    ):
        raise SystemExit(
            "refusing to patch: --section-id %d does not match detected "
            "section %d\n"
            "the operator appears to be confused about what file this is."
            % (declared_id, detected_id)
        )

    return detected_id if detected_id is not None else declared_id


def load_image(path):
    with open(path, "rb") as f:
        return f.read()


def _parse_addr_spec(spec, sep="="):
    if sep not in spec:
        raise ValueError("malformed spec (expected ADDR%sOLD:NEW): %r" % (sep, spec))
    addr_str, rest = spec.split(sep, 1)
    addr = int(addr_str, 0)
    if ":" not in rest:
        raise ValueError("malformed spec (expected OLD:NEW): %r" % spec)
    old, new = rest.split(":", 1)
    return addr, old, new


def parse_str_patch(spec, load_addr):
    """Parse a `--str ADDR=OLD:NEW` spec into a patch dict.

    OLD and NEW are ASCII strings and must be equal length. Raises
    ValueError on malformed input; existence of OLD in the image and the
    trailing-NUL check happen later, in validate().
    """
    addr, old, new = _parse_addr_spec(spec)
    if len(old) != len(new):
        raise ValueError(
            "--str length mismatch (%d vs %d): %r" % (len(old), len(new), spec)
        )
    old_bytes = old.encode("ascii")
    new_bytes = new.encode("ascii")
    return {
        "kind": "str",
        "addr": addr,
        "offset": addr - load_addr,
        "old": old_bytes,
        "new": new_bytes,
        "length": len(old_bytes),
    }


def parse_bytes_patch(spec, load_addr):
    """Parse a `--bytes ADDR=OLDHEX:NEWHEX` spec into a patch dict."""
    addr, old, new = _parse_addr_spec(spec)
    if len(old) != len(new):
        raise ValueError(
            "--bytes hex length mismatch (%d vs %d): %r" % (len(old), len(new), spec)
        )
    old_bytes = bytes.fromhex(old)
    new_bytes = bytes.fromhex(new)
    if len(old_bytes) != len(new_bytes):
        raise ValueError(
            "--bytes byte length mismatch (%d vs %d): %r"
            % (len(old_bytes), len(new_bytes), spec)
        )
    return {
        "kind": "bytes",
        "addr": addr,
        "offset": addr - load_addr,
        "old": old_bytes,
        "new": new_bytes,
        "length": len(old_bytes),
    }


def validate(image, patches):
    """Check all patches against `image`, returning a list of error strings.

    Checks: address inside the image, expected old bytes present (for
    --str, also that the byte after OLD is NUL), and no two patches
    overlap. Collects every failure instead of stopping at the first.
    """
    errors = []
    for p in patches:
        offset = p["offset"]
        length = p["length"]
        if offset < 0 or offset + length > len(image):
            errors.append(
                "%s patch @0x%08x: offset %d..%d outside image (size %d)"
                % (p["kind"], p["addr"], offset, offset + length, len(image))
            )
            continue
        actual = image[offset : offset + length]
        if actual != p["old"]:
            errors.append(
                "%s patch @0x%08x: expected %s, found %s"
                % (p["kind"], p["addr"], p["old"].hex(), actual.hex())
            )
            continue
        if p["kind"] == "str":
            end = offset + length
            if end >= len(image) or image[end] != 0:
                errors.append(
                    "%s patch @0x%08x: byte after OLD is not NUL "
                    "(found 0x%02x) -- OLD may be a prefix of a longer string"
                    % (p["addr"], p["addr"], image[end] if end < len(image) else -1)
                )

    spans = sorted(
        ((p["offset"], p["offset"] + p["length"], p) for p in patches),
        key=lambda t: t[0],
    )
    for i in range(1, len(spans)):
        prev_start, prev_end, prev_p = spans[i - 1]
        start, end, p = spans[i]
        if start < prev_end:
            errors.append(
                "overlapping patches: %s @0x%08x and %s @0x%08x"
                % (prev_p["kind"], prev_p["addr"], p["kind"], p["addr"])
            )

    return errors


def apply_patches(image, patches):
    """Return a new bytes object with every patch in `patches` applied."""
    result = bytearray(image)
    for p in patches:
        offset = p["offset"]
        result[offset : offset + p["length"]] = p["new"]
    return bytes(result)


def _format_old_new(p):
    if p["kind"] == "str":
        return "%r -> %r" % (p["old"].decode("ascii"), p["new"].decode("ascii"))
    return "%s -> %s" % (p["old"].hex(), p["new"].hex())


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--image", required=True, help="input section image")
    ap.add_argument("--out", required=True, help="output path")
    ap.add_argument(
        "--load-addr",
        type=lambda s: int(s, 0),
        default=DEFAULT_LOAD_ADDR,
        help="guest load address of image[0] (default 0x%x)" % DEFAULT_LOAD_ADDR,
    )
    ap.add_argument(
        "--str",
        dest="strs",
        action="append",
        default=[],
        metavar="ADDR=OLD:NEW",
        help="ASCII string patch, repeatable",
    )
    ap.add_argument(
        "--bytes",
        dest="byteses",
        action="append",
        default=[],
        metavar="ADDR=OLDHEX:NEWHEX",
        help="raw byte patch, repeatable",
    )
    ap.add_argument(
        "--force", action="store_true", help="allow overwriting an existing --out"
    )
    ap.add_argument(
        "--section-id",
        type=int,
        default=None,
        help="declare which firmware section --image is; refuses sections 2 and 4",
    )
    ap.add_argument("--json", help="write the patch manifest as JSON")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="verify and report, but do not write --out",
    )
    args = ap.parse_args(argv)

    if os.path.exists(args.out) and not args.force and not args.dry_run:
        print(
            "refusing to overwrite existing file: %s (use --force)" % args.out,
            file=sys.stderr,
        )
        return 2

    patches = []
    parse_errors = []
    for spec in args.strs:
        try:
            patches.append(parse_str_patch(spec, args.load_addr))
        except ValueError as e:
            parse_errors.append(str(e))
    for spec in args.byteses:
        try:
            patches.append(parse_bytes_patch(spec, args.load_addr))
        except ValueError as e:
            parse_errors.append(str(e))
    if parse_errors:
        print("patch spec errors:", file=sys.stderr)
        for e in parse_errors:
            print("  %s" % e, file=sys.stderr)
        return 2

    image = load_image(args.image)
    section_id = check_section_allowed(image, args.section_id)
    if section_id is not None:
        print("identified as section %d" % section_id, file=sys.stderr)

    errors = validate(image, patches)
    if errors:
        print("validation errors:", file=sys.stderr)
        for e in errors:
            print("  %s" % e, file=sys.stderr)
        return 2

    patched = apply_patches(image, patches)
    sha_before = hashlib.sha256(image).hexdigest()
    sha_after = hashlib.sha256(patched).hexdigest()
    bytes_changed = sum(p["length"] for p in patches)

    manifest = {
        "image": args.image,
        "out": args.out,
        "load_addr": args.load_addr,
        "section_id": section_id,
        "sha256_before": sha_before,
        "sha256_after": sha_after,
        "bytes_changed": bytes_changed,
        "patches": [
            {
                "kind": p["kind"],
                "addr": p["addr"],
                "offset": p["offset"],
                "old": p["old"].hex(),
                "new": p["new"].hex(),
                "length": p["length"],
            }
            for p in patches
        ],
    }

    if args.json:
        with open(args.json, "w") as f:
            json.dump(manifest, f, indent=2)

    if not args.dry_run:
        with open(args.out, "wb") as f:
            f.write(patched)

    print(
        "=== %s -> %s%s ==="
        % (args.image, args.out, " (dry-run)" if args.dry_run else "")
    )
    print("sha256_before=%s" % sha_before)
    print("sha256_after=%s" % sha_after)
    print("bytes_changed=%d  patches=%d" % (bytes_changed, len(patches)))
    print()
    for p in patches:
        print(
            "%-6s @0x%08x  len=%-4d  %s"
            % (p["kind"], p["addr"], p["length"], _format_old_new(p))
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
