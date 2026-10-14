"""Native types of the translated subset, their constants and mypy type conversion."""

from __future__ import annotations

import hashlib
import types as pytypes
from dataclasses import dataclass
from typing import Any

from mypy import types as mt


class TranspileError(Exception):
    """A construct outside the translatable subset."""


# ---------------------------------------------------------------------------
# Native types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class T:
    """A native type. KIND is one of the names below; ARGS are T or str."""

    kind: str
    args: tuple = ()

    def __repr__(self) -> str:
        if not self.args:
            return self.kind
        return "%s(%s)" % (self.kind, ", ".join(repr(a) for a in self.args))


INT = T("int")
FLOAT = T("float")
BOOL = T("bool")
STR = T("str")
NONE = T("none")
VAL = T("v")  # Const | Unknown | PartialConst (| Affine: never native)
MR = T("mr")
SPEC = T("spec")  # Operand | MR
VI = T("vi")  # Value | int (memory addresses)
STATE = T("state")
INSN = T("insn")
FIELDS = T("fields")
REGVIEW = T("regview")
SPECVIEW = T("specview")
LSTATE = T("lstate")  # list[State]: Ok(()) natively
CONCRETE = T("concrete")  # the loader image handle
ERASED = T("erased")  # an observability-only value
FN = T("fn")
TABLE = T("table")
ANY = T("any")
CONFIG_MAP = T("configmap")  # state.provisional_interpretations
CFGSET = T("cfgset")  # state.provisional_forms


def OPT(t: T) -> T:
    return T("opt", (t,))


def TUP(*items: T) -> T:
    return T("tup", tuple(items))


def VTUP(t: T) -> T:
    return T("vtup", (t,))


def REC(name: str) -> T:
    return T("rec", (name,))


def UNION(items: list[T]) -> T:
    uniq: list[T] = []
    for i in items:
        if i not in uniq:
            uniq.append(i)
    uniq.sort(key=repr)
    return T("union", tuple(uniq))


RECORDS = {
    "sharc_core.values.FlagUpdate": "FlagUpdate",
    "sharc_core.compute_mult.MultSpec": "MultSpec",
    "sharc_core.compute_multi.MultifnOperands": "MultifnOperands",
    "sharc_core.state.Pending": "Pending",
    "sharc_core.state.Loop": "Loop",
}
VALUE_CLASSES = {
    "sharc_core.values.Const",
    "sharc_core.values.Unknown",
    "sharc_core.values.Affine",
    "sharc_core.values.PartialConst",
}


def normalize_union(items: list[T]) -> T:
    flat: list[T] = []
    for i in items:
        if i.kind == "union":
            flat.extend(i.args)
        elif i.kind == "opt":
            flat.append(NONE)
            flat.append(i.args[0])
        else:
            flat.append(i)
    if ANY in flat:
        return ANY
    has_none = NONE in flat
    rest = [i for i in flat if i != NONE]
    kinds = set(rest)
    if FIELDS in kinds and VAL in kinds:
        # Const | dict[str, int] | None: _load_normal_ureg's result. The
        # dict (a combined-PX summary) only feeds observability, so natively
        # it is None; the Const is kept.
        kinds.discard(FIELDS)
    if SPECVIEW in kinds:
        # Mapping[str, Operand | MR] | None: the view carries None itself.
        kinds.discard(SPECVIEW)
        if kinds:
            raise TranspileError("special-register view mixed with %s" % kinds)
        return SPECVIEW
    if BOOL in kinds and INT in kinds:
        kinds.discard(BOOL)
    if VAL in kinds and MR in kinds:
        kinds -= {VAL, MR}
        kinds.add(SPEC)
    if SPEC in kinds:
        kinds -= {VAL, MR}
    if VAL in kinds and INT in kinds and len(kinds) == 2:
        kinds = {VI}
    # Tuples of the same length join elementwise.
    tups = [k for k in kinds if k.kind == "tup"]
    if len(tups) > 1 and len({len(t.args) for t in tups}) == 1:
        joined = TUP(
            *[
                normalize_union([t.args[i] for t in tups])
                for i in range(len(tups[0].args))
            ]
        )
        kinds -= set(tups)
        kinds.add(joined)
    vtups = [k for k in kinds if k.kind == "vtup"]
    if len(vtups) > 1:
        joined = VTUP(normalize_union([t.args[0] for t in vtups]))
        kinds -= set(vtups)
        kinds.add(joined)
    if LSTATE in kinds and len(kinds) > 1:
        kinds = {LSTATE}
    if len(kinds) == 0:
        base = NONE
    elif len(kinds) == 1:
        base = next(iter(kinds))
    else:
        base = UNION(sorted(kinds, key=repr))
    if has_none and base != NONE:
        if base.kind == "opt":
            return base
        return OPT(base)
    return base


