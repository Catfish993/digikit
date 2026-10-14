"""Engine candidate queue: canonical JSON, documented entries, evidence and batch dossiers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys

from tools.sharc_dev import sharcinv

from .dossier import build_dossier, print_dossier, sha256_of
from .regions import _here

# --- deterministic Phase A engine queue ---------------------------------

ENGINE_QUEUE_SCHEMA = "sharc-engine-queue/v1"
ENGINE_MIN_INSNS = 60
ENGINE_MIN_FLOAT_MUL_MAC = 10
_NOTE_NAME_RE = re.compile(r"^blk(?P<block>\d+)-(?P<entry>[0-9a-fA-F]+)\.md$")
_NOTE_ENTRY_RE = re.compile(r"\*\*Bounds\*\*:[^\n]*?entry `0x([0-9a-fA-F]+)`")


def canonical_json_bytes(value) -> bytes:
    """Canonical queue serialization: sorted keys, UTF-8, final newline."""
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def canonical_digest(value) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def engine_candidates(functions):
    """Phase A predicate over sharcinv's finalized vector keys only."""
    selected = []
    for fn in functions:
        vector = fn["vector"]
        float_mul_mac = vector["float_mul"] + vector["mac"]
        if (
            fn["n_insns"] >= ENGINE_MIN_INSNS
            and float_mul_mac >= ENGINE_MIN_FLOAT_MUL_MAC
        ):
            selected.append(fn)
    return sorted(selected, key=lambda fn: (fn["block"], fn["entry"]))


