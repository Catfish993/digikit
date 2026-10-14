"""One queryable SQLite database per SHARC+ image: instructions, functions,
control-flow edges, literals, memory-access facts and a relocation-tolerant
function hash for cross-image matching.

    uv run python -m tools.sharc_dev.sharcdb build BLOB [--out out/sharcdb/<name>.sqlite]
        [--name NAME] [--min-depth N] [--blocks 1,56,69,...] [--force]
    uv run python -m tools.sharc_dev.sharcdb build BLOB1 BLOB2 ... [--out-dir out/sharcdb]
        [--jobs N] [--force]

This exists so a question like "who jumps into 0x1c71ec" or "what writes
0x262138" is a `sqlite3` query in milliseconds, built once, instead of a
fresh ~5s tools/sharc_dev/sharcfn.py load-and-decode plus a throwaway script every
time. It reuses every decoder already in this repo -- tools/sharc_dev/sharcldr.py
(loader blocks, LoadedMemory, sw<->byte mapping), tools/sharc_dev/sharcinv.py (code
block classification, function boundaries, the merged-field helper),
tools/sharc_dev/sharcflow.py (call/return site finding, pcrel_target), tools/sharc_dev/sharcfn.py
(render_instruction for the mnemonic text, register/condition naming) and
tools/sharc_dev/sharc_disasm.py/sharcimm.py (the linear disassembler and its sweep) --
and adds no new instruction decoding of its own.

A CALL-only caller graph misses JUMP edges: a `JUMP IF SV` at sw 0x1c7053
into FUN_1c71ec went unrecorded for weeks because nothing but a bare
tools/sharc_dev/sharcflow.py call/return scan was ever run over that address. The
`edges` table below records calls, plain and conditional jumps (with the
not-taken path as a `fallthrough` edge), returns and indirect sites
uniformly, so "callers of X" is a `to_sw = X` query across every kind at
once, not a CALL-only search.

Code-block selection. tools/sharc_dev/sharcinv.py's CODE_BLOCKS (56, 69, 76, 78, 80,
88, 91, 93) is a hand-verified list for one specific image (DT2 1.16) --
docs/findings/06-sharc-engine-and-startup.md, "The SHARC code is not one
block". It is NOT a general "decodes densely" rule: several DATA blocks in
that same image alias-decode at 70-97% density (blk19, blk35, blk37, blk48),
as dense as some of the small code fragments, so a decode-density threshold
alone silently mislabels data as code. This file therefore keeps a small,
explicit KNOWN_CODE_BLOCKS table keyed by image sha256 rather than guessing:
DT2 1.15C's block layout was checked by inspection to be identical to 1.16's
(same block indices, same target addresses -- reuses sharcinv.CODE_BLOCKS
directly), and DN2 1.11/1.10E's code blocks were identified by target-address
correspondence to DT2's known regions (the three small 0x2838xxxx blocks,
startup at 0x28380548 and the main code region), the same method
docs/findings/11-sharc-cross-image-comparison.md describes ("Selection uses
the same loader-target code regions as the existing DT2 inventory. The
block indices differ where the loader stream's preceding records differ.").
Every image's list originally also carried the loader-target-0x282403f0
block (index 1 on all four images): docs/findings/05-sharc-isa-and-decoding.md's
"One decode path" section (DB_VERSION v13) found it is not code in any of
the four final images (a later block in the same boot stream overwrites
99.92% of its declared byte range before boot ends), so it is no longer in
this table or in sharcinv.CODE_BLOCKS. DN2 1.11/1.10E's block 57 (target
0x2001e888) is a genuine, unoverwritten L2-window code block: it used to be
mis-reported as stale by --compare-decode-at because sharcldr.L2_BYTE_LIMIT
modeled only the first of L2 SRAM's eight 128 KB banks (v13 widened it to
the full 1 MB). DN2 1.11 also has an extra L2-window region (blk59, target
0x200779f8): it fell outside the OLD L2_BYTE_LIMIT and was left out of
KNOWN_CODE_BLOCKS for that reason; the wider v13 bound now covers its start
address too, but it has not been verified as code (unlike block 57, no
prior characterization exists for it), so it remains a known, unverified
gap here, not silently treated as data. An image whose sha256 is not in
KNOWN_CODE_BLOCKS refuses to build without an explicit --blocks override,
rather than guessing.
"""

from __future__ import annotations

from .build import build_database
from .facts import (
    classify_literal_range,
    extract_literal,
    extract_mem_access,
    mask_relocatable,
)
from .ghidra import build_ghidra_database
from .pointers import _ptr_mem_form
from .regdefs import (
    _shiftimm_regdef_reguse,
    _shortcompute_regdef_reguse,
    register_effects,
)
from .schema import DB_VERSION, read_meta

__all__ = [
    "DB_VERSION",
    "_ptr_mem_form",
    "_shiftimm_regdef_reguse",
    "_shortcompute_regdef_reguse",
    "build_database",
    "build_ghidra_database",
    "classify_literal_range",
    "extract_literal",
    "extract_mem_access",
    "mask_relocatable",
    "read_meta",
    "register_effects",
]
