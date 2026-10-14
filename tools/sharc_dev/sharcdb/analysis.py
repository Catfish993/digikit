"""Roots, reachability, call graph, dominators, loops and unentered clusters of a built database."""

from __future__ import annotations

import bisect
import collections
import time

import networkx as nx

from tools.sharc_dev import sharcldr

from .pointers import _build_ptr_rows

# --- analysis pass (roots/reach/callgraph/idom/loops/unentered) ------------
#
# Runs once per build (and standalone via `sharcdb analyze`) over the tables
# _fill_database already wrote: edges, succ, dataref, literals and
# functions. Nothing here decodes another instruction; it is pure graph work
# on what is already in the database, using networkx instead of another
# hand-rolled CFG walk.

# --- interrupt vector table (see DB_VERSION v7 comment above) ---------------

IVT_SW = 0x120000
IVT_N_SLOTS = 32
IVT_SLOT_BYTES = 24
# An unpopulated slot's four 48-bit words, verified identical across all four
# images this repo builds.
IVT_EMPTY_INSN = bytes((0, 0, 0, 0, 0x3E, 0x0B))
# The trailing two bytes of a direct-jump 8a_abs instruction (see
# _gather_terminators's own t in ("8a_abs", ...) check) -- distinguishes a
# real jump word from the LCNTR-literal/other filler words some slots also
# carry (e.g. slot 5's second word, '020000 05760f').
_IVT_JUMP_SUFFIX = b"\x3e\x06"

# sharc-plus-prm Table 4-46 "Interrupt Priority and Vectors".
IVT_VECTOR_NAMES = {
    0: "EMUI",
    1: "RSTI",
    2: "reserved",
    3: "PARI",
    4: "ILOPI",
    5: "CB7I",
    6: "IICDI",
    7: "SOVFI",
    8: "ILADI",
    9: "reserved",
    10: "reserved",
    11: "TMZHI",
    12: "BKPI",
    13: "FIR",
    14: "IIR",
    15: "SECI",
    16: "reserved",
    17: "reserved",
    18: "reserved",
    19: "reserved",
    20: "RINSEQI",
    21: "CB15I",
    22: "TMZLI",
    23: "FIXI",
    24: "FLTOI",
    25: "FLTUI",
    26: "FLTII",
    27: "EMULI",
    28: "SFT0I",
    29: "SFT1I",
    30: "SFT2I",
    31: "SFT3I",
}

# Two further ISR dispatch tables outside the IVT proper (addresses in this
# repo's bare DM convention -- literals.value/dataref.value/ptr.address all
# already use it; add sharcldr.SW_ALIAS_BASE to reach a LoadedMemory byte
# address). Checked against DT2 1.16/1.15C: both sit entirely inside a
# zero-argument FILL block (see DB_VERSION v7 comment), so this table only
# ever fires the "runtime-filled, no root added" branch there today; it is
# still CPU/image-generic so a future image that ships these tables
# initialized gets code_pointer_array roots for free.
IVT_DISPATCH_TABLES = (
    (0x240AD8, 8, "generic interrupt dispatch (by ASTATX number)"),
    (0x240948, 2, "SECI dispatch (by SEC id)"),
)


def _detect_interrupt_vector_roots(mem):
    """[(target_sw, 'interrupt_vector', 'slot N NAME'), ...] -- one per
    populated IVT slot (see the module-level IVT_* constants). mem is
    optional: without it (no LoadedMemory on hand), this detector is
    skipped, like loader_entry/code_pointer_array above."""
    if mem is None:
        return []
    roots = []
    byte_base = sharcldr.sw_to_byte(IVT_SW)
    for slot in range(IVT_N_SLOTS):
        chunk = mem.read(byte_base + slot * IVT_SLOT_BYTES, IVT_SLOT_BYTES)
        if chunk is None:
            break
        if chunk == IVT_EMPTY_INSN * 4:
            continue
        target = None
        for k in range(4):
            insn = chunk[k * 6 : (k + 1) * 6]
            if insn[4:6] == _IVT_JUMP_SUFFIX:
                target = int.from_bytes(insn[0:3], "little")
                break
        if target is None:
            # A populated slot with no recognisable direct-jump word --
            # left unmodelled rather than guessed (none seen on any of the
            # four images checked).
            continue
        name = IVT_VECTOR_NAMES.get(slot, "reserved")
        roots.append((target, "interrupt_vector", "slot %d %s" % (slot, name)))
    return roots


