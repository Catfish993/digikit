"""Basic blocks, terminators and control-flow successors of loader code."""

from __future__ import annotations

import collections

from tools.sharc_dev import sharcflow, sharcfn, sharcinv


def _slots(insns, n):
    """The two aligned instructions after insns[n] if they follow it without
    a gap -- the same delay-slot adjacency check as tools/sharc_dev/sharcflow.py's
    find_sites() (its slots()/after() are closures, not exported; see this
    file's proposed-spec note in the build report)."""
    out = []
    for k in (1, 2):
        if n + k >= len(insns):
            return None
        prev_off, prev = insns[n + k - 1]
        if prev_off + prev.length_bytes != insns[n + k][0]:
            return None
        out.append(insns[n + k])
    return out


def _after_sw(base_sw, pair):
    off, insn = pair[1]
    return base_sw + (off + insn.length_bytes) // 2


# --- basic blocks and their control-flow successors -------------------------
#
# Two-phase build across every scanned loader code block: _gather_terminators
# finds, per block, every site that ends a basic block (a branch/call/return/
# indirect, after its delay slots when delayed, or a hardware DO..UNTIL
# loop's last body instruction) and the successor edges it implies; then
# _build_blocks_and_succ collects every terminator's target and "after"
# address into one global leader set (so a target in a DIFFERENT loader code
# block still gets a leader there) before splitting each block's aligned
# instructions into basic blocks against that set. See the module's SCHEMA
# comment on `bblocks`/`succ` for the kind vocabulary.

