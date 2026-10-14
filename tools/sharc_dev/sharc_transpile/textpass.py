"""Text passes over the generated variant code and the regexes the translator checks it with."""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DECL_RE = re.compile(r"^\s*let mut (\w+): .* = Default::default\(\);$")
_ASSIGN_RE = re.compile(r"^\s*(?:let )?(\w+) = (.*);$")
# A call in an expression (anything but the constructors below) may have
# an effect: such an assignment stays even when its target is dead.
_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_:]*)\s*\(")
_PURE_CALLS = {
    "Some",
    "V::c",
    "Spec::V",
    "Spec::M",
    "RegView",
    "SpecView",
    "Trap",
    "stk_len_loops",
    "stk_len_call_stack",
    "stk_len_pc_stack",
    "stk_len_status_stack",
}


def _pure_rhs(rhs: str) -> bool:
    if "?" in rhs or "return" in rhs or "{" in rhs:
        return False
    for m in _CALL_RE.finditer(rhs):
        name = m.group(1)
        if name in _PURE_CALLS or re.fullmatch(r"U\d+::A\d+", name):
            continue
        return False
    return True


_SIG_RE = re.compile(r"^pub fn __VARIANT__\((.*)\) -> (.*) \{$")
_FWD_RE = re.compile(
    r"^\s*(?:return Ok\()?(crate::generated::image::spec_\d+::\w+)\((.*)\)\?(\))?;$"
)


def _only_sets_pc(text: str) -> str | None:
    """The statement a parameterless unit variant TEXT amounts to, when all
    it does is set the block's PC to a constant."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip() not in ("{", "}", "")]
    if len(lines) != 4 or not lines[0].startswith("#["):
        return None
    if not re.match(
        r"^pub fn __VARIANT__\(s: &mut St, rf: &mut Rf\) -> R<\(\)> \{$", lines[1]
    ):
        return None
    m = re.match(r"^(?:\{ )?rf\.pc = (\(?-?\d+i128\)?);(?: \};)?$", lines[2])
    if m is None or lines[3] != "return Ok(());":
        return None
    return "{ rf.pc = %s; }" % m.group(1)


def _forwards_to(text: str) -> str | None:
    """The variant TEXT calls, when all it does is call one with its own
    parameters in order and return that result (or unit)."""
    lines = [ln for ln in text.split("\n") if ln.strip() not in ("{", "}", "")]
    if len(lines) < 3 or not lines[0].startswith("#["):
        return None
    sig = _SIG_RE.match(lines[1])
    if sig is None:
        return None
    params = [p.strip() for p in sig.group(1).split(",") if p.strip()]
    names = []
    for p in params:
        name = p.split(":", 1)[0].replace("mut ", "").strip()
        names.append(name)
    body = lines[2:-1] if lines[-1].strip() == "}" else lines[2:]
    if len(body) == 1:
        m = _FWD_RE.match(body[0])
        if (
            m is None
            or not body[0].strip().startswith("return Ok(")
            or m.group(3) is None
        ):
            return None
    elif len(body) == 2 and body[1].strip() == "return Ok(());":
        m = _FWD_RE.match(body[0])
        if m is None or body[0].strip().startswith("return") or m.group(3):
            return None
        if sig.group(2).strip() != "R<()>":
            return None
    else:
        return None
    args = [a.strip() for a in m.group(2).split(",")]
    if args != names:
        return None
    return m.group(1)


def _prune(lines: list[str]) -> list[str]:
    """Partial-evaluation clean-up of one variant's text: drop assignments
    to locals nothing reads (their values were folded where used) when the
    assigned expression has no effect, their declarations, and empty
    blocks. It only removes code; rustc would drop the same, but the
    generated source is much smaller."""
    header = lines[:2]
    body = lines[2:]
    while True:
        text = "\n".join(body)
        counts: dict[str, int] = {}
        for name in _IDENT_RE.findall(text):
            counts[name] = counts.get(name, 0) + 1
        dead_lines = set()
        dead_names = set()
        defs: dict[str, list[int]] = {}
        for i, line in enumerate(body):
            m = _DECL_RE.match(line)
            if m:
                defs.setdefault(m.group(1), []).append(i)
                continue
            m = _ASSIGN_RE.match(line)
            if m and _pure_rhs(m.group(2)):
                defs.setdefault(m.group(1), []).append(i)
        for name, idxs in defs.items():
            # Every occurrence is one of these definitions: nothing reads it.
            own = 0
            for i in idxs:
                own += len(
                    re.findall(r"\b%s\b" % re.escape(name), body[i].split("=", 1)[0])
                )
            if counts.get(name, 0) == own:
                dead_names.add(name)
                dead_lines.update(idxs)
        for i, line in enumerate(body):
            m = re.match(r"^\s*let _ = (.*);$", line)
            if m and _pure_rhs(m.group(1).replace("{", "").replace("}", "")):
                dead_lines.add(i)
        new = [line for i, line in enumerate(body) if i not in dead_lines]
        # Empty blocks.
        out: list[str] = []
        for line in new:
            st = line.strip()
            if st == "}" and out:
                prev = out[-1].strip()
                if prev == "{":
                    out.pop()
                    continue
                if prev == "} else {":
                    out[-1] = out[-1].replace("} else {", "}")
                    continue
            out.append(line)
        if out == body:
            break
        body = out
    return header + body


# What makes a variant body impure: state or register-file access, a trap
# or `?`, a boundary call. Calls of other variants are checked one by one.
_EFFECT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:s|rf)(?![A-Za-z0-9_])|\?|Err\(|bnd::|stk_"
)
_VARIANT_CALL_RE = re.compile(r"crate::generated::image::spec_\d+::\w+")
_VARIANT_CALL_OPEN_RE = re.compile(r"crate::generated::image::spec_\d+::\w+\(")


def _can_trap(body: str, traps: dict[str, bool]) -> bool:
    """Whether a variant's code can return Err.  Only ``Err(`` and ``?`` do
    (an early ``return Err``/``?`` is the sole way out with a Trap).  A ``?``
    that directly follows a call of another variant counts only when that
    variant can trap (it is in TRAPS, filled callee-first); an unknown callee
    counts as trapping.  Any other ``?`` (runtime helpers) counts."""
    if "Err(" in body:
        return True
    q = body.count("?")
    for m in _VARIANT_CALL_OPEN_RE.finditer(body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            c = body[i]
            depth += (c == "(") - (c == ")")
            i += 1
        if i < len(body) and body[i] == "?" and not traps.get(m.group()[:-1], True):
            q -= 1
    return q > 0


_STRING_OR_Q = re.compile(r'"(?:[^"\\]|\\.)*"|\?|return Err\(')


def _cold_paths(text: str) -> str:
    """Block-mode variant text with every trap path marked unlikely: `x?` is
    `x.ce()?` and `return Err(t)` is `return cold_err(t)` (rt.rs ColdErr,
    cold_err), so LLVM places the trap paths out of line and the hot path of
    a block stays dense. Same values and control flow."""

    def sub(m: re.Match) -> str:
        t = m.group(0)
        if t == "?":
            return ".ce()?"
        if t.startswith("return Err("):
            return "return cold_err("
        return t

    return _STRING_OR_Q.sub(sub, text)


# Runtime calls that write the undo log (rt.rs St::log).
_LOGS_RE = re.compile(r"stk_(?:push|pop|set)_|s_set_special|bnd::_dm_write\(")
_USES_S = re.compile(r"(?<![A-Za-z0-9_:.])s(?![A-Za-z0-9_])")
_USES_SRF = re.compile(r"(?<![A-Za-z0-9_:.])(?:s|rf)(?![A-Za-z0-9_])")