def _detect_dispatch_table_roots(mem, blocks, owner):
    """[(value, 'code_pointer_array', note), ...] for IVT_DISPATCH_TABLES,
    same consecutive-in-code-pointer-run logic as _detect_roots's
    i_reg_base table scan. A table whose base sits inside a FILL block is
    reported as runtime-filled and skipped -- see the module note above."""
    if mem is None or blocks is None:
        return []
    roots = []
    for base, stride, desc in IVT_DISPATCH_TABLES:
        byte_base = base + sharcldr.SW_ALIAS_BASE
        if sharcldr.fill_block_for_address(blocks, byte_base, space="byte") is not None:
            continue
        values, first_owner = [], None
        for i in range(64):
            raw = mem.read(byte_base + i * stride, min(stride, 4))
            if raw is None:
                break
            value = int.from_bytes(raw, "little")
            fn = owner(value)
            if fn is None or (first_owner is not None and fn != first_owner):
                break
            first_owner = fn
            values.append(value)
        if len(values) >= 2:
            for i, value in enumerate(values):
                roots.append(
                    (
                        value,
                        "code_pointer_array",
                        "%s table=0x%x idx=%d" % (desc, base, i),
                    )
                )
    return roots


def _owner_factory(function_spans):
    """A closure mapping any sw to the entry_sw of the function whose
    [entry_sw, end_sw) span contains it (not necessarily an aligned
    instruction there -- see analyze_image()'s note on RTOS task entries),
    or None."""
    spans = sorted(function_spans)
    starts = [e for e, _ in spans]

    def owner(sw):
        i = bisect.bisect_right(starts, sw) - 1
        if i < 0:
            return None
        entry, end = spans[i]
        return entry if entry <= sw < end else None

    return owner


