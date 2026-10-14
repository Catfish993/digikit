"""Tests for tools/devtools/relocate.py (tiny git repositories, no firmware)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support.git_repo import make_repo

from tools.devtools import relocate

AB_MOVES = {"tools/a.py": "tools/emu/a.py", "tools/b.py": "tools/emu/b.py"}
AB_FILES = {"tools/a.py": "VALUE = 1\n", "tools/b.py": "VALUE = 2\n"}


def run(
    root: Path,
    moves: dict[str, str],
    *,
    apply: bool = False,
    packages: dict[str, str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    spec = {"moves": moves, "packages": packages or {}, "exclude_text": exclude or []}
    map_path = root.parent / "map.json"
    map_path.write_text(json.dumps(spec))
    report = root.parent / "report.json"
    report.unlink(missing_ok=True)
    args = [str(map_path), "--root", str(root), "--report", str(report)]
    if apply:
        args.append("--apply")
    code = relocate.main(args)
    return code, json.loads(report.read_text()) if report.exists() else {}


def lines(root: Path, rel: str) -> list[str]:
    return (root / rel).read_text().splitlines()


def found(data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [r for r in data["reports"] if r["category"] == category]


def kinds(data: dict[str, Any]) -> set[str]:
    return {e["kind"] for e in data["edits"]}


IMPORT_SRC = """\
import sys
import a
import b as bb
from a import VALUE
from tools import a as aa, b
import os, a, json
import a.sub
import tools.b
from tools import unknown
x = 1; import b
"""


def test_imports_i1_to_i5(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_x.py": IMPORT_SRC})
    packages = {"tools/emu": "Emulator."}
    code, data = run(root, AB_MOVES, packages=packages)
    assert code == 0
    assert (root / "tests/test_x.py").read_text() == IMPORT_SRC
    assert not (root / "tools/emu").exists()
    assert kinds(data) == {"I1", "I2", "I3", "I4"}
    assert [r["line"] for r in found(data, "dotted-import")] == [7, 8]
    assert [r["text"] for r in found(data, "from-tools-unknown")] == ["unknown"]
    code, _ = run(root, AB_MOVES, apply=True, packages=packages)
    assert code == 0
    assert lines(root, "tests/test_x.py") == [
        "import sys",
        "from tools.emu import a",
        "from tools.emu import b as bb",
        "from tools.emu.a import VALUE",
        "from tools.emu import a as aa",
        "from tools.emu import b",
        "import os",
        "from tools.emu import a",
        "import json",
        "import a.sub",
        "import tools.b",
        "from tools import unknown",
        "x = 1; from tools.emu import b",
    ]
    assert (root / "tools/emu/a.py").read_text() == "VALUE = 1\n"
    assert not (root / "tools/a.py").exists()
    assert (root / "tools/emu/__init__.py").read_text() == '"""Emulator."""\n'


def test_split_on_shared_line_is_reported(tmp_path: Path) -> None:
    src = "import os\nif os: import os, a\nif os: import a\n"
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_y.py": src})
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_y.py") == [
        "import os",
        "if os: import os, a",
        "if os: from tools.emu import a",
    ]
    assert [r["line"] for r in found(data, "import-shared-line")] == [2]


STRING_SRC = """\
import importlib
import sys
from unittest import mock

mod = importlib.import_module("a")
with mock.patch("a.helper.run"):
    pass
has_b = "b" in sys.modules
name = "a.thing"
script = "a.py"
"""


def test_string_literals(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_s.py": STRING_SRC})
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_s.py")[4:] == [
        'mod = importlib.import_module("tools.emu.a")',
        'with mock.patch("tools.emu.a.helper.run"):',
        "    pass",
        'has_b = "tools.emu.b" in sys.modules',
        'name = "a.thing"',
        'script = "a.py"',
    ]
    assert kinds(data) == {"string"}
    assert [r["text"] for r in found(data, "dotted-literal")] == ["a.thing"]
    assert [r["text"] for r in found(data, "basename-literal")] == ["a.py"]


def test_sys_path_removal(tmp_path: Path) -> None:
    guard = """\
import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
print(sys.argv)
"""
    orphan = """\
import sys
from pathlib import Path

# make tools importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

X = 1
"""
    kept = """\
