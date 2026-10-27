"""`pk_build.source_hash`: the ColdFire object hash changes when only a header or an `.include` file changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.patchkit import pk_build, pk_toolchains

pytestmark = pytest.mark.skipif(
    pk_toolchains.m68k_bin("gcc") is None or pk_toolchains.m68k_bin("as") is None,
    reason="m68k toolchain not installed",
)


def hash_of(moddir: Path, source: str) -> str | None:
    return pk_build.source_hash(moddir, {"sources": [source]})


def test_header_change_changes_hash(tmp_path: Path) -> None:
    (tmp_path / "a.h").write_text("#define N 1\n")
    (tmp_path / "a.c").write_text('#include "a.h"\nint f(void) { return N; }\n')
    before = hash_of(tmp_path, "a.c")
    (tmp_path / "a.h").write_text("#define N 2\n")
    assert hash_of(tmp_path, "a.c") != before


def test_include_file_change_changes_hash(tmp_path: Path) -> None:
    gen = tmp_path / "build" / "gen"
    gen.mkdir(parents=True)
    (gen / "b.inc").write_text(".equ N, 1\n")
    (tmp_path / "a.s").write_text('.include "b.inc"\n.text\nnop\n')
    before = hash_of(tmp_path, "a.s")
    (gen / "b.inc").write_text(".equ N, 2\n")
    assert hash_of(tmp_path, "a.s") != before