def _detect_roots(db, name, mem, blocks):
    """[(sw, kind, note), ...] -- see the `roots` table's SCHEMA comment for
    the kind vocabulary. `mem`/`blocks` (a LoadedMemory and its parsed block
    list) are optional: without them, loader_entry and code_pointer_array
    roots are skipped (their note explains why) rather than guessed."""
    function_spans = db.execute(
        "SELECT entry_sw, end_sw FROM functions WHERE image=?", (name,)
    ).fetchall()
    owner = _owner_factory(function_spans)
    roots = []

    if blocks is not None:
        firsts = sharcldr.entry_points(blocks)
        if firsts:
            # The main program's own entry: the LAST BFLAG_FIRST target, the
            # same one tools/sharc_dev/sharcldr.py's main_program() takes as the final
            # application's code (a boot stream may carry earlier FIRST
            # blocks for a bootstrap stage this file never scans).
            roots.append(
                (
                    firsts[-1],
                    "loader_entry",
                    "last of %d BFLAG_FIRST entries" % len(firsts),
                )
            )

    roots += _detect_interrupt_vector_roots(mem)
    roots += _detect_dispatch_table_roots(mem, blocks, owner)

    # RTOS task entries: a CALL whose immediately preceding instructions (in
    # the SAME function, within a small window -- this idiom is always a
    # tight run of literal loads right before the call) load R4/R8/R12 by
    # literal, where R4's value itself resolves into a scanned function's
    # span. That last check is what tells this idiom apart from the many
    # other calls that happen to load exactly these three registers for an
    # unrelated reason (checked against both DT2 1.16 and DN2 1.11: without
    # it, a dozen-plus unrelated call sites match; with it, only the real
    # task-create helper's call sites survive, in both images, without
    # hardcoding either helper's address).
    reg_rows = db.execute(
        "SELECT sw, value, dest_reg FROM literals WHERE image=? AND dest_reg IN ('R4','R8','R12') ORDER BY sw",
        (name,),
    ).fetchall()
    by_reg = collections.defaultdict(list)
    for sw, value, reg in reg_rows:
        by_reg[reg].append((sw, value))
    by_reg_sw = {reg: [s for s, _v in lst] for reg, lst in by_reg.items()}

    def last_within(reg, sw, window=20):
        sws = by_reg_sw.get(reg)
        if not sws:
            return None
        i = bisect.bisect_left(sws, sw)
        if i == 0:
            return None
        s, v = by_reg[reg][i - 1]
        return v if sw - s <= window else None

    call_rows = db.execute(
        "SELECT from_sw, to_function FROM edges WHERE image=? AND kind='call' AND to_function IS NOT NULL",
        (name,),
    ).fetchall()
    for from_sw, to_fn in call_rows:
        r4 = last_within("R4", from_sw)
        r8 = last_within("R8", from_sw)
        r12 = last_within("R12", from_sw)
        if r4 is None or r8 is None or r12 is None:
            continue
        r4u = r4 & 0xFFFFFFFF
        if owner(r4u) is None:
            continue
        roots.append(
            (r4u, "rtos_task", "helper=0x%x call_site=0x%x" % (to_fn, from_sw))
        )

    # Every dataref value that lands inside a scanned function's span: a
    # callback or dispatch pointer materialised as an ordinary literal/table
    # address rather than a CALL/JUMP target.
    for value, sw in db.execute(
        "SELECT value, MIN(sw) FROM dataref WHERE image=? GROUP BY value", (name,)
    ).fetchall():
        if owner(value) is not None:
            roots.append((value, "dataref_code_pointer", "first ref at 0x%x" % sw))

    # Loader-backed code-pointer arrays: consecutive 32-bit little-endian
    # words at an i_reg_base table address (a literal loaded directly into
    # an I register -- the generic "table base" pattern) that decode to
    # addresses inside ONE function. The array ends at the first word that
    # resolves to a different function (or to none) -- e.g. 0x8055c840's
    # entries 0-14 all land in FUN_1c642a; entry 15 lands elsewhere and ends
    # the run. Capped at 64 words so a false-positive base can't spin.
    if mem is not None:
        for (base,) in db.execute(
            "SELECT DISTINCT value FROM dataref WHERE image=? AND role='i_reg_base'",
            (name,),
        ).fetchall():
            values, first_owner = [], None
            for i in range(64):
                raw = mem.read(base + i * 4, 4)
                if raw is None:
                    break
                value = int.from_bytes(raw, "little")
                fn = owner(value)
                if fn is None or (first_owner is not None and fn != first_owner):
                    break
                first_owner = fn
                values.append(value)
            if len(values) >= 2:
                for i, value in enumerate(values):
                    roots.append(
                        (value, "code_pointer_array", "table=0x%x idx=%d" % (base, i))
                    )

    # Functions no edge of any kind targets -- the same generalised "no
    # static caller" check as sharcdb.sql's canned query.
    for (entry,) in db.execute(
        "SELECT f.entry_sw FROM functions f WHERE f.image=? AND NOT EXISTS "
        "(SELECT 1 FROM edges e WHERE e.image=f.image AND e.to_function=f.entry_sw)",
        (name,),
    ).fetchall():
        roots.append((entry, "no_static_entry", None))

    # ColdFire vector table entries: Ghidra itself named these
    # vector_<N>_handler (see build_ghidra_database); a SHARC function is
    # always named FUN_%06x, so this never matches a SHARC image either --
    # no cpu flag needed here.
    for entry, fn_name in db.execute(
        r"SELECT entry_sw, name FROM functions WHERE image=? AND name LIKE 'vector\_%\_handler' ESCAPE '\'",
        (name,),
    ).fetchall():
        roots.append((entry, "vector_entry", fn_name))

    return roots