import sys

sys.path.insert(0, "plugins")


def f():
    sys.path.insert(0, ROOT)
"""
    files = {
        **AB_FILES,
        "tests/test_g.py": guard,
        "tests/test_p.py": orphan,
        "tests/test_q.py": kept,
    }
    root = make_repo(tmp_path, files)
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_g.py") == [
        "import sys",
        "from pathlib import Path",
        "",
        "ROOT = Path(__file__).parent",
        "print(sys.argv)",
    ]
    assert lines(root, "tests/test_p.py") == [
        "from pathlib import Path",
        "",
        "# make tools importable",
        "",
        "X = 1",
    ]
    assert (root / "tests/test_q.py").read_text() == kept
    assert {"sys-path", "unused-import"} <= kinds(data)
    assert [r["file"] for r in found(data, "orphan-comment")] == ["tests/test_p.py"]
    assert [r["line"] for r in found(data, "sys-path-kept")] == [3]
    assert [r["line"] for r in found(data, "sys-path-structure")] == [7]


def test_anchor_depth(tmp_path: Path) -> None:
    top = """\
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
UP = Path(__file__).resolve().parent.parent
HERE = os.path.dirname(os.path.abspath(__file__))
HUP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = os.path.dirname(HERE)
"""
    sub = """\
from pathlib import Path

OWN = Path(__file__).parent
ROOT = Path(__file__).resolve().parents[2]
"""
    stay = "from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n"
    files = {"tools/x.py": top, "tools/sub/y.py": sub, "tests/t.py": stay}
    moves = {"tools/x.py": "tools/emu/x.py", "tools/sub/y.py": "tools/sub/deep/y.py"}
    root = make_repo(tmp_path, files)
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert lines(root, "tools/emu/x.py")[3:] == [
        "ROOT = Path(__file__).resolve().parents[2]",
        "TOOLS = Path(__file__).resolve().parent",
        "UP = Path(__file__).resolve().parent.parent.parent",
        "HERE = os.path.dirname(os.path.abspath(__file__))",
        "HUP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
        "TOP = os.path.dirname(os.path.dirname(HERE))",
    ]
    assert lines(root, "tools/sub/deep/y.py")[2:] == [
        "OWN = Path(__file__).parent",
        "ROOT = Path(__file__).resolve().parents[3]",
    ]
    assert (root / "tests/t.py").read_text() == stay
    assert kinds(data) == {"anchor-depth"}
    assert len(data["edits"]) == 5


def test_paired_cleanup_try_finally_unwraps(tmp_path: Path) -> None:
    src = """\
import sys
from pathlib import Path

ROOT = Path(__file__).parent
try:
    # make tools importable
    sys.path.insert(0, str(ROOT))
    import json

    print(json.dumps({}))
finally:
    sys.path.pop(0)
print("done")
"""
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_t.py": src})
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_t.py") == [
        "from pathlib import Path",
        "",
        "ROOT = Path(__file__).parent",
        "# make tools importable",
        "import json",
        "",
        "print(json.dumps({}))",
        'print("done")',
    ]
    assert {"sys-path", "unused-import"} <= kinds(data)
    assert found(data, "sys-path-structure") == []


def test_paired_cleanup_keeps_except(tmp_path: Path) -> None:
    src = """\
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
try:
    import json
except ImportError:
    json = None
finally:
    sys.path.pop(0)
"""
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_e.py": src})
    code, _ = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_e.py") == [
        "from pathlib import Path",
        "",
        "ROOT = Path(__file__).parent",
        "try:",
        "    import json",
        "except ImportError:",
        "    json = None",
    ]


def test_paired_cleanup_refuses_multiline_string(tmp_path: Path) -> None:
    src = '''\
import sys
from pathlib import Path

ROOT = Path(__file__).parent
try:
    sys.path.insert(0, str(ROOT))
    TEXT = """a
    b"""
finally:
    sys.path.pop(0)
'''
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_m.py": src})
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert (root / "tests/test_m.py").read_text() == src
    assert [r["line"] for r in found(data, "sys-path-structure")] == [5]
    assert kinds(data) == set()


def test_pop_is_paired_per_scope(tmp_path: Path) -> None:
    src = """\
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def paired():
    sys.path.insert(0, str(ROOT))
    value = 1
    sys.path.pop(0)
    return value