def from_mypy(t: mt.Type | None, where: str = "") -> T:
    if t is None:
        return ANY
    p = mt.get_proper_type(t)
    if isinstance(p, mt.AnyType):
        return ANY
    if isinstance(p, mt.NoneType):
        return NONE
    if isinstance(p, mt.UninhabitedType):
        return NONE
    if isinstance(p, mt.LiteralType):
        return from_mypy(p.fallback, where)
    if isinstance(p, mt.UnionType):
        return normalize_union([from_mypy(i, where) for i in p.items])
    if isinstance(p, mt.TupleType):
        fb = p.partial_fallback.type.fullname
        if fb in RECORDS:
            return REC(RECORDS[fb])
        return TUP(*[from_mypy(i, where) for i in p.items])
    if isinstance(p, (mt.CallableType, mt.Overloaded)):
        return FN
    if isinstance(p, mt.TypeType):
        return ANY
    if isinstance(p, mt.Instance):
        name = p.type.fullname
        if name == "builtins.int":
            return INT
        if name == "builtins.bool":
            return BOOL
        if name == "builtins.float":
            return FLOAT
        if name == "builtins.str":
            return STR
        if name in VALUE_CLASSES:
            return VAL
        if name == "sharc_core.state.MR":
            return MR
        if name == "sharc_core.state.State":
            return STATE
        if name == "tools.sharc_dev.sharc_disasm.Instruction":
            return INSN
        if name == "tools.sharc_dev.sharcldr.LoadedMemory":
            return CONCRETE
        if name in RECORDS:
            return REC(RECORDS[name])
        if name in ("builtins.tuple",):
            return VTUP(from_mypy(p.args[0], where))
        if name == "builtins.list":
            item = from_mypy(p.args[0], where)
            if item == STATE:
                return LSTATE
            return VTUP(item)
        if name in (
            "builtins.dict",
            "typing.Mapping",
            "collections.abc.Mapping",
            "types.MappingProxyType",
            "typing.MutableMapping",
        ):
            k = from_mypy(p.args[0], where)
            v = from_mypy(p.args[1], where)
            if k == STR and v == INT:
                return FIELDS
            if k == INT and v in (VAL,):
                return REGVIEW
            if k == STR and v in (SPEC, VAL, MR):
                return SPECVIEW
            if k == STR and v == STR:
                return CONFIG_MAP
            if v == FN or v.kind == "table":
                return TABLE
            return TABLE
        if name in ("builtins.frozenset", "builtins.set"):
            return TABLE
        if name == "builtins.bytearray" or name == "builtins.bytes":
            return ERASED
        if name == "builtins.object":
            return ANY
        if name == "builtins.dict_items":
            return ANY
        if name == "sharc_core.memory.UnmodeledMMR":
            return ERASED
        if name == "builtins.ValueError" or name == "builtins.KeyError":
            return ERASED
        if name == "builtins.function":
            return FN
    raise TranspileError("no native type for %s %s" % (p, where))


# ---------------------------------------------------------------------------
# Function translation
# ---------------------------------------------------------------------------

STACKS = {
    "loops": REC("Loop"),
    "loop_slots": TUP(VAL, VAL),
    "call_stack": INT,
    "pc_stack": INT,
    "status_stack": TUP(VAL, VAL, VAL),
}


def STK(name: str) -> T:
    return T("stk", (name,))


def _type_of_value(value: Any) -> T:
    """The native type of a static Python value."""
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return FLOAT
    if isinstance(value, str):
        return STR
    if value is None:
        return NONE
    cls = type(value).__name__
    if cls in ("Const", "Unknown", "PartialConst"):
        return VAL
    if cls == "MR":
        return MR
    for full, rec in RECORDS.items():
        if full.endswith("." + cls):
            return REC(rec)
    if cls == "Instruction":
        return INSN
    if isinstance(value, tuple):
        return TUP(*[_type_of_value(v) for v in value])
    if isinstance(value, pytypes.FunctionType):
        return FN
    return ANY


def _contains(t: T, x: T) -> bool:
    if t == x:
        return True
    return any(isinstance(a, T) and _contains(a, x) for a in t.args)


def _has_any(t: T) -> bool:
    if t == ANY:
        return True
    return any(isinstance(a, T) and _has_any(a) for a in t.args)


def _type_tag(t: T) -> str:
    return hashlib.sha1(repr(t).encode()).hexdigest()[:8]
