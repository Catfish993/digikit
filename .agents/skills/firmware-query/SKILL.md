---
name: firmware-query
description: Use before writing SQL or one-off scripts against the firmware databases (ColdFire cfdb, SHARC sharcdb) or when asking where a feature (machines, FX, parameters, menus, pattern and step storage, LFO, p-lock) is handled on a firmware version. Gives one compact query CLI, tools/coldfire/fw.py, and the per-firmware atlas.
---

# Firmware queries with tools/coldfire/fw.py

Run from the repo root. `-i IMAGE` (dt2-1.17, dn2-1.12, dt2-1.16, dn2-1.11) or `export FW_IMAGE=dt2-1.17`.
The CPU follows the address: 0x40000000 and up is ColdFire, below 0x1000000 is SHARC (`--cpu` overrides).
Output is capped at 60 lines (`--max N`, 0 = all). A call costs about 0.05 to 0.1 s.

Use `batch` for more than one question: one process, one `## query` block each.

    uv run python -m tools.coldfire.fw batch - <<'Q'
    atlas machine
    class MachineListView --max 8
    fn 0x401be1be --max 20
    str WRONG UI
    Q

Order of work:
1. `atlas TERM` for "where is X handled" (terms: machine, fx, parameter, step, pattern, kit, sound, track,
   page, menu, lfo, p-lock, trig, retrig, song, chain, plus-drive, sample, filter, midi, project ...).
   Rows list the function that builds the table, UI classes, strings, descriptors, parameters and manual pages,
   each with a provenance and confidence. Build with `uv run python -m tools.coldfire.atlas build dt2-1.17`.
2. `search TERM` (names, strings, decompiled C, atlas; `--in names,strings,decomp,atlas`).
3. `fn`, `class`, `callers`, `callees`, `xref`, `path`, `decomp`, `disasm` on what the search found.
4. `port NAME --from dt2-1.16 --to dt2-1.17` carries a function between versions (cfmatch matches).
5. `sql "SELECT ..."` (read-only) only when none of these fits. Tables: functions, edges, datarefs, strings,
   symbols, typeinfos, vtables, matches (cfdb); functions, edges, insn, literals, ptr (sharcdb).
Only then write a script, and put it under tools/ if it will be reused.

An empty `callers` or `xref` is not evidence of dead code (Ghidra misses code outside functions and indirect
calls through vtables); confirm with `tools/coldfire/refscan.py`. Atlas rows marked `medium` are heuristic.

## Catalog: candidate functions for anchors

Build (both devices, one run; needs `atlas build` for both images and the cfdb of the old versions for `stable`):

    uv run python -m tools.coldfire.atlas catalog dt2-1.17 dn2-1.12

It adds the table `catalog` to `out/atlas/<image>.sqlite` and writes `out/atlas/<image>-catalog.tsv` (best score first). Query:

    fw.py catalog -i dt2-1.17 --grade --top 20          # best anchor candidates
    fw.py catalog menu -i dt2-1.17 --tag near:cf.menu   # TERM matches name, class, hint or tags
    fw.py catalog -i dn2-1.12 --tag role:model
    fw.py catalog -i dt2-1.17 --groups --top 20         # groups by size
    fw.py catalog -i dt2-1.17 --group 3 --top 50        # rows of one group

Review by group first, then rows. The build also writes `out/atlas/<image>-catalog-groups.tsv` (table `catalog_groups`) and `out/atlas/<image>-catalog-graph.json` (nodes and call edges between catalog rows). A group is a Louvain community of the call graph between catalog rows; group -1 is a row alone.

Review page: `uv run python -m tools.firmware.catalog_page dt2-1.17 [--labels FILE ...]` writes `out/atlas/dt2-1.17-review.html`. Labels are JSONL files, by default every `*.jsonl` in `out/atlas/labels/<image>/`. Keep/later/skip decisions are stored by the published page; read them back with the artifact database tools.

| column | meaning |
| --- | --- |
| tags | why it is a candidate: ui_own_vfunc, ui_vfunc, ui_helper, ui_string, near:PREFIX (next to an existing anchor), role:ROLE, term:TERM |
| ui_callers, ui_strings, hint | UI vfuncs that call it; UI strings it uses; first such string |
| h_strict, hash_count, stable | hash, functions with the same hash, same hash as the matched function in the old version (NULL: unknown) |
| pair_addr, pair_method, pair_same_hash | the matched function on the other device |
| anchors | existing anchors in the function (these score lower) |
| rule_kind, rule_ordinal, rule_both | suggested rule (cf_func, cf_vtable_slot, none) and whether it is the same on both devices |
| anchor_grade, score | 1 when a hash rule fits both devices and versions; ranking |
| caller_classes | up to 3 classes whose vfuncs call it, as `Class:count` |
| callees_known | up to 3 callees that are anchored or named, most called first |
| work_name | working name of a `FUN_` row: `Class::helper_XXXX`, `str:HINT` or `calls:NAME`; empty otherwise |
| group | call-graph group id, -1 when alone |