def alone():
    sys.path.pop(0)
    return 2
"""
    root = make_repo(tmp_path, {**AB_FILES, "tests/test_s.py": src})
    code, data = run(root, AB_MOVES, apply=True)
    assert code == 0
    assert lines(root, "tests/test_s.py") == [
        "import sys",
        "from pathlib import Path",
        "",
        "ROOT = Path(__file__).parent",
        "",
        "",
        "def paired():",
        "    value = 1",
        "    return value",
        "",
        "",
        "def alone():",
        "    sys.path.pop(0)",
        "    return 2",
    ]
    assert [r["line"] for r in found(data, "sys-path-kept")] == [15]


def test_derived_anchor_path_style(tmp_path: Path) -> None:
    src = """\
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UP = HERE.parents[0]
DOCS = ROOT / "docs"
REL = Path("a").relative_to(HERE.parent)
"""
    root = make_repo(tmp_path, {"tools/x.py": src})
    code, data = run(root, {"tools/x.py": "tools/emu/x.py"}, apply=True)
    assert code == 0
    assert lines(root, "tools/emu/x.py")[2:] == [
        "HERE = Path(__file__).resolve().parent",
        "ROOT = HERE.parent.parent",
        "UP = HERE.parents[1]",
        'DOCS = ROOT / "docs"',
        'REL = Path("a").relative_to(HERE.parent.parent)',
    ]
    assert kinds(data) == {"anchor-depth"}
    assert len(data["edits"]) == 3


def test_derived_anchor_os_style_and_rewritten_base(tmp_path: Path) -> None:
    os_src = """\
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
"""
    path_src = """\
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT.parent
DOCS = ROOT / "docs"
"""
    files = {"tools/y.py": os_src, "tools/z.py": path_src}
    moves = {"tools/y.py": "tools/emu/y.py", "tools/z.py": "tools/emu/z.py"}
    root = make_repo(tmp_path, files)
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert lines(root, "tools/emu/y.py")[2:] == [
        "HERE = os.path.dirname(os.path.abspath(__file__))",
        "TOP = os.path.dirname(os.path.dirname(HERE))",
    ]
    assert lines(root, "tools/emu/z.py")[2:] == [
        "ROOT = Path(__file__).resolve().parents[2]",
        "UP = ROOT.parent",
        'DOCS = ROOT / "docs"',
    ]
    assert len(data["edits"]) == 2


def test_join_literals_are_fixed(tmp_path: Path) -> None:
    src = """\
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / "tools" / "fw.py"
"""
    files = {"tools/fw.py": "X = 1\n", "tests/test_f.py": src}
    root = make_repo(tmp_path, files)
    code, data = run(root, {"tools/fw.py": "tools/coldfire/fw.py"}, apply=True)
    assert code == 0
    assert lines(root, "tests/test_f.py")[3] == (
        'FW = ROOT / "tools" / "coldfire" / "fw.py"'
    )
    assert kinds(data) == {"join"}
    assert found(data, "join-mismatch") == []


def test_join_literals_with_support_paths_root(tmp_path: Path) -> None:
    src = 'from support.paths import TOOLS\n\nLOCK = TOOLS / "benchlock.py"\n'
    files = {"tools/benchlock.py": "X = 1\n", "tests/test_l.py": src}
    root = make_repo(tmp_path, files)
    moves = {"tools/benchlock.py": "tools/devtools/benchlock.py"}
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert lines(root, "tests/test_l.py")[2] == (
        'LOCK = TOOLS / "devtools" / "benchlock.py"'
    )
    assert found(data, "join-mismatch") == []


def test_join_moving_together_needs_no_edit(tmp_path: Path) -> None:
    src = """\
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "sharcspec/decode_table.json")
"""
    files = {"tools/x.py": src, "tools/sharcspec/decode_table.json": "{}\n"}
    moves = {
        "tools/x.py": "tools/emu/x.py",
        "tools/sharcspec/decode_table.json": "tools/emu/sharcspec/decode_table.json",
    }
    root = make_repo(tmp_path, files)
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert (root / "tools/emu/x.py").read_text() == src
    assert data["edits"] == []
    assert found(data, "join-mismatch") == []


def test_join_into_sibling_owner_stays_a_report(tmp_path: Path) -> None:
    src = """\