def documented_function_entries(notes_dir):
    """Return independently bounded note entries and rejected note reasons.

    A filename alone is not enough: the note must state the same entry in its
    Bounds line.  Thus a continuation note cannot create a completed entry.
    """
    entries, rejected = set(), []
    if not os.path.isdir(notes_dir):
        return entries, [{"reason": "notes directory absent", "path": notes_dir}]
    for name in sorted(os.listdir(notes_dir)):
        match = _NOTE_NAME_RE.match(name)
        if not match:
            continue
        path = os.path.join(notes_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            rejected.append({"path": name, "reason": str(exc)})
            continue
        bounds = _NOTE_ENTRY_RE.search(text)
        expected = int(match.group("entry"), 16)
        if bounds is None:
            rejected.append({"path": name, "reason": "no bounded entry declaration"})
        elif int(bounds.group(1), 16) != expected:
            rejected.append({"path": name, "reason": "filename/bounds entry mismatch"})
        else:
            entries.add((int(match.group("block")), expected))
    return entries, rejected


def source_hashes():
    return {
        name: sha256_of(os.path.join(_here, name))
        for name in ("sharcfn.py", "sharcinv.py", "sharcflow.py", "sharcldr.py")
    }


def sqlite_evidence(sqlite_path, candidates, source_image_sha256=None):
    """Read optional SQLite evidence without modifying it.

    Facts remain advisory unless the database records the exact SHA-256 of
    the source image and it matches ``source_image_sha256``.  The join uses
    SHARC short-word addresses throughout.
    """
    result = {
        "path": sqlite_path,
        "sha256": sha256_of(sqlite_path),
        "mode": "read-only",
        "meta": [],
        "status": "usable",
        "freshness": "not_verified: no source image SHA-256 supplied",
        "evidence_status": "advisory",
        "schema_issues": [],
        "facts": {},
    }
    try:
        con = sqlite3.connect(
            "file:%s?mode=ro" % os.path.abspath(sqlite_path), uri=True
        )
        tables = {
            row[0]
            for row in con.execute("select name from sqlite_master where type='table'")
        }
        if "meta" not in tables:
            result["schema_issues"].append("missing meta table")
        else:
            result["meta"] = [
                {"key": row[0], "value": row[1]}
                for row in con.execute("select key, value from meta order by key")
            ]
            meta_values = {row["key"]: row["value"] for row in result["meta"]}
            image_sha256 = meta_values.get("image_sha256")
            if image_sha256 is None:
                result["freshness"] = (
                    "not_verified: meta lacks full source image SHA-256"
                )
            elif source_image_sha256 is None:
                result["freshness"] = "not_verified: source image SHA-256 not supplied"
            elif image_sha256 == source_image_sha256:
                result["freshness"] = "verified"
            else:
                result["freshness"] = (
                    "stale: SQLite image_sha256 does not match source image SHA-256"
                )
        needed = {"functions", "insn", "refs", "warnings", "decompiled"}
        missing = sorted(needed - tables)
        if missing:
            result["schema_issues"].append("missing tables: " + ", ".join(missing))
        if not result["schema_issues"]:
            for fn in candidates:
                entry = fn["entry"]
                function = con.execute(
                    "select name, instructions, in_main from functions where sw=?",
                    (entry,),
                ).fetchone()
                insn_count = con.execute(
                    "select count(*) from insn where function_sw=?", (entry,)
                ).fetchone()[0]
                refs = con.execute(
                    "select count(*) from refs where from_sw=? or to_sw=?",
                    (entry, entry),
                ).fetchone()[0]
                warnings = con.execute(
                    "select count(*) from warnings where function_sw=?", (entry,)
                ).fetchone()[0]
                decomp = con.execute(
                    "select completed, error from decompiled where function_sw=?",
                    (entry,),
                ).fetchone()
                result["facts"]["0x%x" % entry] = {
                    "function_present": function is not None,
                    "instruction_count": function[1] if function else insn_count,
                    "reference_count": refs,
                    "warning_count": warnings,
                    "decompiler": None
                    if decomp is None
                    else {
                        "available": True,
                        "completed": bool(decomp[0]),
                        "error": decomp[1],
                    },
                }
        else:
            result["status"] = "incompatible"
        con.close()
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "unavailable"
        result["schema_issues"].append(str(exc))

    if result["status"] == "usable" and result["freshness"] == "verified":
        result["evidence_status"] = "verified"
    for fact in result["facts"].values():
        fact["evidence_status"] = result["evidence_status"]
    return result


# Stable output schema for Phase A coverage.  These are only existing
# decoder/classifier observations; a zero means the form/opcode was absent.
TARGET_OPCODE_COVERAGE_KEYS = (
    "Type10a_rel",
    "Type10a_abs",
    "Type2b",
    "ALU_0xe0",
    "ALU_0x05",
    "ALU_0x06",
    "multifunction_0x1a",
    "multifunction_0x1e",
    "multifunction_0x1f",
    "convert_by_RY_0xd9",
    "convert_by_RY_0xda",
    "convert_by_RY_0xdd",
)


def target_opcode_coverage(ctx, candidates):
    """Count only forms/opcodes identified by existing decoder/classifier fields."""
    counts = {key: 0 for key in TARGET_OPCODE_COVERAGE_KEYS}
    unsupported = []
    candidate_ranges = {(fn["block"], fn["entry"], fn["exit"]) for fn in candidates}
    for block_id, entry, exit_ in candidate_ranges:
        block = ctx["analyzed"].get(block_id)
        if block is None:
            unsupported.append("candidate block %d was not analyzed" % block_id)
            continue
        for _sw, insn in sharcinv.instructions_in(block, entry, exit_):
            form = insn.type_name
            if form == "10a_rel":
                counts["Type10a_rel"] += 1
            elif form == "10a_abs":
                counts["Type10a_abs"] += 1
            if form == "2b":
                counts["Type2b"] += 1
            field = (
                sharcinv.merge_fields(insn.fields).get("compute")
                if form in sharcinv.COMPUTE_FORMS
                else None
            )
            if not field:
                continue
            cu, detail = sharcinv.classify_compute(field)
            opcode = detail.get("opcode")
            if cu == "ALU" and opcode in (0xE0, 0x05, 0x06):
                counts["ALU_0x%02x" % opcode] += 1
            if cu == "MULTIFN":
                selector = (field >> 16) & 0x3F
                if selector in (0x1A, 0x1E, 0x1F):
                    counts["multifunction_0x%02x" % selector] += 1
            if cu == "ALU" and opcode in (0xD9, 0xDA, 0xDD):
                counts["convert_by_RY_0x%02x" % opcode] += 1
    return {
        "derived_counts": dict(sorted(counts.items())),
        "unsupported_or_ambiguous": unsupported
        + [
            "Type10a_rel/abs are counted only if the existing decoder emits those form names",
            "convert-by-RY is limited to existing ALU classifier opcodes 0xd9, 0xda, 0xdd",
        ],
        "word_address_units": "all joins and counts use SHARC short-word addresses",
    }


def build_engine_queue(ctx, notes_dir, sqlite_path=None, target_coverage=None):
    inventory = sorted(ctx["functions"], key=lambda fn: (fn["block"], fn["entry"]))
    candidates = engine_candidates(inventory)
    documented, rejected_notes = documented_function_entries(notes_dir)
    rows = []
    for fn in candidates:
        row = {
            "block": fn["block"],
            "entry": "0x%x" % fn["entry"],
            "id": fn["id"],
            "n_insns": fn["n_insns"],
            "float_mul": fn["vector"]["float_mul"],
            "mac": fn["vector"]["mac"],
            "documented": (fn["block"], fn["entry"]) in documented,
        }
        rows.append(row)
    evidence = None
    if sqlite_path:
        if not os.path.isfile(sqlite_path):
            evidence = {
                "path": sqlite_path,
                "status": "unavailable",
                "schema_issues": ["SQLite file absent"],
            }
        else:
            evidence = sqlite_evidence(sqlite_path, candidates, ctx["sha256"])
            for row in rows:
                fact = evidence.get("facts", {}).get(row["entry"])
                if fact is not None:
                    row["sqlite"] = fact
    return {
        "schema_version": ENGINE_QUEUE_SCHEMA,
        "provenance": {
            "source_image_sha256": ctx["sha256"],
            "tool_source_sha256": source_hashes(),
            "predicate": {
                "n_insns_gte": ENGINE_MIN_INSNS,
                "float_multiply_or_mac_gte": ENGINE_MIN_FLOAT_MUL_MAC,
                "vector_keys": ["float_mul", "mac"],
            },
            "canonical_inventory_sha256": canonical_digest(inventory),
            "sqlite": evidence,
        },
        "historical_claim": {
            "read": 21,
            "remaining": 30,
            "membership": "unknown/unverified; no exact remaining set is emitted",
        },
        "documented_notes": {
            "bounded_entries": len(documented),
            "rejected": rejected_notes,
        },
        "counts": {
            "candidates": len(rows),
            "documented": sum(r["documented"] for r in rows),
            "undocumented": sum(not r["documented"] for r in rows),
        },
        "target_opcode_coverage": (
            target_coverage
            if target_coverage is not None
            else target_opcode_coverage(ctx, candidates)
        ),
        "candidates": rows,
    }


def write_batch_dossiers(ctx, addresses, out_dir, want_listing):
    os.makedirs(out_dir, exist_ok=True)
    for addr in addresses:
        d = build_dossier(ctx, addr, want_listing=want_listing)
        fn_id = d.get("bounds", {}).get("id") if "bounds" in d else None
        name = (fn_id or ("blk?@0x%x" % addr)).replace("@", "-").replace("0x", "")
        out_path = os.path.join(out_dir, "%s.json" % name)
        with open(out_path, "wb") as fh:
            fh.write(canonical_json_bytes(d))
        txt_path = os.path.join(out_dir, "%s.txt" % name)
        import io

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            print_dossier(d, show_listing=want_listing)
        finally:
            sys.stdout = old
        with open(txt_path, "w") as fh:
            fh.write(buf.getvalue())
        print(
            "0x%x -> %s (%s)" % (addr, out_path, d.get("bounds", {}).get("id", "ERROR"))
        )