### DSP (SHARC) catalog

    uv run python -m tools.sharc_dev.sharc_catalog dt2-1.17 dn2-1.12 [--profile IMAGE=PATH]
    fw.py catalog -i dt2-1.17 --cpu sharc --grade --top 20

Needs `out/sharcdb/<image>.sqlite` (also of the old image, for `stable`). Writes `out/atlas/<image>-dsp.sqlite` (tables `catalog`, `catalog_groups`), `<image>-dsp-catalog.tsv`, `<image>-dsp-catalog-groups.tsv` and `<image>-dsp-catalog-graph.json`. Addresses are short words. Columns differ from ColdFire: `reloc_hash` replaces `h_strict`; no ui or class fields. DSP-only: `role` (voice, master/fx, util, frame, io, other), `label`, `callers`, `callees`, `loops`, `do`, `recip`, `lit_data`, `abs_dm`, `hints`, and `prof_insns` (NULL without `--profile`, which also adds the tag `hot`). `fw.py catalog` shows `role` and `hints`; TERM matches name, role, hints or tags.

### From a candidate to a declaration

    uv run python -m tools.firmware.catalog emit-rule dt2-1.17 0x40113dc8 --other dn2-1.12    # rule, where it resolves, same on both?
    uv run python -m tools.firmware.catalog declare dt2-1.17 0x40113dc8 cf.text.fn.draw_fmt --other dn2-1.12 [--write]

ADDR below 0x1000000 is a SHARC pc. `emit-rule` prints the `devices/fw` rule of the function that contains ADDR (hash, `ordinal` if the hash is not unique, offset, `--len`) and checks it on the image and, with `--other`, on the other device against the catalogue `pair_addr`. `declare` builds a `mark = "O"` entry from it (shared rule, or one rule per device) and with `--write` inserts it into `devices/fw/<subsystem>.toml`. Needs the atlas catalogue for the pair.

Marks stay [D]/[O] until a second check against image bytes. "No callers" needs `tools/coldfire/refscan.py`.

## Examples (output trimmed)

Where is the machine list built?

    $ fw.py atlas machine -i dn2-1.12 --max 6
    glossary 'machine': 29 rows
      function  0x400c2c4e  builds the 43-entry page/machine descriptor table at 0x42433af8 ... (high)
      class     MachineListView  role=view vtable=0x401df910 slots=18 (high)
      descriptor #1@0x42433b24  FM TONE/FM TONE role=machine (high)

What does a function do and who uses it?

    $ fw.py fn SourcePageView::vfunc_19
    40016de2 SourcePageView::vfunc_19  size=202  end=40016eac
    vtable: SourcePageView#19
    callees: 12 ...
    decomp (12 of 27 lines): ...

A UI string and the code that shows it:

    $ fw.py str "PATTERN NAME"
    4023eb34 'PATTERN NAME'
        used by 4007f086 std::_Sp_counted_ptr_inplace<NameView,...>::ctor_dtor

An RTTI class with its vtable (methods inherited from a base are marked `<View>`):

    $ fw.py class PatternGridView --max 4
    PatternGridView  vmi_class  typeinfo=401f9008  bases=View, Observer, LedHandler, GlobalInstances
    vtable 401f9074 (offset_to_top 0)
      #0   401b1744 PatternGridView::vfunc_0

Everything that references an address:

    $ fw.py xref 0x40241826
    xref 40241826  40241826
      string 'WRONG UI CARD'
      data DATA  400cadb2 in 400cad5a FUN_400cad5a

The same function on another version:

    $ fw.py port 0x401bdee2 --from dt2-1.16 --to dt2-1.17
    401bdee2 FUN_401bdee2  (dt2-1.16) -> dt2-1.17
      401be1be FUN_401be1be  [strict]

A SHARC function (no decompiler; listing and call counts):

    $ fw.py fn 0xb80105 --max 8
    b80105 FUN_b80105 (DMA/descriptor construction)  insns=38 end=b8015b block=69 leaf
    callers: 2 ...

Substring search over decompiled C:

    $ fw.py search "pattern name" --in decomp
    decomp: 2 functions (best 2)
      40070c00 PatternGridView::vfunc_2 : char PatternGridView::vfunc_2(int param_1,undefined4 param_2)