def _function_call_jump_graph(db, name, function_entries):
    """A networkx DiGraph over function entries: a CALL edge costs 1 (a call
    depth increment), a JUMP/COND_JUMP edge costs 0 (a tail/cross-function
    jump reaches its target at no extra call depth). Where both a call and a
    jump connect the same pair, the jump's 0 wins (min cost)."""
    G = nx.DiGraph()
    G.add_nodes_from(function_entries)
    for a, b in db.execute(
        "SELECT DISTINCT from_function, to_function FROM edges WHERE image=? AND kind='call' "
        "AND from_function IS NOT NULL AND to_function IS NOT NULL AND from_function != to_function",
        (name,),
    ).fetchall():
        G.add_edge(a, b, weight=1)
    for a, b in db.execute(
        "SELECT DISTINCT from_function, to_function FROM edges WHERE image=? AND kind IN ('jump','cond_jump') "
        "AND from_function IS NOT NULL AND to_function IS NOT NULL AND from_function != to_function",
        (name,),
    ).fetchall():
        G.add_edge(a, b, weight=0)
    return G


def _detect_reach(db, name, roots, function_entries):
    """[(root_sw, function_sw, depth), ...]: every function reachable from
    each distinct root address, by shortest CALL depth (0-weighted
    jump/cond_jump edges included in the walk, per
    _function_call_jump_graph).

    Every root normally maps to the function whose [entry_sw, end_sw) span
    contains it (owner(), below). loader_entry is the exception: it's the
    scanned region's raw BFLAG_FIRST target (see _detect_roots), which can
    land outside every function's span. Checked directly against both DT2
    1.16 and DN2 1.11: in both, the loader entry sits at the start of a
    `data` block immediately followed by `fill` padding, and the last
    bblock the scanner finds before that padding has no succ row at all --
    there is no live bblock/succ path from the root into the next
    function, so walking that CFG would find nothing to seed with. The
    fallback below is the next function by address instead: the gap is
    either unscanned or alignment padding, i.e. a straight-line
    continuation rather than a call, so it is seeded at depth 0 -- the
    same effect as giving the loader entry a synthetic function span up to
    that next entry, without adding a fake row to `functions` and
    skewing its count. This fallback is general (any owner-less root
    would take it), but today only loader_entry roots ever do -- every
    other root kind is only added when it already resolves to a function
    (see _detect_roots)."""
    owner = _owner_factory(
        db.execute(
            "SELECT entry_sw, end_sw FROM functions WHERE image=?", (name,)
        ).fetchall()
    )
    starts = sorted(function_entries)
    G = _function_call_jump_graph(db, name, function_entries)
    rows = []
    for sw in sorted({r[0] for r in roots}):
        fn = owner(sw)
        if fn is None:
            i = bisect.bisect_right(starts, sw)
            if i >= len(starts):
                continue
            fn = starts[i]
        for target_fn, dist in nx.single_source_dijkstra_path_length(
            G, fn, weight="weight"
        ).items():
            rows.append((sw, target_fn, int(round(dist))))
    return rows


def _detect_callgraph(db, name, function_entries):
    """[(function_sw, n_callers, n_callees, n_transitive_callees, max_depth,
    is_recursive), ...], CALL edges only. Uses networkx's SCC condensation
    so the whole image's transitive-callee counts and longest call chains
    are one O(V+E) pass, not one traversal per function."""
    G = nx.DiGraph()
    G.add_nodes_from(function_entries)
    for a, b in db.execute(
        "SELECT from_function, to_function FROM edges WHERE image=? AND kind='call' "
        "AND from_function IS NOT NULL AND to_function IS NOT NULL",
        (name,),
    ).fetchall():
        G.add_edge(a, b)

    C = nx.condensation(G)
    order = list(nx.topological_sort(C))
    desc_sccs, longest = {}, {}
    for c in reversed(order):
        s, best = set(), 0
        for succ in C.successors(c):
            s.add(succ)
            s |= desc_sccs[succ]
            best = max(best, 1 + longest[succ])
        desc_sccs[c] = s
        longest[c] = best

    mapping = C.graph["mapping"]
    rows = []
    for fn in function_entries:
        c = mapping[fn]
        members = C.nodes[c]["members"]
        transitive = set()
        for sc in desc_sccs[c]:
            transitive |= C.nodes[sc]["members"]
        if len(members) > 1:
            transitive |= members - {fn}
        is_recursive = len(members) > 1 or G.has_edge(fn, fn)
        rows.append(
            (
                fn,
                G.in_degree(fn),
                G.out_degree(fn),
                len(transitive),
                longest[c],
                int(is_recursive),
            )
        )
    return rows


