"""Derive the deliberately small early-init MMIO contract from explicit MAIN images.

This is a narrow static-analysis fallback, not a whole-program disassembler.  It
accepts only the known bootstrap instruction sequence and fails rather than
silently guessing when it changes.
"""

import argparse
import hashlib
import json
from pathlib import Path

from tools.coldfire.disasm import disasm

LOAD_ADDRESS = 0x40000400
START, END = 0x400004E8, 0x40000530
SCHEMA = "digitakt2.early-init-analysis.v1"
# Reviewed bootstrap write sites and register identities. Operands are decoded
# from each image, never copied from this table.
WRITE_SITES = (
    (0x40000500, "PPMCR0", "immediate-a0"),
    (0x40000504, "PPMCR1", "d0-absolute"),
    (0x4000050A, "PPMCR0", "immediate-a0"),
    (0x4000050E, "PPMCR0", "immediate-a0"),
    (0x40000512, "PPMCR0", "immediate-a0"),
    (0x40000516, "PPMCR0", "immediate-a0"),
    (0x4000051A, "PPMCR0", "immediate-a0"),
)
HERE = Path(__file__).resolve().parents[2]
CONTRACT_PATH = HERE / "docs/contracts/early-init-v1.json"


def _number(value):
    return int(value, 0) if isinstance(value, str) else value


