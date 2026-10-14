"""Loading the image and building and printing the dossier of one function."""

from __future__ import annotations

from collections import Counter

from tools.sharc_dev import sharcflow, sharcinv, sharcldr

from .regions import EXPECTED_SHA256
from .render import render_instruction

# --- dossier construction --------------------------------------------------


def sha256_of(path: str) -> str:
    import hashlib

    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_context(blob_path: str, block_idxs, min_depth: int):
    """Build once, reuse for every dossier: the parsed blob, a LoadedMemory
    for RAM/ROM checks, and the full-inventory function list (so cross-block
    call graph resolves) via tools/sharc_dev/sharcinv.py's build_inventory()."""
    with open(blob_path, "rb") as fh:
        data = fh.read()
    blocks = sharcldr.parse_blocks(data)
    mem = sharcldr.LoadedMemory.from_stream(data, blocks)
    functions, by_id = sharcinv.build_inventory(blob_path, block_idxs, min_depth)
    blocks_by_idx = {b["index"]: b for b in blocks}
    analyzed = {}
    for idx in block_idxs:
        r = sharcinv.analyze_block(mem, blocks_by_idx, idx, min_depth)
        if r is None:
            continue
        r["_insn_sw"] = [r["base_sw"] + off // 2 for off, _ in r["insns"]]
        analyzed[idx] = r
    return {
        "data": data,
        "blocks": blocks,
        "blocks_by_idx": blocks_by_idx,
        "mem": mem,
        "functions": functions,
        "by_id": by_id,
        "analyzed": analyzed,
        "blob_path": blob_path,
        "sha256": sha256_of(blob_path),
    }


def base_sw_note(target_address: int) -> str:
    if target_address >= sharcldr.SW_ALIAS_BASE:
        return (
            "target %#x >= 0x28000000: base_sw = (target_address - 0x28000000) / 2 "
            "(ordinary short-word alias)" % target_address
        )
    if sharcldr.L2_BYTE_BASE <= target_address < sharcldr.L2_BYTE_LIMIT:
        return (
            "target %#x is in the L2 byte window %#x-%#x: "
            "base_sw = 0xb80000 + (target_address - 0x20000000) / 2 "
            "(blk69 does NOT use the 0x28000000 alias)"
            % (target_address, sharcldr.L2_BYTE_BASE, sharcldr.L2_BYTE_LIMIT)
        )
    return "target %#x: no known base_sw convention covers this block" % target_address


def find_function(ctx, addr: int):
    """-> fn dict for `addr`, preferring an exact entry match, else the
    function whose [entry, exit) span contains it. None if not found in any
    scanned block."""
    exact = [fn for fn in ctx["functions"] if fn["entry"] == addr]
    if exact:
        return exact[0], True
    containing = [fn for fn in ctx["functions"] if fn["entry"] <= addr < fn["exit"]]
    if containing:
        containing.sort(key=lambda fn: addr - fn["entry"])
        return containing[0], False
    return None, False


def locate_block(ctx, addr: int):
    for idx, block in ctx["analyzed"].items():
        span = block["payload_len"] // 2
        if block["base_sw"] <= addr < block["base_sw"] + span:
            return idx, block
    return None, None


def scan_conditional_returns(block):
    """9b_abs jumps through I4/M6 (b=0, pmm=5, j=1) whose cond isn't 31 --
    sharcflow.py's returns list only matches the unconditional return word
    (raw == RETURN_JUMP), so a real conditional return would be invisible
    there. Cheap to detect from the same aligned instruction stream."""
    out = []
    for off, insn in block["insns"]:
        if insn.type_name != "9b_abs":
            continue
        f = sharcinv.merge_fields(insn.fields)
        if (
            (f.get("b"), f.get("pmm"), f.get("j")) == (0, 5, 1)
            and f.get("cond") not in (31, None)
            and insn.raw != sharcflow.RETURN_JUMP
        ):
            out.append({"sw": block["base_sw"] + off // 2, "cond": f.get("cond")})
    return out


def internal_branch_targets(fn_insns, entry, exit_):
    """JUMP (not CALL) targets inside this function's own span, other than
    its entry -- the hazard flagged in the task brief: a branch target
    inside a return-delimited span is not automatically a function."""
    out = []
    for sw, insn in fn_insns:
        t = insn.type_name
        f = sharcinv.merge_fields(insn.fields)
        target = None
        if t in ("8a_abs", "9a_abs") and f.get("b") == 0:
            target = f.get("addr")
        elif t in ("8a_rel", "9a_rel") and f.get("b") == 0:
            target = sharcflow.pcrel_target(sw, f.get("reladdr", 0))
        if target is not None and entry <= target < exit_ and target != entry:
            out.append((sw, target))
    return out


def backward_exit_note(fn_insns, entry):
    """Flag any unconditional (cond=31) JUMP inside this function's own
    span whose target is below `entry` -- the blk93@0x1c4f81 pattern: the
    function's only live exit jumps backward into a preceding span's shared
    restore+return epilogue instead of returning through its own frame.
    Scans the whole span, not just the last instruction: when the real exit
    sits before the end of the computed span (the tail beyond it absorbed
    by an interior-call-target split, as at 0x1c4f81/0x1c5334), the last
    instruction is not the jump."""
    notes = []
    for sw, insn in fn_insns:
        t = insn.type_name
        if t not in ("8a_abs", "8a_rel", "9a_abs", "9a_rel"):
            continue
        f = sharcinv.merge_fields(insn.fields)
        if f.get("b") != 0 or f.get("cond") not in (31, None):
            continue
        if t in ("8a_abs", "9a_abs"):
            target = f.get("addr")
        else:
            target = sharcflow.pcrel_target(sw, f.get("reladdr", 0))
        if target is not None and target < entry:
            notes.append(
                "unconditional JUMP at 0x%x targets 0x%x, before this function's "
                "own entry -- likely a shared epilogue inside a preceding span, "
                "not a return through this function's own frame" % (sw, target)
            )
    return notes


def build_dossier(ctx, addr: int, want_listing: bool = True):
    idx, block = locate_block(ctx, addr)
    if idx is None:
        return {"addr": addr, "error": "not covered by any scanned code block"}

    fn, exact = find_function(ctx, addr)
    b = ctx["blocks_by_idx"][idx]
    file_offset = b["payload_offset"] + (addr - block["base_sw"]) * 2

    ident = {
        "block": idx,
        "target_address": "0x%x" % b["target_address"],
        "payload_offset": b["payload_offset"],
        "base_sw_convention": base_sw_note(b["target_address"]),
        "base_sw": "0x%x" % block["base_sw"],
        "file_offset_of_entry": "0x%x" % file_offset,
        "image_sha256": ctx["sha256"],
        "image_sha256_known_as": EXPECTED_SHA256.get(
            ctx["sha256"], "(not a recognised image)"
        ),
    }

    result = {"addr": "0x%x" % addr, "identification": ident}

    if fn is None:
        result["error"] = "no return-delimited span in blk%d covers 0x%x" % (idx, addr)
        return result

    if not exact:
        result["address_note"] = (
            "0x%x is not this function's own entry; using the "
            "containing function %s (entry 0x%x)" % (addr, fn["id"], fn["entry"])
        )

    entry, exit_ = fn["entry"], fn["exit"]
    fn_insns = sharcinv.instructions_in(block, entry, exit_)

    bounds = {
        "id": fn["id"],
        "entry": "0x%x" % entry,
        "exit": "0x%x" % exit_,
        "n_insns": fn["n_insns"],
        "entry_kind": fn["entry_kind"],
        "boundary_note": fn.get("boundary_note"),
        "split_from": fn.get("split_from"),
        "tail_split_into": fn.get("tail_split_into"),
        "method": (
            "return-delimited span (tools/sharc_dev/sharcflow.py returns), further split at "
            "any direct-call target landing strictly inside it (tools/sharc_dev/sharcinv.py "
            "function_bounds())"
        ),
    }
    internal_targets = internal_branch_targets(fn_insns, entry, exit_)
    if internal_targets:
        bounds["internal_branch_targets"] = [
            "0x%x -> 0x%x (branch target inside this span, NOT a separate function)"
            % (sw, t)
            for sw, t in internal_targets
        ]
    backward = backward_exit_note(fn_insns, entry)
    if backward:
        bounds["backward_exit_hazard"] = backward

    cond_returns = [
        r for r in scan_conditional_returns(block) if entry <= r["sw"] < exit_
    ]

    callgraph = {
        "callers": fn["callers"],
        "callees": fn["callees"],
        "unresolved_callees": fn.get("unresolved_callees", []),
        "is_leaf": fn["is_leaf"],
        "has_no_static_caller": fn["has_no_static_caller"],
        "indirect_calls": fn["vector"].get("indirect_calls", 0),
        "conditional_returns": cond_returns,
    }
    if cond_returns:
        callgraph["conditional_return_note"] = (
            "tools/sharc_dev/sharcflow.py only matches the unconditional return word "
            "(raw==0x083F343F); these are 9b_abs jumps through I4/M6 with cond != 31 "
            "found by scanning this block's own aligned instructions"
        )

    summary = {
        "label": fn["label"],
        "confidence": fn["confidence"],
        "label_reasons": fn["label_reasons"],
        "vector": fn["vector"],
        "named_tables_touched": fn["vector"].get("named_tables_touched", []),
        "param_frame_referenced": any(
            r.startswith("param_frame") for r in fn["vector"].get("literal_regions", {})
        ),
        "literal_regions": fn["vector"].get("literal_regions", {}),
    }

    result.update({"bounds": bounds, "call_graph": callgraph, "summary": summary})

    if want_listing:
        named_tables_touched = set()
        literal_regions = Counter()
        float_immediates = []
        listing = []
        for sw, insn in fn_insns:
            mnemonic, notes, gap = render_instruction(
                sw,
                insn,
                ctx["mem"],
                named_tables_touched,
                literal_regions,
                float_immediates,
            )
            raw_bytes = (
                insn.raw.to_bytes(insn.length_bytes, "big")
                if insn.raw is not None and insn.length_bytes
                else b""
            )
            listing.append(
                {
                    "sw": "0x%x" % sw,
                    "bytes": raw_bytes.hex(),
                    "form": insn.type_name,
                    "mnemonic": mnemonic,
                    "notes": notes,
                    "gap": gap,
                }
            )
        result["listing"] = listing
        result["listing_extra"] = {
            "named_tables_touched_in_listing": sorted(named_tables_touched),
            "literal_regions_in_listing": dict(literal_regions),
            "float_immediates_in_listing": float_immediates,
            "undecoded_count": sum(1 for r in listing if r["form"] == "unknown"),
            "gap_count": sum(1 for r in listing if r["gap"]),
        }

    return result


# --- printing ---------------------------------------------------------


def print_dossier(d: dict, show_listing: bool):
    addr = d.get("addr")
    print("=== dossier for %s ===" % addr)
    if "error" in d and "identification" not in d:
        print("  ERROR: %s" % d["error"])
        return
    ident = d["identification"]
    print("\n-- identification --")
    for k in (
        "block",
        "target_address",
        "payload_offset",
        "base_sw",
        "file_offset_of_entry",
        "image_sha256",
        "image_sha256_known_as",
    ):
        print("  %-24s %s" % (k, ident[k]))
    print("  base_sw convention:    %s" % ident["base_sw_convention"])

    if "error" in d:
        print("\n  ERROR: %s" % d["error"])
        return
    if d.get("address_note"):
        print("\n  NOTE: %s" % d["address_note"])

    b = d["bounds"]
    print("\n-- bounds --")
    print(
        "  id=%s entry=%s exit=%s n_insns=%s entry_kind=%s"
        % (b["id"], b["entry"], b["exit"], b["n_insns"], b["entry_kind"])
    )
    print("  method: %s" % b["method"])
    if b.get("boundary_note"):
        print("  boundary_note: %s" % b["boundary_note"])
    for t in b.get("internal_branch_targets", []):
        print("  HAZARD: %s" % t)
    for note in b.get("backward_exit_hazard") or []:
        print("  HAZARD: %s" % note)

    cg = d["call_graph"]
    print("\n-- call graph --")
    print("  callers: %s" % (cg["callers"] or "(none found)"))
    print("  callees: %s" % (cg["callees"] or "(none)"))
    if cg["unresolved_callees"]:
        print(
            "  unresolved callees (target outside scanned blocks): %s"
            % ["0x%x" % t for t in cg["unresolved_callees"]]
        )
    print(
        "  is_leaf=%s has_no_static_caller=%s indirect_calls=%d"
        % (cg["is_leaf"], cg["has_no_static_caller"], cg["indirect_calls"])
    )
    if cg["conditional_returns"]:
        print(
            "  conditional returns: %s"
            % [
                ("0x%x cond=%s" % (r["sw"], r["cond"]))
                for r in cg["conditional_returns"]
            ]
        )
        print("  %s" % cg["conditional_return_note"])

    s = d["summary"]
    print("\n-- summary --")
    print("  label: %s (confidence %.2f)" % (s["label"], s["confidence"]))
    for r in s["label_reasons"]:
        print("    - %s" % r)
    print("  named_tables_touched: %s" % s["named_tables_touched"])
    print("  param_frame_referenced: %s" % s["param_frame_referenced"])
    print("  literal_regions: %s" % s["literal_regions"])
    v = s["vector"]
    print(
        "  compute_total=%d mem_load=%d mem_store=%d loop_literal=%d loop_register=%d"
        % (
            v["compute_total"],
            v["mem_load"],
            v["mem_store"],
            v["loop_literal"],
            v["loop_register"],
        )
    )

    if show_listing and "listing" in d:
        print("\n-- listing (%d instructions) --" % len(d["listing"]))
        for row in d["listing"]:
            gap = "  <<< GAP" if row["gap"] else ""
            line = "  %-10s %-16s %-10s %s%s" % (
                row["sw"],
                row["bytes"],
                row["form"],
                row["mnemonic"],
                gap,
            )
            print(line)
            for note in row["notes"]:
                print("             ; %s" % note)
        extra = d["listing_extra"]
        print(
            "\n  named tables touched in listing: %s"
            % extra["named_tables_touched_in_listing"]
        )
        print("  literal regions in listing: %s" % extra["literal_regions_in_listing"])
        print(
            "  undecoded instructions: %d   flagged gap opcodes: %d"
            % (extra["undecoded_count"], extra["gap_count"])
        )
