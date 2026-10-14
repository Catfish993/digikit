"""The generated lowering (native/sharc/src/fast/lower/gen_forms.rs) is what
tools/sharc_dev/sharc_lowergen.py makes of tools/sharc_dev/sharc_core today.

Exactness against the interpreter is the Rust test `gen_lowering_matches_the_
interpreter` (cargo test --lib gen_, with the shared generated core); this one
only keeps the checked-in file from going stale when the semantics change.
"""

from __future__ import annotations

from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.sharc_dev import sharc_lowergen as g  # noqa: E402
from tools.sharc_dev import sharc_lowergen_rs as r  # noqa: E402

GEN = ROOT / "native" / "sharc" / "src" / "fast" / "lower" / "gen_forms.rs"


def checked_in_forms() -> list[str]:
    text = GEN.read_text()
    marker = "pub const GENERATED: &[&str] = &["
    line = next(ln for ln in text.splitlines() if ln.startswith(marker))
    return [x.strip().strip('"') for x in line[len(marker) : -2].split(",")]


def test_generated_file_is_current() -> None:
    assert r.emit_file(checked_in_forms()) == GEN.read_text()


def test_every_generated_form_traces_without_a_crash() -> None:
    tab = r.type_names()
    for name in checked_in_forms():
        leaves = g.trace_form(name, tab[name])
        assert any(not lf.refused for lf in leaves), name


def test_simd_leaves_exist() -> None:
    """A load through an index register has a PEy companion leaf: the SIMD
    state is a decision on MODE1, not a second implementation."""
    tab = r.type_names()
    leaves = g.trace_form("15a", tab["15a"])
    simd = [
        lf
        for lf in leaves
        if any(e[0] == "d" and "mode1" in repr(e[1]) and e[2] for e in lf.path)
    ]
    assert simd and any(
        eff[0] == "load" for lf in simd if not lf.refused for eff in lf.effects
    )


def test_shared_core_is_not_stale() -> None:
    """The transpiled core the Rust tests build against must come from the
    current tools/sharc_dev/sharc_core (`mise run regen`)."""
    import re

    from tools.sharc_dev import sharc_transpile

    tables = ROOT / "out" / "native" / "shared-core" / "gen" / "tables.rs"
    if not tables.exists():
        import pytest

        pytest.skip("no shared core generated here")
    m = re.search(r'CORE_SHA256: &str = "([0-9a-f]+)"', tables.read_text())
    assert m and m.group(1) == sharc_transpile.core_hash(), "run: mise run regen"