def load_contract(path=CONTRACT_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_contract(contract):
    required = {
        "contract",
        "schema_version",
        "range",
        "registers",
        "firmware",
        "static_write_events",
        "static_read_boundary",
        "qemu_provenance",
        "acceptance_records",
    }
    missing = required - contract.keys()
    if missing or contract["schema_version"] != 1:
        raise ValueError("invalid early-init contract: missing %s" % sorted(missing))
    writes = contract["static_write_events"]
    if len(writes) != 7 or any(e["direction"] != "write" for e in writes):
        raise ValueError("invalid early-init contract: exactly seven writes required")
    boundary = contract["static_read_boundary"]
    if boundary["direction"] != "read" or _number(boundary["address"]) != 0xEC09000E:
        raise ValueError("invalid early-init contract: unknown read boundary required")
    for name in ("PPMCR0", "PPMCR1", "UNKNOWN_0xec09000e"):
        if name not in contract["registers"]:
            raise ValueError("invalid early-init contract: missing register " + name)
    acceptance = contract["acceptance_records"]
    if not any(
        _number(r.get("address", 0)) == 0xFC0CC02C
        and _number(r.get("mask", 0)) == 0x08000000
        and _number(r.get("site", 0)) == 0x4012001E
        for r in acceptance
    ):
        raise ValueError(
            "invalid early-init contract: eSDHC INITA acceptance record required"
        )
    return contract


def _event(pc, address, width, direction, value, register, **extra):
    result = {
        "address": "0x%08x" % address,
        "direction": direction,
        "pc": "0x%08x" % pc,
        "register": register,
        "value": value,
        "width": width,
    }
    result.update(extra)
    return result


def analyze_bytes(image, *, contract=None):
    """Return the seven PPM writes and one unknown read, or raise ValueError.

    Existing ``tools.coldfire.disasm`` decoding supplies instruction boundaries.  The
    matching intentionally retains no data-flow beyond this fixed bootstrap:
    a different sequence requires a new reviewed contract instead of inference.
    """
    contract = validate_contract(contract or load_contract())
    if len(image) < END - LOAD_ADDRESS:
        raise ValueError("MAIN image does not contain early-init range")
    ins = list(disasm(image, LOAD_ADDRESS, START, END))
    got = {pc: raw for pc, raw, _mnemonic, _operands in ins}
    # Fixed instruction forms establish the a0 base, d0 value, and boundary.
    moveq, lea, boundary, read = (
        got.get(pc) for pc in (0x400004F8, 0x400004FA, 0x4000051E, 0x40000524)
    )
    if (
        moveq is None
        or len(moveq) != 4
        or not moveq.startswith("70")
        or lea is None
        or len(lea) != 12
        or not lea.startswith("41f9")
        or boundary != "41f9ec09000e"
        or read != "3010"
    ):
        raise ValueError("early-init sequence mismatch")
    a0 = int(lea[4:], 16)
    d0 = int(moveq[2:], 16)
    decoded = []
    for pc, register, form in WRITE_SITES:
        raw = got.get(pc)
        if raw is None:
            raise ValueError("early-init sequence mismatch at 0x%08x" % pc)
        if form == "immediate-a0":
            if len(raw) != 8 or raw[:6] != "10bc00":
                raise ValueError("early-init write form mismatch at 0x%08x" % pc)
            address, value = a0, int(raw[6:], 16)
        else:
            if len(raw) != 12 or raw[:4] != "13c0":
                raise ValueError("early-init write form mismatch at 0x%08x" % pc)
            address, value = int(raw[4:], 16), d0
        decoded.append((pc, address, 8, "write", value, register))
    contract_writes = [
        (
            _number(e["pc"]),
            _number(e["address"]),
            e["width"],
            e["direction"],
            _number(e["value"]),
            e["register"],
        )
        for e in contract["static_write_events"]
    ]
    if contract_writes != decoded:
        raise ValueError(
            "early-init contract write disagreement with instruction bytes"
        )
    records = [
        _event(
            pc,
            address,
            width,
            direction,
            "0x%x" % value,
            register,
            provenance=["DOCUMENTED", "STATIC_ANALYSIS_FALLBACK"],
        )
        for pc, address, width, direction, value, register in decoded
    ]
    b = contract["static_read_boundary"]
    records.append(
        _event(
            _number(b["pc"]),
            _number(b["address"]),
            b["width"],
            "read",
            None,
            b["register"],
            outcome=b["outcome"],
            provenance=["STATIC_ANALYSIS_FALLBACK"],
        )
    )
    return records


def join_qemu(records, firmware_sha256, seed):
    """Join exact QEMU observations without changing static evidence truth."""
    dynamic = seed.get("events", seed.get("qemu_events", []))
    seed_sha = seed.get("firmware_sha256") or seed.get("firmware", {}).get(
        "digitaktMainSha256"
    )
    keys = {
        (
            e.get("firmware_sha256", seed_sha),
            _number(e["pc"]),
            _number(e["address"]),
            e["direction"],
            e.get("width", e.get("size")),
            _number(e["value"]) if e.get("value") is not None else None,
        )
        for e in dynamic
    }
    output = []
    for event in records:
        value = _number(event["value"]) if event["value"] is not None else None
        key = (
            firmware_sha256,
            _number(event["pc"]),
            _number(event["address"]),
            event["direction"],
            event["width"] // 8,
            value,
        )
        # Seed values size is bytes while contract width is bits.
        joined = key in keys
        copy = dict(event)
        copy["provenance"] = sorted(
            set(copy["provenance"]) | ({"DYNAMIC_QEMU"} if joined else set())
        )
        copy["qemu_joined"] = joined
        output.append(copy)
    return output


def analyze_path(path, *, contract=None, seed=None):
    contract = validate_contract(contract or load_contract())
    image = Path(path).read_bytes()
    sha = hashlib.sha256(image).hexdigest()
    records = analyze_bytes(image, contract=contract)
    # QEMU is evidence only for the Digitakt hash.  A seed may contain a full
    # firmware tag on each event, which makes it usable outside this repo.
    if seed and sha == contract["firmware"]["digitakt_main_sha256"]:
        records = join_qemu(records, sha, seed)
    else:
        records = [dict(e, qemu_joined=False) for e in records]
    relocation = image[0x400004EC - LOAD_ADDRESS : 0x400004F2 - LOAD_ADDRESS].hex()
    return {
        "analysis_provenance": "STATIC_ANALYSIS_FALLBACK",
        "events": records,
        "firmware_sha256": sha,
        "image": str(Path(path)),
        "range": contract["range"],
        "relocation_store_encoding": relocation,
        "schema": SCHEMA,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--main", action="append", required=True, help="explicit MAIN image path"
    )
    p.add_argument("--out", required=True, help="output JSON path (never stdout)")
    p.add_argument("--qemu-seed", help="optional JSON QEMU event seed")
    args = p.parse_args(argv)
    contract = load_contract()
    seed = json.loads(Path(args.qemu_seed).read_text()) if args.qemu_seed else None
    images = sorted(
        [analyze_path(path, contract=contract, seed=seed) for path in args.main],
        key=lambda x: x["firmware_sha256"],
    )
    # Compare facts, excluding their deliberately different provenance.
    sequence = lambda item: [  # noqa: E731
        {k: e[k] for k in ("pc", "address", "width", "direction", "value", "register")}
        for e in item["events"]
    ]
    output = {
        "comparison": {
            "identical_access_sequence": len(
                {json.dumps(sequence(i), sort_keys=True) for i in images}
            )
            == 1,
            "relocation_stores": [
                {
                    "firmware_sha256": i["firmware_sha256"],
                    "encoding": i["relocation_store_encoding"],
                }
                for i in images
            ],
        },
        "images": images,
        "schema": SCHEMA,
    }
    Path(args.out).write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