def _gather_terminators(base_sw, aligned_insns, sites):
    """(seq, sw_index, terminators, extra_leaders) for one loader code
    block's aligned instructions.

    seq: [(sw, off, insn), ...] in address order.
    sw_index: {sw: position in seq}.
    terminators: {sw: {'end_after': Optional[int], 'succs': [(kind, to_sw), ...]}}
    for every site that ends a basic block there.
    extra_leaders: a DO/DO-UNTIL loop's own body-start sw, added even when
    the loop's end address does not resolve to an aligned instruction here
    (e.g. the loop body is scanned but its last instruction fell just short
    of --min-depth)."""
    seq = [(base_sw + off // 2, off, insn) for off, insn in aligned_insns]
    sw_index = {sw: i for i, (sw, _off, _insn) in enumerate(seq)}
    terminators = {}
    extra_leaders = set()

    def add(sw, end_after, succs):
        entry = terminators.setdefault(sw, {"end_after": None, "succs": []})
        entry["succs"].extend(succs)
        if end_after is not None:
            entry["end_after"] = end_after

    return_sws = {r["sw"] for r in sites["returns"]}
    linked_indirect_sws = {c["sw"] for c in sites["indirect_calls"]}

    # Calls and returns already carry their post-delay-slot address
    # (tools/sharc_dev/sharcflow.py's find_sites 'returns_to'/'after'); the call
    # target itself is a `edges` table concern (kind='call'), not `succ` --
    # here a call only contributes the call_return edge back into its own
    # function.
    for c in sites["calls"]:
        add(c["sw"], c.get("returns_to"), [("call_return", c.get("returns_to"))])
    for ic in sites["indirect_calls"]:
        add(ic["sw"], ic.get("returns_to"), [("call_return", ic.get("returns_to"))])
    for r in sites["returns"]:
        add(r["sw"], r.get("after"), [("return", None)])

    for n, (off, insn) in enumerate(aligned_insns):
        t = insn.type_name
        f = sharcinv.merge_fields(insn.fields)
        sw = base_sw + off // 2

        if t in ("8a_abs", "8a_rel", "9a_rel", "9b_rel") and f.get("b") == 0:
            cond = f.get("cond")
            target = f.get("addr") if t == "8a_abs" else \
                sharcflow.pcrel_target(sw, f.get("reladdr", 0))
            delayed = f.get("j")
            conditional = cond is not None and cond != sharcfn.ALWAYS_TRUE_COND
            if delayed:
                pair = _slots(aligned_insns, n)
                end_after = _after_sw(base_sw, pair) if pair else None
            else:
                end_after = base_sw + (off + insn.length_bytes) // 2
            succs = [(("cond_taken" if conditional else "jump"), target)]
            if conditional and end_after is not None:
                succs.append(("cond_not_taken", end_after))
            add(sw, end_after, succs)

        elif t in ("9a_abs", "9b_abs") and sw not in return_sws and sw not in linked_indirect_sws:
            # A generic indirect jump (not the fixed return word, not an
            # indirect call's push+store idiom): both forms carry their own
            # 'j' bit (decode_table.json), so -- unlike the fixed-pattern
            # return word and the indirect-call idiom, which are only ever
            # matched delayed -- a plain indirect JUMP can be non-delayed
            # too (confirmed on dt2-1.16 at sw 0x1c6579, rendered "JUMP
            # non-delayed target=indirect PM(I4, M5)"); trusting a blanket
            # "always 2 slots" here silently swallowed the following
            # instructions into the wrong basic block.
            delayed = f.get("j")
            if delayed:
                pair = _slots(aligned_insns, n)
                end_after = _after_sw(base_sw, pair) if pair else None
            else:
                end_after = base_sw + (off + insn.length_bytes) // 2
            add(sw, end_after, [("indirect", None)])

        elif t in ("12a_imm", "12a_ureg"):
            # DO addr UNTIL LCE: no delay slots of its own (the hardware
            # loop mechanism, not an instruction-level delayed branch) --
            # the terminator is the loop's LAST body instruction, at
            # end_sw, not this DO instruction. Same end_sw formula as
            # tools/sharc_dev/sharcfn.py's render_loop / tools/sharc_dev/sharc_trace.py's
            # _start_counted_loop.
            reladdr = f.get("reladdr")
            if reladdr is not None:
                start_sw = sw + (insn.length_bytes or 6) // 2
                extra_leaders.add(start_sw)
                end_sw = sw + sharcfn.sign_extend(reladdr, 23)
                pos = sw_index.get(end_sw)
                if pos is not None:
                    end_insn = seq[pos][2]
                    end_after = end_sw + (end_insn.length_bytes or 0) // 2
                    add(end_sw, end_after, [("loop_back", start_sw), ("loop_exit", end_after)])

    return seq, sw_index, terminators, extra_leaders


def _build_blocks_and_succ(name, code_blocks, analyzed, functions, owner_of):
    """(bblock_rows, succ_rows, leaders_by_idx) for every scanned loader code
    block. leaders_by_idx (idx -> set of basic-block-start sw's in that
    block) is returned for reuse by the dataref pass below, which resets its
    "last literal loaded into I register" tracking at each basic-block
    boundary.

    Only intra-loader-code-block successors are resolved into a to_block: a
    branch whose target this build never disassembled (a different image,
    an unscanned region, or -- rare -- a different loader code block this
    pass has not looked at yet) gets a succ row with a to_block that matches
    no bblocks row, the same "unresolved" shape the existing edges/
    to_function pair already uses; the interprocedural call graph itself
    stays in `edges`, as call_return already only crosses back into the
    caller."""
    per_idx = {}
    global_leaders = set()

    fn_entries_by_idx = collections.defaultdict(set)
    for fn in functions:
        fn_entries_by_idx[fn["block"]].add(fn["entry"])

    for idx in code_blocks:
        block = analyzed.get(idx)
        if block is None:
            continue
        base_sw = block["base_sw"]
        seq, sw_index, terminators, extra_leaders = _gather_terminators(
            base_sw, block["insns"], block["sites"])
        per_idx[idx] = (seq, sw_index, terminators)
        if seq:
            global_leaders.add(seq[0][0])
        global_leaders |= extra_leaders
        global_leaders |= fn_entries_by_idx.get(idx, set())
        for term in terminators.values():
            if term["end_after"] is not None:
                global_leaders.add(term["end_after"])
            for _kind, to_sw in term["succs"]:
                if to_sw is not None:
                    global_leaders.add(to_sw)

    bblock_rows, succ_rows, leaders_by_idx = [], [], {}

    for idx, (seq, sw_index, terminators) in per_idx.items():
        local_leaders = {sw for sw in global_leaders if sw in sw_index}
        if seq:
            local_leaders.add(seq[0][0])
        for i in range(1, len(seq)):
            prev_sw, _prev_off, prev_insn = seq[i - 1]
            cur_sw, _cur_off, _cur_insn = seq[i]
            if prev_sw + (prev_insn.length_bytes or 0) // 2 != cur_sw:
                # A decode gap: force a split so the CFG never silently
                # bridges it.
                local_leaders.add(cur_sw)
        leaders_by_idx[idx] = local_leaders

        blocks, cur_members = [], []
        for item in seq:
            sw = item[0]
            if cur_members and sw in local_leaders:
                blocks.append(cur_members)
                cur_members = []
            cur_members.append(item)
        if cur_members:
            blocks.append(cur_members)

        for members in blocks:
            start_sw = members[0][0]
            last_sw, _last_off, last_insn = members[-1]
            end_sw_excl = last_sw + (last_insn.length_bytes or 0) // 2
            fn_sw = owner_of(idx, start_sw)
            bblock_rows.append((name, start_sw, end_sw_excl, fn_sw, len(members), idx))

            # The block's terminator may not be its LAST member: a delayed
            # branch/call/indirect's own sw is followed by its own delay
            # slots, which stay in this same block (see the module
            # docstring). Search every member, not just the last one --
            # keying off last_sw alone silently relabelled every delayed
            # terminator's real edge (cond_taken/call_return/loop_back/...)
            # as a plain 'fallthrough' to the same (coincidentally correct)
            # address, and dropped it entirely on the rare block whose
            # delay slots pushed end_sw_excl to an address this pass also
            # split on for an unrelated reason.
            term = None
            for member_sw, _off, _insn in members:
                candidate = terminators.get(member_sw)
                if candidate is not None:
                    term = candidate
                    break
            if term is None:
                if end_sw_excl in local_leaders:
                    succ_rows.append((name, start_sw, end_sw_excl, "fallthrough"))
            else:
                for kind, to_sw in term["succs"]:
                    succ_rows.append((name, start_sw, to_sw, kind))

    return bblock_rows, succ_rows, leaders_by_idx


def _overwritten_bytes(mem, b):
    """Count of block `b`'s own declared byte range NOT owned by itself in
    the final image (mem.owner_runs(), last write wins), or None for a FILL
    block (see the `blocks` table's schema comment, DB_VERSION v13)."""
    if b["fill"] or not b["byte_count"]:
        return None
    runs = mem.owner_runs(b["target_address"], b["byte_count"])
    own = sum(end - start for start, end, owner in runs if owner == b["index"])
    return b["byte_count"] - own
