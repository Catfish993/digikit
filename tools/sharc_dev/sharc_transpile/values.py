"""Static values of partial evaluation, translated expressions and function signatures."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .types import T

# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


class _NoConst:
    """E.const when the value is not known at generation time."""

    def __repr__(self) -> str:
        return "<dynamic>"


_NOCONST: Any = _NoConst()


class PS:
    """A partially static tuple (partial evaluation): PARTS holds each
    item's generation-time value, or _NOCONST where it is known only at run
    time. A local holding one keeps its Rust value too."""

    __slots__ = ("parts",)

    def __init__(self, parts: tuple) -> None:
        self.parts = parts

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PS) or len(other.parts) != len(self.parts):
            return False
        for a, b in zip(self.parts, other.parts, strict=True):
            if (a is _NOCONST) != (b is _NOCONST):
                return False
            if a is not _NOCONST and not _same_static(a, b):
                return False
        return True

    def __hash__(self) -> int:
        return len(self.parts)

    def __repr__(self) -> str:
        return "PS%r" % (self.parts,)


def _ps_parts(e: E) -> tuple | None:
    """The per-item static values of tuple E (None when no item is)."""
    if _is_const(e) and isinstance(e.const, tuple) and not isinstance(e.const, Tag):
        return tuple(e.const)
    if e.parts is not None and any(p is not _NOCONST for p in e.parts):
        return e.parts
    return None


def _ps_item(e: E) -> Any:
    """E as an item of a partially static tuple: its value, a nested PS,
    or _NOCONST."""
    if _is_const(e) and not isinstance(e.const, Tag):
        return e.const
    parts = _ps_parts(e)
    if parts is not None:
        return PS(parts)
    return _NOCONST


def _merge_item(vals: list) -> Any:
    """Join of partially static items: equal statics stay, a None next to
    partially static tuples (an Optional) keeps their parts."""
    ps = [v for v in vals if isinstance(v, PS)]
    if ps:
        if all(isinstance(v, PS) or v is None for v in vals) and all(
            p == ps[0] for p in ps[1:]
        ):
            return ps[0]
        return _NOCONST
    v0 = vals[0]
    if v0 is _NOCONST or isinstance(v0, Tag):
        return _NOCONST
    if any(v is _NOCONST or not _same_static(v, v0) for v in vals[1:]):
        return _NOCONST
    return v0


@dataclass(frozen=True)
class Tag:
    """A static value that exists only natively (a register or special
    register view, the loader image handle): its Rust code."""

    code: str


@dataclass
class E:
    """A translated expression: Rust CODE of native type T. S is True when
    the code reads or writes the state (so it cannot be an argument next to
    another use of ``s``). CONST is the value when it is known at generation
    time (a literal, a module constant, or partial evaluation), else
    _NOCONST."""

    code: str
    t: T
    s: bool = False
    const: Any = _NOCONST
    pts: frozenset | None = None  # points-to set for FN/TABLE values
    # Values a dynamic int can take (a DO loop's end address: one of the
    # image's DO instructions' ends), or None.
    vset: frozenset | None = None
    # A dynamic tuple's items known at generation time (PS.parts), or None.
    parts: tuple | None = None


def _is_const(e: E) -> bool:
    return e.const is not _NOCONST


def lit_int(v: int) -> str:
    return "(%di128)" % v if v < 0 else "%di128" % v


def lit_float(v: float) -> str:
    if math.isnan(v) or math.isinf(v) or v == 0.0:
        return "f64::from_bits(0x%016x)" % (
            int.from_bytes(__import__("struct").pack(">d", v), "big")
        )
    r = repr(v)
    if "e" not in r and "." not in r:
        r += ".0"
    return "(%s_f64)" % r


@dataclass
class FnSig:
    fullname: str
    module: str
    name: str
    params: list[tuple[str, T, Any]]  # (name, type, default node or None)
    param_kinds: list[Any]
    ret: T
    state_params: set[str]
    rust_name: str


def _same_static(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return type(a) is type(b) and bool(a == b)
    except Exception:  # noqa: BLE001
        return False
