"""Translate tools/sharc_dev/sharc_core (the SHARC+ instruction semantics) to Rust.

The translatable subset is tools/sharc_dev/sharc_core/SUBSET.md; this is the
translator it anticipates. It walks the syntax tree mypy builds from each
core module (``ast`` underneath, with names resolved and every expression
typed -- see tools/sharc_dev/sharc_transpile_infer.py for how the core's
unannotated handlers get their types), and emits one Rust module per core
module. Every module-level function becomes a Rust ``fn`` over the native
runtime in native/sharc-rt/src/rt.rs:

- Python ``int`` -> ``Int`` (i128), ``float`` -> ``f64``, ``bool``,
  ``str`` -> ``Sym`` (an interned string id; strings that only feed
  observability become ``SYM_DYN`` and are never built).
- The value lattice (``Const``/``Unknown``/``PartialConst``) -> ``V``
  (known-bit mask and bits); ``MR`` -> ``MR``; ``Operand | MR`` -> ``Spec``.
  ``Affine`` never exists natively (the concrete driver never builds one).
- ``FlagUpdate`` and the other NamedTuple records -> Rust structs.
- ``State`` -> the implicit ``s: &mut St`` every function takes; register
  maps (``state.uregs``, a ``_snapshot_uregs`` copy, a ``_pey_view``) ->
  ``RegView`` tags read through ``s``.
- Function values (op tables, ``transfer = _transfer if j else ...``) ->
  ``FnId``; a call through one is a ``match`` over the functions the
  points-to analysis says can reach it.
- A fork (``_copy``), a stop (``_stop``), any ``raise``, an unmodelled
  lookup (KeyError), or a boundary the runtime does not model returns
  ``Err(Trap)``: the caller undoes the instruction and lets the Python core
  execute it.
- Observability (``_event``, trace updates, Unknown and stop reasons) is
  erased.

The boundary SUBSET.md declares (values.py, the state.py helpers listed
there, generation-time decode) is not translated; ``BOUNDARY`` below lists
it together with the memory store (memory.py's byte access over the loader
image and write overlay) that the runtime implements by hand.

Anything outside the subset stops the translation with the module, line
and construct.

Usage (writes out/native/gen/core.rs; it holds no firmware, but lives
next to the firmware-derived block code it is compiled with):

    uv run python -m tools.sharc_dev.sharc_transpile [--out out/native/gen]
"""

from __future__ import annotations

from .driver import GENERATOR_VERSION, core_hash, render_tables, translate, write
from .tables import PARTIAL_MASKS
from .textpass import _cold_paths, _forwards_to, _only_sets_pc, _prune
from .types import (
    BOOL,
    INT,
    MR,
    NONE,
    OPT,
    SPEC,
    STR,
    VAL,
    VI,
    TranspileError,
    normalize_union,
)
from .values import _NOCONST, PS, _merge_item

__all__ = [
    "BOOL",
    "GENERATOR_VERSION",
    "INT",
    "MR",
    "NONE",
    "OPT",
    "PARTIAL_MASKS",
    "PS",
    "SPEC",
    "STR",
    "TranspileError",
    "VAL",
    "VI",
    "_NOCONST",
    "_cold_paths",
    "_forwards_to",
    "_merge_item",
    "_only_sets_pc",
    "_prune",
    "core_hash",
    "normalize_union",
    "render_tables",
    "translate",
    "write",
]
