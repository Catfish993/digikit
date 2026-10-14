"""tools/coldfire/fw.py and tools/coldfire/atlas.py: pure helpers always, database queries when
the derived databases exist (out/cfdb, out/sharcdb, out/sections)."""

import os
import subprocess
import sys

import pytest
from support.paths import REPO_ROOT

ROOT = str(REPO_ROOT)

from tools import python_env  # noqa: E402
from tools.coldfire import (  # noqa: E402
    atlas,  # noqa: E402
    fw,  # noqa: E402
)

CF_DB = os.path.join(ROOT, "out", "cfdb", "dt2-1.17-emac.sqlite")
SHARC_DB = os.path.join(ROOT, "out", "sharcdb", "dt2-1.17.sqlite")
SECTION = os.path.join(ROOT, "out", "sections", "dt2-1.17", "section_3_MAIN_OS.bin")
need_cf = pytest.mark.skipif(not os.path.exists(CF_DB), reason="no cfdb for dt2-1.17")
need_sharc = pytest.mark.skipif(
    not os.path.exists(SHARC_DB), reason="no sharcdb for dt2-1.17"
)


def test_capped_adds_tail():
    assert fw.capped(["a", "b", "c"], 2) == "a\nb\n... 1 more (--max N, 0 = all)"
    assert fw.capped(["a", "b"], 0) == "a\nb"
    assert fw.capped(["a"], 5) == "a"


def test_parse_addr_and_cpu_choice():
    assert fw.parse_addr("0x401be1be") == 0x401BE1BE
    assert fw.parse_addr("FUN_401be1be") == 0x401BE1BE
    assert fw.parse_addr("401be1be") == 0x401BE1BE
    assert fw.parse_addr("PatternGridView") is None
    assert fw.want_sharc("0xb80105", None)
    assert not fw.want_sharc("0x401be1be", None)
    assert fw.want_sharc("0x401be1be", "sharc")


def test_tri_query():
    assert fw.tri_query("pattern step") == '"pattern" AND "step"'
    assert fw.tri_query("wrong ui") == '"wrong ui"'
    with pytest.raises(fw.FwError):
        fw.tri_query("  ")


def test_role_rules():
    assert atlas.role_of("SourcePageView") == "page"
    assert atlas.role_of("TempoMenuView") == "menu"
    assert atlas.role_of("ProgressWindow") == "window"
    assert atlas.role_of("Zzz") == "other"


def run(*argv: str) -> str:
    return fw.run_line(fw.Session(), list(argv), "dt2-1.17", 60)


@need_cf
def test_fn_and_callers():
    out = run("fn", "0x401be1be")
    assert out.startswith("401be1be FUN_401be1be")
    assert "callees:" in out
    assert "error" not in run("callers", "0x40111924")


@need_cf
def test_unknown_function_is_one_line_error():
    assert run("fn", "NoSuchThing_zzz").startswith("error: no function matches")


@need_cf
def test_class_and_str():
    out = run("class", "SourcePageView")
    assert "vtable" in out and "SourcePageView::vfunc_4" in out
    assert "WRONG UI CARD" in run("str", "WRONG UI")


@need_cf
def test_search_and_sql_and_cap():
    out = run("search", "pattern", "--max", "8")
    assert "names:" in out and "more" in out
    assert run("sql", "select count(*) from functions").splitlines()[1].isdigit()
    assert run("sql", "delete from functions").startswith("error: sql is read-only")


@need_cf
def test_path_and_port():
    assert "hops" in run("path", "0x400caff0", "0x40001928")
    assert "401be1be" in run(
        "port", "0x401bdee2", "--from", "dt2-1.16", "--to", "dt2-1.17"
    )


@need_cf
def test_batch_blocks(tmp_path):
    q = tmp_path / "q.txt"
    q.write_text("# c\nfn 0x401be1be --max 3\nclass Pattern --max 2\nbogus\n")
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.coldfire.fw",
            "batch",
            str(q),
            "-i",
            "dt2-1.17",
        ],
        env=python_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert heads == ["## fn 0x401be1be --max 3", "## class Pattern --max 2", "## bogus"]
    assert "error: bad arguments" in out


@need_sharc
def test_sharc_fn_and_callers():
    assert run("fn", "0xb80105").startswith("b80105 FUN_b80105")
    assert "call sites" in run("callers", "0x1c0ccf")
    assert run("decomp", "0xb80105").startswith("error: no decompiler")


@need_cf
@pytest.mark.skipif(
    not os.path.exists(SECTION), reason="no MAIN OS section for dt2-1.17"
)
def test_atlas_build_and_query(tmp_path):
    counts = atlas.build("dt2-1.17", str(tmp_path))
    assert (
        counts["machines"] >= 5 and counts["params"] > 200 and counts["classes"] > 100
    )
    import sqlite3

    con = sqlite3.connect(str(tmp_path / "dt2-1.17.sqlite"))
    names = {r[0] for r in con.execute("select name1 from machines")}
    assert "SAMPLE" in names
    assert (
        con.execute("select count(*) from glossary where term='machine'").fetchone()[0]
        > 5
    )
