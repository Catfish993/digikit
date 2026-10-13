---
name: firmware-query
description: Use before writing SQL or one-off scripts against the firmware databases (ColdFire cfdb, SHARC sharcdb) or when asking where a feature (machines, FX, parameters, menus, pattern and step storage, LFO, p-lock) is handled on a firmware version. Gives one compact query CLI, tools/fw.py, and the per-firmware atlas.
---

# Firmware queries with tools/fw.py

Run from the repo root. `-i IMAGE` (dt2-1.17, dn2-1.12, dt2-1.16, dn2-1.11) or `export FW_IMAGE=dt2-1.17`.
The CPU follows the address: 0x40000000 and up is ColdFire, below 0x1000000 is SHARC (`--cpu` overrides).
Output is capped at 60 lines (`--max N`, 0 = all). A call costs about 0.05 to 0.1 s.

Use `batch` for more than one question: one process, one `## query` block each.

    uv run python tools/fw.py batch - <<'Q'
    atlas machine
    class MachineListView --max 8
    fn 0x401be1be --max 20
    str WRONG UI
    Q

Order of work:
1. `atlas TERM` for "where is X handled" (terms: machine, fx, parameter, step, pattern, kit, sound, track,
   page, menu, lfo, p-lock, trig, retrig, song, chain, plus-drive, sample, filter, midi, project ...).
   Rows list the function that builds the table, UI classes, strings, descriptors, parameters and manual pages,
   each with a provenance and confidence. Build with `uv run python tools/atlas.py build dt2-1.17`.
2. `search TERM` (names, strings, decompiled C, atlas; `--in names,strings,decomp,atlas`).
3. `fn`, `class`, `callers`, `callees`, `xref`, `path`, `decomp`, `disasm` on what the search found.
4. `port NAME --from dt2-1.16 --to dt2-1.17` carries a function between versions (cfmatch matches).
5. `sql "SELECT ..."` (read-only) only when none of these fits. Tables: functions, edges, datarefs, strings,
   symbols, typeinfos, vtables, matches (cfdb); functions, edges, insn, literals, ptr (sharcdb).
Only then write a script, and put it under tools/ if it will be reused.

An empty `callers` or `xref` is not evidence of dead code (Ghidra misses code outside functions and indirect
calls through vtables); confirm with `tools/refscan.py`. Atlas rows marked `medium` are heuristic.

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
