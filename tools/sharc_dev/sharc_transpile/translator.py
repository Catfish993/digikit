"""The translator: interning of functions, variants, tables and constants."""

from __future__ import annotations

import hashlib
import re
import types as pytypes
from typing import Any

from mypy import nodes as mn
from mypy import types as mt

from tools.sharc_dev import sharc_transpile_infer as infer

from .fn import FnT, _state_assigns
from .tables import (
    BOUNDARY,
    FACT_ATTRS,
    GEN_CFG_DEFAULT,
    NARROW_FIELDS,
    RT_FN_IDS,
    Syms,
    rust_ident,
)
from .textpass import (
    _EFFECT_RE,
    _LOGS_RE,
    _VARIANT_CALL_RE,
    _can_trap,
    _cold_paths,
    _forwards_to,
    _only_sets_pc,
)
from .types import (
    ANY,
    BOOL,
    FIELDS,
    FLOAT,
    FN,
    INT,
    MR,
    NONE,
    RECORDS,
    SPEC,
    SPECVIEW,
    STATE,
    STR,
    TABLE,
    VAL,
    VI,
    T,
    TranspileError,
    _type_tag,
    from_mypy,
)
from .values import _NOCONST, PS, E, FnSig, Tag, lit_float, lit_int


class Translator:
    def __init__(self, core: infer.Core) -> None:
        self.core = core
        self.syms = Syms()
        self.sigs: dict[str, FnSig] = {}
        self.fn_ids: dict[str, int] = {}
        self.fn_names: list[str] = []
        self.unions: dict[T, str] = {}
        self.union_order: list[T] = []
        self.consts_needed: dict[str, set[str]] = {}
        self.const_items: dict[str, dict[str, str]] = {}  # module -> name -> code
        self.lookup_fns: dict[str, str] = {}  # key -> rust fn text
        self.trap_sites: list[str] = []
        self.sym_fns: dict[str, tuple[str, Any]] = {}
        self.errors: list[str] = []
        self.table_ids: dict[int, int] = {}
        self.table_objs: list[Any] = []
        self.lookup_keys: set = set()
        self.affix_ids: dict[tuple, int] = {}
        self.called: set[str] = set()
        self.trap_ids: dict[str, int] = {}
        # Partial evaluation (block code): specialized variants by key and
        # by text, their Rust text per module, the fact attributes each may
        # change, the Rust names of static instructions, the run
        # configuration block code assumes, and the image's DO loop ends.
        self.variants: dict[Any, str] = {}
        self.variant_by_text: dict[str, str] = {}
        self.variant_texts: list[tuple[int, str]] = []
        self.variant_busy: set = set()
        self.variant_effects: dict[str, set[str]] = {}
        self.variant_modules = 16
        self.static_names: dict[int, str] = {}
        self.gen_cfg: dict[str, Any] = dict(GEN_CFG_DEFAULT)
        self.loop_ends: frozenset | None = None
        self.block_failures: list[tuple[int, str]] = []
        self._assigns: dict[str, set[str]] | None = None
        # Block mode (tools/sharc_dev/sharc_rsgen.py): variants keep the register file
        # in the block's local ``rf`` (rt.rs Rf) and take it after ``s``;
        # every register index must then be a generation-time constant.
        # ``variant_regs[path]`` is what a variant reads from the current
        # file, reads from the instruction-start snapshot, and writes.
        self.blk = False
        self.variant_regs: dict[str, tuple[frozenset, frozenset, frozenset]] = {}
        self.variant_putregs: dict[str, frozenset] = {}
        # The items every return of a variant has static (PS.parts), or None.
        self.variant_parts: dict[str, tuple | None] = {}
        self.variant_pure: dict[str, bool] = {}
        # Variants too small to call: the statement they amount to.
        self.variant_inline: dict[str, str] = {}
        self.variant_logs: dict[str, bool] = {}
        # Whether a variant can return Err (see _can_trap).
        self.variant_traps: dict[str, bool] = {}
        # Register facts known after a variant returns (a MODE1 it set).
        self.variant_facts_out: dict[str, dict] = {}
        # Whether a variant (with its callees) reads data memory.
        self.variant_memreads: dict[str, bool] = {}
        self.variant_memwrites: dict[str, bool] = {}
        self.variant_const: dict[str, Any] = {}
        self._intern_decode_names()

    # -- setup -------------------------------------------------------------

    def _intern_decode_names(self) -> None:
        from tools.sharc_dev import sharc_disasm

        for t in sharc_disasm.TYPES:
            self.syms.intern(t["name"])
            for f in t["fields"]:
                label = f if isinstance(f, str) else f["label"]
                self.syms.intern(label)
                self.syms.intern(label.split("[")[0])
        for s in (
            "unknown",
            "confident",
            "uncertain",
            "compute[22:16]",
            "compute[15:0]",
        ):
            self.syms.intern(s)

    def fn_id(self, fullname: str) -> int:
        if fullname not in self.fn_ids:
            self.fn_ids[fullname] = len(self.fn_names)
            self.fn_names.append(fullname)
        return self.fn_ids[fullname]

    def fn_const(self, fullname: str) -> str:
        if fullname in RT_FN_IDS:
            return RT_FN_IDS[fullname]
        self.fn_id(fullname)
        return "FN_" + re.sub(r"[^A-Za-z0-9]", "_", fullname.replace("sharc_core.", ""))

    def trap(self, where: str, kind: str) -> str:
        site = "%s: %s" % (where, kind)
        if site not in self.trap_ids:
            self.trap_ids[site] = len(self.trap_sites)
            self.trap_sites.append(site)
        return "Trap(%d)" % self.trap_ids[site]

    def union_name(self, t: T) -> str:
        if t not in self.unions:
            self.unions[t] = "U%d" % len(self.unions)
            self.union_order.append(t)
            for a in t.args:
                self.rust_type(a)
        return self.unions[t]

    def rust_type(self, t: T) -> str:
        k = t.kind
        simple = {
            "int": "Int",
            "float": "f64",
            "bool": "bool",
            "str": "Sym",
            "none": "()",
            "v": "V",
            "mr": "MR",
            "spec": "Spec",
            "vi": "VI",
            "insn": "Insn",
            "fields": "Fields",
            "regview": "RegView",
            "specview": "SpecView",
            "lstate": "()",
            "concrete": "()",
            "erased": "()",
            "fn": "FnId",
            "table": "TblId",
            "configmap": "()",
            "cfgset": "()",
            "state": "()",
        }
        if k in simple:
            return simple[k]
        if k == "opt":
            return "Option<%s>" % self.rust_type(t.args[0])
        if k == "tup":
            if len(t.args) == 1:
                return "(%s,)" % self.rust_type(t.args[0])
            return "(%s)" % ", ".join(self.rust_type(a) for a in t.args)
        if k == "vtup":
            return "Tup<%s>" % self.rust_type(t.args[0])
        if k == "rec":
            return t.args[0]
        if k == "union":
            return self.union_name(t)
        raise TranspileError("no Rust type for %r" % (t,))

    # -- signatures ---------------------------------------------------------

    def signature(self, fullname: str) -> FnSig:
        if fullname in self.sigs:
            return self.sigs[fullname]
        fdef = self.core.funcs[fullname]
        module, _, name = fullname.rpartition(".")
        ftype = fdef.type
        if not isinstance(ftype, mt.CallableType):
            raise TranspileError("%s has no signature" % fullname)
        params = []
        kinds = []
        state_params: set[str] = set()
        for arg, at in zip(fdef.arguments, ftype.arg_types, strict=True):
            pt = from_mypy(at, fullname + "(" + arg.variable.name + ")")
            if pt == STATE:
                state_params.add(arg.variable.name)
            params.append((arg.variable.name, pt, arg.initializer))
            kinds.append(arg.kind)
        ret = from_mypy(ftype.ret_type, fullname + " return")
        b = BOUNDARY.get(fullname)
        if b is not None:
            if b.params is not None:
                params = [
                    (p[0], bt, p[2])
                    for p, bt in zip(
                        [p for p in params if p[1] != STATE], b.params, strict=True
                    )
                ]
                state_params = set()
            if b.ret is not None:
                ret = b.ret
        sig = FnSig(
            fullname=fullname,
            module=module,
            name=name,
            params=params,
            param_kinds=kinds,
            ret=ret,
            state_params=state_params,
            rust_name=rust_ident(name),
        )
        self.sigs[fullname] = sig
        return sig

    def fn_path(self, fullname: str, absolute: bool = False) -> str:
        module, _, name = fullname.rpartition(".")
        if fullname in BOUNDARY:
            return "bnd::%s" % rust_ident(name)
        if absolute:
            return "crate::generated::core_i::%s::%s" % (
                module.split(".")[-1],
                rust_ident(name),
            )
        return "super::%s::%s" % (module.split(".")[-1], rust_ident(name))

    # -- specialized variants (block code) ------------------------------------

    def static_key(self, value: Any) -> Any:
        """A hashable key for static VALUE (instructions by their decode)."""
        cls = type(value).__name__
        if isinstance(value, PS):
            return (
                "ps",
                tuple(
                    "<dyn>" if p is _NOCONST else self.static_key(p)
                    for p in value.parts
                ),
            )
        if cls == "Instruction":
            return (
                "insn",
                value.type_name,
                value.length_bytes,
                value.kind,
                tuple(value.fields.items()),
            )
        if isinstance(value, dict):
            return ("dict", tuple((k, self.static_key(v)) for k, v in value.items()))
        if isinstance(value, pytypes.MappingProxyType):
            return ("dict", id(value))
        if isinstance(value, (list, tuple)) and cls in ("list", "tuple"):
            return (cls, tuple(self.static_key(v) for v in value))
        if isinstance(value, (pytypes.FunctionType, pytypes.BuiltinFunctionType)):
            return ("fn", getattr(value, "__module__", ""), value.__qualname__)
        try:
            hash(value)
        except TypeError:
            return ("id", id(value))
        return (cls, value)

    def variant(
        self, fullname: str, static: dict[str, Any], facts: dict[str, Any] | None = None
    ) -> str:
        """Rust path of FULLNAME specialized on STATIC (parameter -> value)
        and on FACTS (state attribute -> value at entry), generated on first
        use. ``variant_effects[path]`` is the set of fact attributes it may
        change."""
        facts = facts or {}
        key = (
            fullname,
            tuple(sorted((k, self.static_key(v)) for k, v in static.items())),
            tuple(sorted((k, self.static_key(v)) for k, v in facts.items())),
        )
        found = self.variants.get(key)
        if found is not None:
            return found
        if key in self.variant_busy:
            raise TranspileError("recursive specialization of %s" % fullname)
        self.variant_busy.add(key)
        try:
            fnt = FnT(
                self,
                fullname,
                static=static,
                pe=True,
                rust_name="__VARIANT__",
                facts=facts,
            )
            text = fnt.run("inline(always)")
        finally:
            self.variant_busy.discard(key)
        # Instructions that differ only in what a function does not read
        # get the same code: share it.
        summary = fnt.ret_summary()
        # What callers learn from a variant besides its code (returned
        # statics, facts it changes or leaves known) is part of its identity.
        facts_out = fnt.facts_out()
        digest = hashlib.sha1(
            "\n//".join(
                [
                    text,
                    repr(self.static_key(PS(summary))) if summary else "",
                    repr(sorted(fnt.effects)),
                    repr(sorted(facts_out.items())),
                    repr((fnt.memreads, fnt.memwrites)),
                ]
            ).encode()
        ).hexdigest()
        path = self.variant_by_text.get(digest)
        if path is None and self.blk:
            pc_only = _only_sets_pc(text)
            if pc_only is not None:
                # Callers use the statement itself (variant_inline).
                path = "@inline%d" % len(self.variant_inline)
                self.variant_inline[path] = pc_only
                self.variant_by_text[digest] = path
        if path is None and self.blk:
            # A variant that only calls another with its own parameters is
            # that one: callers call it directly.
            target = _forwards_to(text)
            if target is not None:
                path = target
                self.variant_by_text[digest] = path
        if path is None:
            index = len(self.variant_by_text)
            module = index % self.variant_modules
            name = "%s__v%d" % (rust_ident(fullname.rpartition(".")[2]), index)
            path = "crate::generated::image::spec_%02d::%s" % (module, name)
            self.variant_by_text[digest] = path
            text = text.replace("__VARIANT__", name)
            self.variant_texts.append((module, _cold_paths(text) if self.blk else text))
        self.variant_effects[path] = set(fnt.effects)
        self.variant_facts_out[path] = facts_out
        self.variant_memreads[path] = fnt.memreads
        self.variant_memwrites[path] = fnt.memwrites
        self.variant_parts[path] = summary
        # A variant without effects or traps whose every return has the same
        # static value folds to that value at its call sites.
        body = text.split("\n", 2)[-1]
        pure = not _EFFECT_RE.search(body) and all(
            self.variant_pure.get(m, False) for m in _VARIANT_CALL_RE.findall(body)
        )
        self.variant_pure[path] = pure
        # Whether it (or a callee) records anything in the undo log.
        self.variant_traps[path] = _can_trap(body, self.variant_traps)
        self.variant_logs[path] = bool(_LOGS_RE.search(body)) or any(
            self.variant_logs.get(m, True) for m in _VARIANT_CALL_RE.findall(body)
        )
        rc = fnt.ret_const()
        self.variant_const[path] = rc if pure else _NOCONST
        self.variant_putregs[path] = frozenset(fnt.putregs)
        self.variant_regs[path] = (
            frozenset(fnt.rregs),
            frozenset(fnt.oregs),
            frozenset(fnt.wregs),
        )
        self.variants[key] = path
        return path

    def state_assigns(self, fullname: str) -> set[str]:
        """FACT_ATTRS FULLNAME (with everything it calls) may assign."""
        if self._assigns is None:
            self._assigns = _state_assigns(self.core)
        return self._assigns.get(fullname, set(FACT_ATTRS))

    # -- constants ------------------------------------------------------------

    def const_value(self, value: Any, t: T, where: str) -> str:
        """Rust code for live Python VALUE at native type T."""
        if isinstance(value, Tag):
            return value.code
        if id(value) in self.static_names:
            return self.static_names[id(value)]
        if type(value).__name__ == "Instruction" or (
            isinstance(value, dict) and t in (FIELDS, ANY)
        ):
            return "__NOREPR__"
        if t.kind == "opt":
            if value is None:
                return "None"
            return "Some(%s)" % self.const_value(value, t.args[0], where)
        if t == SPECVIEW:
            if isinstance(value, pytypes.MappingProxyType) and not value:
                return "SpecView::EMPTY"
            if value is None:
                return "SpecView::NONE"
        if t.kind == "union":
            for i, a in enumerate(t.args):
                if self._value_fits(value, a):
                    return "%s::A%d(%s)" % (
                        self.union_name(t),
                        i,
                        self.const_value(value, a, where),
                    )
            raise TranspileError("%s: %r fits no member of %r" % (where, value, t))
        if isinstance(value, bool):
            if t == INT:
                return lit_int(int(value))
            return "true" if value else "false"
        if isinstance(value, int):
            if t == FLOAT:
                return lit_float(float(value))
            if t == BOOL:
                return "true" if value else "false"
            if t == VI:
                return "VI::I(%s)" % lit_int(value)
            return lit_int(value)
        if isinstance(value, float):
            return lit_float(value)
        if isinstance(value, str):
            return self.syms.ident(value)
        if value is None:
            if t == NONE:
                return "()"
            raise TranspileError("%s: None at %r" % (where, t))
        cls = type(value).__name__
        if cls == "Const":
            code = "V::c(%s)" % lit_int(value.value)
            if t == SPEC:
                return "Spec::V(%s)" % code
            return code
        if cls == "Unknown":
            return "Spec::V(V::UNK)" if t == SPEC else "V::UNK"
        if cls == "MR":
            code = "MR::new(%s, %s)" % (lit_int(value.mask), lit_int(value.bits))
            return "Spec::M(%s)" % code if t == SPEC else code
        if isinstance(value, pytypes.FunctionType):
            name = self.core.live_funcs.get(id(value))
            if name is None:
                raise TranspileError(
                    "%s: function %r outside the core" % (where, value)
                )
            return self.fn_const(name)
        if isinstance(value, pytypes.BuiltinFunctionType) or value is abs:
            return self.fn_const("builtins." + value.__name__)
        if t.kind == "rec":
            rec = t.args[0]
            fields = self.record_fields(rec)
            parts = []
            for fname, ftype in fields:
                code = self.const_value(getattr(value, fname), ftype, where)
                if fname in NARROW_FIELDS.get(rec, ()):
                    code = "(%s) as %s" % (code, NARROW_FIELDS[rec][fname])
                parts.append("%s: %s" % (fname, code))
            return "%s { %s }" % (rec, ", ".join(parts))
        if isinstance(value, (tuple, list)):
            if t.kind == "tup":
                if len(value) != len(t.args):
                    raise TranspileError("%s: tuple length" % where)
                inner = ", ".join(
                    self.const_value(v, a, where)
                    for v, a in zip(value, t.args, strict=True)
                )
                return "(%s,)" % inner if len(value) == 1 else "(%s)" % inner
            if t.kind == "vtup":
                inner = ", ".join(self.const_value(v, t.args[0], where) for v in value)
                return "Tup::from_slice(&[%s])" % inner
        if (
            isinstance(value, (dict, frozenset, set, pytypes.MappingProxyType))
            and t == TABLE
        ):
            return "%d" % self.table_id(value)
        raise TranspileError("%s: no constant for %r as %r" % (where, value, t))

    def _value_fits(self, value: Any, t: T) -> bool:
        if isinstance(value, bool):
            return t in (BOOL, INT)
        if isinstance(value, int):
            return t in (INT, VI)
        if isinstance(value, str):
            return t == STR
        if isinstance(value, tuple):
            if t.kind == "vtup":
                return all(self._value_fits(v, t.args[0]) for v in value)
            if t.kind == "tup":
                return len(value) == len(t.args) and all(
                    self._value_fits(v, a) for v, a in zip(value, t.args, strict=True)
                )
            return False
        cls = type(value).__name__
        if cls in ("Const", "Unknown"):
            return t in (VAL, SPEC)
        if cls == "MR":
            return t in (MR, SPEC)
        return False

    def table_id(self, obj: Any) -> int:
        if id(obj) not in self.table_ids:
            self.table_ids[id(obj)] = len(self.table_objs)
            self.table_objs.append(obj)
        return self.table_ids[id(obj)]

    def record_fields(self, rec: str) -> list[tuple[str, T]]:
        full = {v: k for k, v in RECORDS.items()}[rec]
        module, _, cls = full.rpartition(".")
        info = self.core.trees[module].names[cls].node
        assert isinstance(info, mn.TypeInfo)
        out = []
        if info.tuple_type is not None:
            names = [
                n
                for n in info.names
                if not n.startswith("_")
                and n in info.metadata.get("namedtuple", {}).get("fields", info.names)
            ]
            tt = info.tuple_type
            fieldnames = [n for n in _namedtuple_fields(info)]
            for fname, ft in zip(fieldnames, tt.items, strict=True):
                out.append((fname, from_mypy(ft, full + "." + fname)))
            del names
            return out
        # dataclass
        for name in _dataclass_fields(info):
            var = info.names[name].node
            assert isinstance(var, mn.Var)
            out.append((name, from_mypy(var.type, full + "." + name)))
        return out

    def lookup_fn(self, tables: list, vt: T, kt: T, where: str) -> str:
        if len(tables) == 1:
            return self._lookup_one(tables[0], vt, kt, where)
        ids = sorted(self.table_id(t) for t in tables)
        name = "tbl_multi_%s_get" % "_".join(str(i) for i in ids)
        key = ("multi", tuple(ids), vt, kt)
        if key not in self.lookup_keys:
            arms = []
            for t in tables:
                one = self._lookup_one(t, vt, kt, where)
                arms.append("%d => %s(k)" % (self.table_id(t), one))
            arms.append("_ => None")
            self.lookup_fns[name] = (
                "#[inline(always)]\npub fn %s(t: TblId, k: %s) -> Option<%s> {\n    match t { %s }\n}"
                % (name, self.rust_type(kt), self.rust_type(vt), ", ".join(arms))
            )
            self.lookup_keys.add(key)
        return name

    def _lookup_one(self, table: Any, vt: T, kt: T, where: str) -> str:
        tid = self.table_id(table)
        name = "tbl_%d_get_%s" % (tid, _type_tag(vt))
        if name in self.lookup_fns:
            return name
        arms = []
        for key, value in table.items():
            kcode = self._pattern(key, kt, where)
            if kcode is None:
                continue
            if isinstance(value, (dict, frozenset, pytypes.MappingProxyType)):
                vcode = "%d" % self.table_id(value)
            else:
                vcode = self.const_value(value, vt, where)
            arms.append("%s => Some(%s)" % (kcode, vcode))
        arms.append("_ => None")
        self.lookup_fns[name] = (
            "#[inline(always)]\npub fn %s(k: %s) -> Option<%s> {\n    match k {\n        %s\n    }\n}"
            % (name, self.rust_type(kt), self.rust_type(vt), ",\n        ".join(arms))
        )
        return name

    def _pattern(self, key: Any, kt: T, where: str) -> str | None:
        if isinstance(key, bool):
            if kt == BOOL:
                return "true" if key else "false"
            return lit_int(int(key))
        if isinstance(key, int):
            if kt not in (INT, BOOL):
                return None
            return lit_int(key)
        if isinstance(key, str):
            if kt != STR:
                return None
            return self.syms.ident(key)
        if isinstance(key, tuple):
            if kt.kind != "tup" or len(kt.args) != len(key):
                return None
            parts = [
                self._pattern(k, t, where) for k, t in zip(key, kt.args, strict=True)
            ]
            if any(p is None for p in parts):
                return None
            return "(%s)" % ", ".join(parts)  # type: ignore[arg-type]
        raise TranspileError("%s: table key %r" % (where, key))

    def contains_fn(self, table: Any, kt: T, where: str) -> str:
        tid = self.table_id(table)
        name = "tbl_%d_contains_%s" % (tid, _type_tag(kt))
        if name not in self.lookup_fns:
            keys = (
                list(table.keys())
                if isinstance(table, (dict, pytypes.MappingProxyType))
                else sorted(table, key=repr)
            )
            pats = [
                p for p in (self._pattern(k, kt, where) for k in keys) if p is not None
            ]
            body = "matches!(k, %s)" % " | ".join(pats) if pats else "false"
            self.lookup_fns[name] = (
                "#[inline(always)]\npub fn %s(k: %s) -> bool {\n    %s\n}"
                % (name, self.rust_type(kt), body)
            )
        return name

    def sym_concat_fn(self, suffix: str) -> str:
        self.syms.intern(suffix)
        name = "sym_concat_%d" % self.syms.intern(suffix)
        self.sym_fns[name] = ("concat", suffix)
        return name

    def sym_affix_fn(self, which: str, affixes: tuple) -> str:
        key = (which, tuple(affixes))
        if key not in self.affix_ids:
            self.affix_ids[key] = len(self.affix_ids)
        name = "sym_%s_%d" % (which, self.affix_ids[key])
        self.sym_fns[name] = (which, tuple(affixes))
        return name

    def sym_map_fn(self, which: str) -> str:
        name = "sym_%s" % which
        self.sym_fns[name] = (which, None)
        return name

    def record_defaults(self, cls_obj: Any) -> dict[str, Any]:
        import dataclasses

        if dataclasses.is_dataclass(cls_obj):
            out = {}
            for f in dataclasses.fields(cls_obj):
                if f.default is not dataclasses.MISSING:
                    out[f.name] = f.default
            return out
        return dict(getattr(cls_obj, "_field_defaults", {}))

    def sym_fns_rust(self) -> str:
        # Materialize sym helpers over every interned string.
        out = []
        for name, (which, arg) in sorted(self.sym_fns.items()):
            names = list(self.syms.names)
            if which == "concat":
                arms = []
                for i, s in enumerate(names):
                    if i == 1:
                        continue
                    target = s + arg
                    if target in self.syms.ids:
                        arms.append(
                            "%s => %s" % (self.syms.idents[i], self.syms.ident(target))
                        )
                arms.append("_ => SYM_DYN")
                out.append(
                    "#[inline(always)]\npub fn %s(a: Sym) -> Sym {\n    match a { %s }\n}"
                    % (name, ", ".join(arms))
                )
            elif which in ("startswith", "endswith"):
                hits = [
                    self.syms.idents[i]
                    for i, s in enumerate(names)
                    if i != 1 and getattr(s, which)(arg)
                ]
                body = "matches!(a, %s)" % " | ".join(hits) if hits else "false"
                out.append(
                    "#[inline(always)]\npub fn %s(a: Sym) -> bool {\n    %s\n}"
                    % (name, body)
                )
            elif which in ("lower", "upper"):
                arms = []
                for i, s in enumerate(names):
                    if i == 1:
                        continue
                    t = getattr(s, which)()
                    if t != s and t in self.syms.ids:
                        arms.append(
                            "%s => %s" % (self.syms.idents[i], self.syms.ident(t))
                        )
                arms.append("_ => a")
                out.append(
                    "#[inline(always)]\npub fn %s(a: Sym) -> Sym {\n    match a { %s }\n}"
                    % (name, ", ".join(arms))
                )
        return "\n\n".join(out)

    def module_const(self, fullname: str, t: T, where: str) -> E:
        """A reference to module-level constant FULLNAME."""
        value = self.core.live_value(fullname)
        if isinstance(value, (dict, frozenset, set, pytypes.MappingProxyType)):
            if t == SPECVIEW:
                return E(self.const_value(value, t, where), t, const=value)
            return E("%d" % self.table_id(value), TABLE, const=value)
        if isinstance(value, pytypes.FunctionType):
            name = self.core.live_funcs[id(value)]
            return E(
                self.fn_const(name),
                FN,
                const=value,
                pts=frozenset({infer.Obj("fn", name)}),
            )
        return E(self.const_value(value, t, where), t, const=value)


def _namedtuple_fields(info: mn.TypeInfo) -> list[str]:
    tt = info.tuple_type
    assert tt is not None
    # mypy stores the field names in the NamedTuple's own 'namedtuple' plugin
    # data; fall back to the class body's annotated names in order.
    names = []
    for stmt in info.defn.defs.body:
        if (
            isinstance(stmt, mn.AssignmentStmt)
            and isinstance(stmt.lvalues[0], mn.NameExpr)
            and (stmt.type is not None or getattr(stmt, "new_syntax", False))
        ):
            names.append(stmt.lvalues[0].name)
    if len(names) != len(tt.items):
        raise TranspileError("cannot read the fields of %s" % info.fullname)
    return names


def _dataclass_fields(info: mn.TypeInfo) -> list[str]:
    names = []
    for stmt in info.defn.defs.body:
        if isinstance(stmt, mn.AssignmentStmt) and isinstance(
            stmt.lvalues[0], mn.NameExpr
        ):
            names.append(stmt.lvalues[0].name)
    return names