from pathlib import Path

HERE = Path(__file__).resolve().parent
NE = HERE / "native_emu.py"
"""
    files = {"tools/x.py": src, "tools/native_emu.py": "X = 1\n"}
    moves = {
        "tools/x.py": "tools/emu/x.py",
        "tools/native_emu.py": "tools/native/native_emu.py",
    }
    root = make_repo(tmp_path, files)
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert (root / "tools/emu/x.py").read_text() == src
    (hit,) = found(data, "join-mismatch")
    assert hit["line"] == 4
    assert "moved to tools/native/native_emu.py" in hit["text"]
    assert "join" not in kinds(data)


def test_dropped_edit_is_reported(tmp_path: Path) -> None:
    src = 'import sys\nsys.path.insert(0, "tools/x.py")\nX = 1\n'
    root = make_repo(tmp_path, {"tools/x.py": "X = 1\n", "tests/test_d.py": src})
    code, data = run(root, {"tools/x.py": "tools/emu/x.py"}, apply=True)
    assert code == 0
    assert "tools/x.py" not in (root / "tests/test_d.py").read_text()
    (hit,) = found(data, "edit-dropped")
    assert hit["file"] == "tests/test_d.py"
    assert hit["line"] == 2
    assert hit["text"] == "T2: tools/x.py"


def test_join_mismatch(tmp_path: Path) -> None:
    src = """\
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "x.py"
FINE = HERE / "docs" / "x.py"
"""
    files = {"tools/x.py": "X = 1\n", "tools/y.py": src}
    moves = {"tools/x.py": "tools/other/x.py", "tools/y.py": "tools/emu/y.py"}
    root = make_repo(tmp_path, files)
    code, data = run(root, moves)
    assert code == 0
    (hit,) = found(data, "join-mismatch")
    assert hit["file"] == "tools/emu/y.py"
    assert hit["line"] == 4
    assert "old target tools/x.py, moved to tools/other/x.py" in hit["text"]
    assert hit["text"].endswith("now yields tools/emu/x.py")
    assert found(data, "basename-literal") == []


GUIDE = """\
Run `python tools/x.py --flag` or `uv run python -u tools/x.py`.
See tools/x.py, tools/unit/ and tools/unit/mod.py; globs tools/g_*.py.
Old: tools/gone.py and tools.x and tools/ here.
python ../tools/x.py
"""

TEXT_MOVES = {
    "tools/x.py": "tools/emu/x.py",
    "tools/unit/mod.py": "tools/emu/unit/mod.py",
    "tools/unit/other.py": "tools/emu/unit/other.py",
    "tools/g_a.py": "tools/grp/g_a.py",
    "tools/g_b.py": "tools/grp/g_b.py",
    "tools/h_a.py": "tools/grp/h_a.py",
    "tools/h_b.py": "tools/grp2/h_b.py",
}


def test_text_rewrites(tmp_path: Path) -> None:
    readme = "tools/gone.py and tools/g_ and tools/h_*.py\n"
    old_note = "python tools/x.py and tools/x.py\n"
    files = {rel: "X = 1\n" for rel in TEXT_MOVES}
    files["docs/guide.md"] = GUIDE
    files["docs/handovers/old.md"] = old_note
    files["README.md"] = readme
    root = make_repo(tmp_path, files)
    code, data = run(root, TEXT_MOVES, apply=True, exclude=["docs/handovers/"])
    assert code == 0
    assert lines(root, "docs/guide.md") == [
        "Run `python -m tools.emu.x --flag` or `uv run python -u -m tools.emu.x`.",
        "See tools/emu/x.py, tools/emu/unit/ and tools/emu/unit/mod.py;"
        " globs tools/grp/g_*.py.",
        "Old: tools/gone.py and tools.emu.x and tools/ here.",
        "python ../tools/emu/x.py",
    ]
    assert (root / "docs/handovers/old.md").read_text() == old_note
    assert (root / "README.md").read_text() == readme
    assert {"T1", "T2", "T3"} <= kinds(data)
    assert [r["line"] for r in found(data, "cmd-relative")] == [4]
    assert [r["line"] for r in found(data, "bare-tools")] == [3]
    assert [r["file"] for r in found(data, "stale-ref")] == ["README.md"]
    assert [r["file"] for r in found(data, "prefix-ref")] == ["README.md"]
    assert [r["file"] for r in found(data, "glob-ambiguous")] == ["README.md"]


def test_command_and_path_in_python_source(tmp_path: Path) -> None:
    src = '# run: python tools/x.py\nCMD = "python tools/x.py"\nPATH = "tools/x.py"\n'
    root = make_repo(tmp_path, {"tools/x.py": "X = 1\n", "tests/test_c.py": src})
    code, data = run(root, {"tools/x.py": "tools/emu/x.py"}, apply=True)
    assert code == 0
    assert lines(root, "tests/test_c.py") == [
        "# run: python -m tools.emu.x",
        'CMD = "python -m tools.emu.x"',
        'PATH = "tools/emu/x.py"',
    ]
    assert [r["line"] for r in found(data, "cmd-in-py")] == [1, 2]
    assert [r["line"] for r in found(data, "py-path-literal")] == [2, 3]


def test_flat_resolution_and_stdlib_shadow(tmp_path: Path) -> None:
    user = 'import sys\n\nsys.path.insert(0, "tools/sharcspec")\nfrom spec import X\n'
    files = {
        "tools/sharcspec/spec.py": "X = 1\n",
        "tools/sharcspec/user.py": "import spec\n",
        "tools/queue.py": "Q = 1\n",
        "tests/test_r.py": user,
        "tests/test_q2.py": "import queue\n",
    }
    moves = {
        "tools/sharcspec/spec.py": "tools/sharc_dev/sharcspec/spec.py",
        "tools/sharcspec/user.py": "tools/sharc_dev/sharcspec/user.py",
        "tools/queue.py": "tools/emu/queue.py",
    }
    root = make_repo(tmp_path, files)
    code, data = run(root, moves, apply=True)
    assert code == 0
    assert (root / "tools/sharc_dev/sharcspec/user.py").read_text() == (
        "from tools.sharc_dev.sharcspec import spec\n"
    )
    assert (root / "tests/test_r.py").read_text() == (
        "\nfrom tools.sharc_dev.sharcspec.spec import X\n"
    )
    assert (root / "tests/test_q2.py").read_text() == "import queue\n"
    assert [r["file"] for r in found(data, "stdlib-shadow")] == ["tests/test_q2.py"]


def test_parse_check_blocks_everything(tmp_path: Path) -> None:
    files = {"tools/x.py": "X = 1\n", "tests/test_z.py": 'P = "tools/x.py"\n'}
    root = make_repo(tmp_path, files)
    code, _ = run(root, {"tools/x.py": 'tools/q"/x.py'}, apply=True)
    assert code == 1
    assert (root / "tests/test_z.py").read_text() == 'P = "tools/x.py"\n'
    assert (root / "tools/x.py").exists()


def test_move_checks(tmp_path: Path, capsys: Any) -> None:
    root = make_repo(tmp_path, {"tools/x.py": "X = 1\n", "tools/y.py": "Y = 1\n"})
    code, _ = run(root, {"tools/x.py": "tools/a/b/x.py"})
    assert code == 1
    assert "one directory deeper" in capsys.readouterr().err
    code, _ = run(root, {"tools/nope.py": "tools/e/nope.py"})
    assert code == 1
    assert "not a tracked file" in capsys.readouterr().err
    code, _ = run(root, {"tools/x.py": "tools/e/x.py", "tools/y.py": "tools/e/x.py"})
    assert code == 1
    assert "duplicate destination" in capsys.readouterr().err


def test_apply_needs_clean_tree(tmp_path: Path, capsys: Any) -> None:
    root = make_repo(tmp_path, {"tools/x.py": "X = 1\n"})
    (root / "tools/x.py").write_text("X = 2\n")
    moves = {"tools/x.py": "tools/e/x.py"}
    assert run(root, moves)[0] == 0
    assert run(root, moves, apply=True)[0] == 1
    assert "uncommitted" in capsys.readouterr().err
    assert (root / "tools/x.py").exists()
