"""Command line."""

from __future__ import annotations

import argparse
import json
import os
import sys

from tools.sharc_dev import sharcinv

from .dossier import build_dossier, load_context, print_dossier
from .queue import build_engine_queue, canonical_json_bytes, write_batch_dossiers
from .regions import EXPECTED_SHA256, _here

# --- CLI ----------------------------------------------------------------


def _int(text: str) -> int:
    return int(text, 0)


def _int_list(text: str):
    return tuple(int(x, 0) for x in text.split(","))


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("blob")
    ap.add_argument("addr", nargs="?", type=_int, help="short-word (VISA PC) address")
    ap.add_argument(
        "--batch",
        type=_int_list,
        help="comma-separated addresses; writes one dossier per address",
    )
    ap.add_argument(
        "--out-dir", help="directory for --batch output (required with --batch)"
    )
    ap.add_argument(
        "--engine-queue",
        action="store_true",
        help="build deterministic Phase A engine candidate queue",
    )
    ap.add_argument(
        "--sqlite", help="optional read-only SQLite/Ghidra evidence for --engine-queue"
    )
    ap.add_argument(
        "--dossier-dir", help="write dossiers for undocumented queue candidates"
    )
    ap.add_argument(
        "--notes-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(_here)), "docs", "findings", "functions"),
        help="per-function notes directory for --engine-queue",
    )
    ap.add_argument("--blocks", type=_int_list, default=sharcinv.CODE_BLOCKS)
    ap.add_argument("--min-depth", type=int, default=8)
    ap.add_argument("--json", help="write the single-address dossier as JSON")
    ap.add_argument(
        "--listing", action="store_true", help="print the annotated instruction listing"
    )
    ap.add_argument(
        "--no-listing",
        action="store_true",
        help="suppress the listing in --batch JSON output",
    )
    args = ap.parse_args(argv)

    modes = sum((args.batch is not None, args.addr is not None, args.engine_queue))
    if modes != 1:
        ap.error("give exactly one of ADDR, --batch, or --engine-queue")
    if args.batch and not args.out_dir:
        ap.error("--batch requires --out-dir")
    if args.dossier_dir and not args.engine_queue:
        ap.error("--dossier-dir requires --engine-queue")
    if not os.path.isfile(args.blob):
        ap.error("firmware blob is absent or not a file: %s" % args.blob)

    ctx = load_context(args.blob, args.blocks, args.min_depth)
    print(
        "loaded %s: sha256=%s (%s), %d functions across blocks %s"
        % (
            args.blob,
            ctx["sha256"],
            EXPECTED_SHA256.get(ctx["sha256"], "unrecognised image"),
            len(ctx["functions"]),
            list(args.blocks),
        )
    )

    if args.engine_queue:
        queue = build_engine_queue(ctx, args.notes_dir, args.sqlite)
        output = canonical_json_bytes(queue)
        if args.json:
            parent = os.path.dirname(args.json)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.json, "wb") as fh:
                fh.write(output)
            print("wrote %s" % args.json)
        else:
            sys.stdout.buffer.write(output)
        if args.dossier_dir:
            undocumented = [
                int(row["entry"], 16)
                for row in queue["candidates"]
                if not row["documented"]
            ]
            write_batch_dossiers(
                ctx, undocumented, args.dossier_dir, want_listing=not args.no_listing
            )
        return 0

    if args.addr is not None:
        want_listing = not args.no_listing
        d = build_dossier(ctx, args.addr, want_listing=want_listing)
        print_dossier(d, show_listing=args.listing or want_listing)
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(d, fh, indent=1)
            print("\nwrote %s" % args.json)
        return 0

    write_batch_dossiers(
        ctx, args.batch, args.out_dir, want_listing=not args.no_listing
    )
    return 0
