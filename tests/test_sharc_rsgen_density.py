"""Hot-code density of the generated block code (docs/design/aot-mask-free.md, section 7).

The generated Rust is not compiled here; these pin the text the generator emits
for the cold paths and the shared exits, with no firmware:
- every `?` and `return Err(` of a block-mode variant is marked unlikely,
- the successor match calls one out-of-line chain function per distinct set,
- the trap tail records the trap at the site and applies it once per region.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sharc_rsgen as rg
import sharc_transpile as tp


def test_cold_paths_mark_question_marks_and_early_errors() -> None:
    text = (
        "let a = f(s, rf)?;\n"
        "if x { return Err(Trap(17)); }\n"
        'let m = "what? Err(" ;\n'
        "let b = g(h(1)?)?;\n"
    )
    out = tp._cold_paths(text)
    assert "let a = f(s, rf).ce()?;" in out
    assert "return cold_err(Trap(17));" in out
    assert 'let m = "what? Err(" ;' in out  # string literals are left alone
    assert "let b = g(h(1).ce()?).ce()?;" in out


def _body(start: int, idx: int, mode1: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(block=SimpleNamespace(start=start), mode1=mode1, idx=idx)


def test_route_calls_one_chain_function_per_successor_set() -> None:
    rg.TAIL.reset("r_X")
    own, other = _body(0x100, 0), _body(0x200, 1)
    by_start = {0x100: [own], 0x200: [other]}
    chain: dict[int, int] = {}
    succ = {0x200, 0x300, 0x400}
    lines = rg.route(own, None, by_start, succ, "", chain)
    text = "\n".join(lines)
    # The body of this region stays an arm; the other region's blocks are the
    # chain function's job.
    assert "0x200 => { at = 1; continue 'r; }" in text
    assert "_ => break 'r r_X_c0(rf.pc as u32)," in text
    assert "0x300" not in text and "0x400" not in text
    assert chain == {0x300: 0, 0x400: 1}
    again = rg.route(own, None, by_start, succ, "", chain)
    assert "r_X_c0(rf.pc as u32)" in "\n".join(again)
    assert len(rg.TAIL.fns) == 1
    fn = rg.TAIL.chain_fn_text()
    assert "#[inline(never)]" in fn and "fn r_X_c0(pc: u32) -> u32 {" in fn
    assert "0x300 => crate::EXIT_CHAIN + 0," in fn
    assert "0x400 => crate::EXIT_CHAIN + 1," in fn
    assert "_ => crate::EXIT_NEXT," in fn


def test_route_without_chain_falls_to_the_dispatcher() -> None:
    rg.TAIL.reset("r_Y")
    own = _body(0x100, 0)
    lines = rg.route(own, None, {0x100: [own]}, {0x500}, "", None)
    text = "\n".join(lines)
    assert "_ => break 'r crate::EXIT_NEXT," in text
    assert "continue 'b0" in text  # back to its own start
    assert not rg.TAIL.fns
