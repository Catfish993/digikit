"""Function dossier for SHARC+ VISA code: everything a reader needs before
looking at one instruction, built from tools/sharc_dev/sharcldr.py, tools/sharc_dev/sharcflow.py,
tools/sharc_dev/sharcinv.py and tools/sharc_dev/sharc_disasm.py rather than reimplemented.

    uv run python -m tools.sharc_dev.sharcfn BLOB.bin ADDR
        [--json OUT] [--listing] [--min-depth N] [--blocks LIST]
    uv run python -m tools.sharc_dev.sharcfn BLOB.bin --batch ADDR,ADDR,...
        --out-dir DIR [--min-depth N] [--blocks LIST]

BLOB.bin is the raw SHARC boot stream (out/sections/dt2-1.16/section_7_BLOB.bin).
ADDR is a short-word (VISA PC) address, hex (0x...) or decimal. If it does not
name a function's own entry, the dossier says so and uses the function whose
return-delimited span contains it (tools/sharc_dev/sharcinv.py's function_bounds()).
Plain `sharcfn.py BLOB ADDR` prints identification, bounds, call graph and
the summary; `--listing` (on by default for a single address, off for
--batch unless given) also prints the annotated instruction-by-instruction
listing, the tool's main deliverable.

A dossier has five parts:

1. Identification -- block, the block's base_sw convention stated in words,
   file offset of the entry, and the image sha256 (checked against the
   known dt2-1.16 hash when it matches; otherwise just reported).
2. Bounds -- entry, exit, instruction count and how they were found
   (tools/sharc_dev/sharcinv.py's two-pass return-delimited-span-then-interior-call-
   target scheme), with both known boundary hazards flagged: an internal
   branch target inside the span that is not itself a function entry, and a
   live exit that jumps backward into a preceding span's shared epilogue.
3. Call graph -- callers/callees/unresolved callees from
   tools/sharc_dev/sharcinv.py's whole-inventory cross-reference (so cross-block calls
   resolve too), conditional vs unconditional and delayed vs non-delayed
   Type 8a calls, indirect calls, and a local scan for conditional returns
   (a 9b_abs jump through I4/M6 whose cond is not 31 -- sharcflow.py only
   matches the unconditional return word).
4. The annotated listing -- sw address, raw bytes, form name, a readable
   mnemonic with resolved operands, and inline annotations: literal
   addresses resolved to a named region (RAM or ROM, checked against the
   loaded blocks), float immediates decoded as IEEE-754 and flagged against
   a short list of known constants, loop trip counts with their body range,
   and calls with their resolved target and inventory label.
5. Summary -- the tools/sharc_dev/sharcinv.py feature vector, named tables touched,
   whether the parameter frame is referenced, and the distinct literal
   regions used.

Mnemonic rendering decodes compute-field register operands from the public
SHARC+ Core Programming Reference's Figure 18-1/18-2/18-3 and Table
18-11/13/15/16/18/19/21 bit layouts (tools/sharc_dev/sharcspec/compute_table.json's
figures_and_bit_layouts), using tools/sharc_dev/sharcinv.py's ALU_OPS/SHIFT_OPS/
SHORT_OPS opcode-name tables (built from the same file's aluop/shiftop/
shortcompute rows) rather than re-deriving them, and tools/sharc_dev/sharc_trace.py's
UREG_NAMES/UREG_CODES for register-file/status-register names rather than
recreating that 128-entry table. A handful of opcodes are called out in this
tool's task brief as unverified even where a name table already has an
entry for them (ALU 0xe0/0xd9/0xda, shifter 0xb0, multifunction 0x1a/0x1e/
0x1f): these always print with a loud "GAP" marker next to whatever name is
on file, never silently. Type10a_rel (and, as measured here, Type10a_abs
too) is absent from tools/sharc_dev/sharc_visa_tables.py's VISA form set entirely, so
any real occurrence surfaces as a disassembler desync -- resynced and
reported like a full-image scan does, not silently skipped. Forms with no
dedicated renderer here fall back to a field dump (name=value, one line)
plus named-region annotation on any address-shaped field -- never skipped,
per the same "loud gap beats silent gap" rule.
"""

from __future__ import annotations

from tools.sharc_dev.sharc_trace import UREG_CODES

from .decode import (
    _UNARY_RX_SHORT_OPS,
    FLOAT_SHORT_OPS,
    UNARY_ALU_OPS,
    reg_name,
    render_compute,
    render_shortcompute,
    ureg_name,
)
from .dossier import build_dossier, load_context, sha256_of
from .queue import (
    TARGET_OPCODE_COVERAGE_KEYS,
    build_engine_queue,
    canonical_digest,
    canonical_json_bytes,
    documented_function_entries,
    engine_candidates,
    sqlite_evidence,
    target_opcode_coverage,
)
from .regions import describe_literal, float_constant_name, name_literal_region
from .render import (
    _IMMOFF_DATA_BITS,
    _SHIFTIMM_MNEMONICS,
    ALWAYS_TRUE_COND,
    DIRECT_MEM_FORMS,
    DUAL_MEM_FORMS,
    IMMOFF_MEM_FORMS,
    INDEXED_MEM_FORMS,
    _space_dir,
    cond_name,
    render_instruction,
    sign_extend,
)

__all__ = [
    "ALWAYS_TRUE_COND",
    "DIRECT_MEM_FORMS",
    "DUAL_MEM_FORMS",
    "FLOAT_SHORT_OPS",
    "IMMOFF_MEM_FORMS",
    "INDEXED_MEM_FORMS",
    "TARGET_OPCODE_COVERAGE_KEYS",
    "UNARY_ALU_OPS",
    "UREG_CODES",
    "_IMMOFF_DATA_BITS",
    "_SHIFTIMM_MNEMONICS",
    "_UNARY_RX_SHORT_OPS",
    "_space_dir",
    "build_dossier",
    "build_engine_queue",
    "canonical_digest",
    "canonical_json_bytes",
    "cond_name",
    "describe_literal",
    "documented_function_entries",
    "engine_candidates",
    "float_constant_name",
    "load_context",
    "name_literal_region",
    "reg_name",
    "render_compute",
    "render_instruction",
    "render_shortcompute",
    "sha256_of",
    "sign_extend",
    "sqlite_evidence",
    "target_opcode_coverage",
    "ureg_name",
]