def _detect_dominators_and_loops(db, name):
    """(idom_rows, loop_rows): per function, networkx.immediate_dominators
    on that function's own bblocks/succ subgraph, then natural loops from
    back edges (succ target dominates succ source) merged per header, kind
    'hw_do' when any merged back edge is succ's own loop_back, else
    'branch_back'."""
    by_func = collections.defaultdict(list)
    for start, fn in db.execute(
        "SELECT start_sw, function_sw FROM bblocks WHERE image=?", (name,)
    ).fetchall():
        if fn is not None:
            by_func[fn].append(start)
    succ_by_from = collections.defaultdict(list)
    for f, t, k in db.execute(
        "SELECT from_block, to_block, kind FROM succ WHERE image=?", (name,)
    ).fetchall():
        succ_by_from[f].append((t, k))

    idom_rows, loop_rows = [], []
    for fn, starts in by_func.items():
        block_set = set(starts)
        H = nx.DiGraph()
        H.add_nodes_from(starts)
        for b in starts:
            for t, k in succ_by_from.get(b, ()):
                if t in block_set:
                    H.add_edge(b, t, kind=k)
        if fn not in H:
            continue
        idom = nx.immediate_dominators(H, fn)
        for node, idom_node in idom.items():
            if node != fn:
                idom_rows.append((fn, node, idom_node))

        def dominates(target, node, _idom=idom, _root=fn):
            n = node
            while True:
                if n == target:
                    return True
                if n == _root:
                    return False
                n = _idom.get(n, _root)

        preds = collections.defaultdict(list)
        for u, v in H.edges():
            preds[v].append(u)

        by_header = collections.defaultdict(lambda: {"body": set(), "hw_do": False})
        for u, v, data in H.edges(data=True):
            if v not in idom or not dominates(v, u):
                continue
            body, stack = {v, u}, ([] if u == v else [u])
            while stack:
                n = stack.pop()
                for p in preds[n]:
                    if p not in body:
                        body.add(p)
                        stack.append(p)
            rec = by_header[v]
            rec["body"] |= body
            if data.get("kind") == "loop_back":
                rec["hw_do"] = True

        headers = list(by_header.items())
        for i, (header, rec) in enumerate(headers):
            depth = 1 + sum(
                1
                for j, (h2, r2) in enumerate(headers)
                if j != i and rec["body"] < r2["body"]
            )
            loop_rows.append(
                (
                    fn,
                    header,
                    len(rec["body"]),
                    "hw_do" if rec["hw_do"] else "branch_back",
                    depth,
                )
            )

    return idom_rows, loop_rows


def _detect_unentered(db, name, roots):
    """[(function_sw, cluster), ...] for every roots.kind='no_static_entry'
    function: the code-pointer array/literal that names it, else the
    nearest preceding function with an indirect site, else 'none'."""
    no_entry = sorted(sw for sw, kind, _note in roots if kind == "no_static_entry")
    array_note = {sw: note for sw, kind, note in roots if kind == "code_pointer_array"}
    literal_note = {
        sw: note for sw, kind, note in roots if kind == "dataref_code_pointer"
    }

    indirect_owners = sorted(
        {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT from_function FROM edges WHERE image=? AND kind='indirect' AND from_function IS NOT NULL",
                (name,),
            ).fetchall()
        }
    )

    rows = []
    for fn in no_entry:
        if fn in array_note:
            cluster = "array:" + array_note[fn]
        elif fn in literal_note:
            cluster = "literal:" + literal_note[fn]
        else:
            i = bisect.bisect_left(indirect_owners, fn) - 1
            cluster = "indirect:0x%x" % indirect_owners[i] if i >= 0 else "none"
        rows.append((fn, cluster))
    return rows


def analyze_image(db, name, mem=None, blocks=None):
    """Fill roots/reach/callgraph/idom/loops/unentered for one already-built
    image (its edges/succ/dataref/literals/functions tables must already
    exist). Idempotent: clears this image's rows from each table first, so
    re-running (e.g. `sharcdb analyze` on a DB built by an older tool
    version) is safe. `mem`/`blocks` are an optional LoadedMemory and its
    parsed block list, for the two root kinds that need to read loaded
    memory directly (see _detect_roots); build_database() always has them
    on hand, `cmd_analyze` reloads them from meta's blob_path when the blob
    is still present locally."""
    t0 = time.time()
    # `ptr` (DB_VERSION 6) may not exist yet on a database `cmd_analyze` is
    # re-running in place from an older tool version -- unlike every other
    # table here, SCHEMA's own CREATE TABLE never ran for it in that case.
    db.execute("""CREATE TABLE IF NOT EXISTS ptr (
        image TEXT, sw INTEGER, base_reg TEXT, base_value INTEGER, address INTEGER,
        direction TEXT, width TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS ptr_address ON ptr(image, address)")
    db.execute("CREATE INDEX IF NOT EXISTS ptr_base_value ON ptr(image, base_value)")
    db.execute("CREATE INDEX IF NOT EXISTS ptr_sw ON ptr(image, sw)")

    for table in ("roots", "reach", "callgraph", "idom", "loops", "unentered", "ptr"):
        db.execute("DELETE FROM %s WHERE image=?" % table, (name,))

    function_entries = [
        r[0]
        for r in db.execute(
            "SELECT entry_sw FROM functions WHERE image=?", (name,)
        ).fetchall()
    ]

    roots = _detect_roots(db, name, mem, blocks)
    reach_rows = _detect_reach(db, name, roots, function_entries)
    callgraph_rows = _detect_callgraph(db, name, function_entries)
    idom_rows, loop_rows = _detect_dominators_and_loops(db, name)
    unentered_rows = _detect_unentered(db, name, roots)
    ptr_rows = _build_ptr_rows(db, name, idom_rows)

    db.executemany("INSERT INTO roots VALUES (?,?,?,?)", [(name,) + r for r in roots])
    db.executemany(
        "INSERT INTO reach VALUES (?,?,?,?)", [(name,) + r for r in reach_rows]
    )
    db.executemany(
        "INSERT INTO callgraph VALUES (?,?,?,?,?,?,?)",
        [(name,) + r for r in callgraph_rows],
    )
    db.executemany(
        "INSERT INTO idom VALUES (?,?,?,?)", [(name,) + r for r in idom_rows]
    )
    db.executemany(
        "INSERT INTO loops VALUES (?,?,?,?,?,?)", [(name,) + r for r in loop_rows]
    )
    db.executemany(
        "INSERT INTO unentered VALUES (?,?,?)", [(name,) + r for r in unentered_rows]
    )
    db.executemany(
        "INSERT INTO ptr VALUES (?,?,?,?,?,?,?)", [(name,) + r for r in ptr_rows]
    )

    return {
        "seconds": time.time() - t0,
        "n_roots": len(roots),
        "n_reach": len(reach_rows),
        "n_callgraph": len(callgraph_rows),
        "n_idom": len(idom_rows),
        "n_loops": len(loop_rows),
        "n_unentered": len(unentered_rows),
        "n_ptr": len(ptr_rows),
    }
