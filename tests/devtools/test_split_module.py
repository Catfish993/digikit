"""Tests for tools/devtools/split_module.py (tiny git repositories, no firmware)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from support.git_repo import git, make_repo

from tools.devtools import split_module

SOURCE = '''\
"""Sample module."""

from __future__ import annotations

import json, os
import sys
import textwrap

from .sibling import helper

# the answer
ANSWER = 42


def dump(value: int) -> str:
    return json.dumps({"v": helper(value)})


# scaled answer
def scaled(factor: int) -> int:
    return ANSWER * factor


class Box:
    def total(self) -> int:
        return scaled(2) + len(sys.argv) * 0


if __name__ == "__main__":
    print(dump(scaled(1)))
'''

FILES = {
    "pkg/__init__.py": "",
    "pkg/sibling.py": "def helper(x):\n    return x + 1\n",
    "pkg/sample.py": SOURCE,
}

MODULES = {
    "consts": {"doc": "Constants.", "take": ["ANSWER"]},
    "funcs": {"doc": "Functions.", "take": ["dump", "scaled"]},
    "box": {"doc": "Box.", "take": ["Box"]},
}

CONSTS = '''\
"""Constants."""

from __future__ import annotations

# the answer
ANSWER = 42
'''

FUNCS = '''\
"""Functions."""

from __future__ import annotations

import json
from ..sibling import helper

from .consts import ANSWER


def dump(value: int) -> str:
    return json.dumps({"v": helper(value)})


# scaled answer
def scaled(factor: int) -> int:
    return ANSWER * factor
'''

BOX = '''\
"""Box."""

from __future__ import annotations

import sys

from .funcs import scaled


class Box:
    def total(self) -> int:
        return scaled(2) + len(sys.argv) * 0
'''


def run(
    root: Path,
    modules: dict[str, Any],
    *,
    apply: bool = False,
    source: str = "pkg/sample.py",
    **extra: Any,
) -> tuple[int, dict[str, Any]]:
    plan = {"source": source, "modules": modules, **extra}
    plan_path = root.parent / "plan.json"
    plan_path.write_text(json.dumps(plan))
    report = root.parent / "report.json"
    report.unlink(missing_ok=True)
    args = [str(plan_path), "--root", str(root), "--report", str(report)]
    if apply:
        args.append("--apply")
    code = split_module.main(args)
    return code, json.loads(report.read_text()) if report.exists() else {}


def found(data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [r for r in data["reports"] if r["category"] == category]


def python(root: Path, *args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-B", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, data = run(root, MODULES)
    assert code == 0
    assert (root / "pkg/sample.py").read_text() == SOURCE
    assert not (root / "pkg/sample").exists()
    assert sorted(data["modules"]) == ["box", "consts", "funcs"]
    assert data["modules"]["consts"]["statements"] == 1
    assert data["modules"]["funcs"]["lines"] == FUNCS.count("\n")
    assert [r["text"] for r in found(data, "import-dropped")] == ["import textwrap"]


def test_apply_moves_statements_and_writes_imports(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, _ = run(root, MODULES, apply=True)
    assert code == 0
    assert not (root / "pkg/sample.py").exists()
    assert (root / "pkg/sample/consts.py").read_text() == CONSTS
    assert (root / "pkg/sample/funcs.py").read_text() == FUNCS
    assert (root / "pkg/sample/box.py").read_text() == BOX
    assert (root / "pkg/sample/__main__.py").read_text() == (
        "from .funcs import dump, scaled\n\nprint(dump(scaled(1)))\n"
    )
    tracked = git(root, "ls-files").split()
    assert "pkg/sample.py" not in tracked
    assert "pkg/sample/funcs.py" in tracked
    assert "pkg/sample/__init__.py" in tracked
    assert (root / "pkg/sample/__init__.py").read_text() == (
        '"""Sample module."""\n\nfrom __future__ import annotations\n'
    )


def test_package_behaves_like_the_original(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    code, _ = run(root, MODULES, apply=True, facade_extra=["dump", "scaled"])
    assert code == 0
    call = "import pkg.sample as s; print(s.dump(s.scaled(1)))"
    assert python(root, "-c", call) == '{"v": 43}\n'
    assert python(root, "-m", "pkg.sample") == '{"v": 43}\n'


def test_facade_exports_names_used_by_importers(tmp_path: Path) -> None:
    use = "from pkg.sample import dump\nimport pkg.sample as sample\n\n"
    use += "VALUE = sample.scaled(1)\n"
    root = make_repo(tmp_path, {**FILES, "tests/use.py": use})
    code, data = run(root, MODULES, apply=True)
    assert code == 0
    assert data["exports"] == ["dump", "scaled"]
    assert (root / "pkg/sample/__init__.py").read_text() == (
        '"""Sample module."""\n\nfrom __future__ import annotations\n\n'
        "from .funcs import dump, scaled\n\n"
        '__all__ = [\n    "dump",\n    "scaled",\n]\n'
    )
    star = "from pkg.sample import *\nprint(sorted(n for n in dir() if n[0] != '_'))"
    assert python(root, "-c", star) == "['dump', 'scaled']\n"


def test_outside_mutation_is_reported(tmp_path: Path) -> None:
    test = "import pkg.sample as sample\n\n\n"
    test += "def test_it(monkeypatch):\n"
    test += '    monkeypatch.setattr(sample, "ANSWER", 1)\n'
    root = make_repo(tmp_path, {**FILES, "tests/test_patch.py": test})
    code, data = run(root, MODULES)
    assert code == 0
    (hit,) = found(data, "outside-mutation")
    assert hit["file"] == "tests/test_patch.py"
    assert hit["line"] == 5
    assert hit["text"] == "ANSWER (defined in consts)"
    assert data["exports"] == ["ANSWER"]
    assert found(data, "unresolved-use") == []


def test_module_object_passed_on_is_unresolved(tmp_path: Path) -> None:
    use = "import pkg.sample as m\n\nregister(m)\n"
    root = make_repo(tmp_path, {**FILES, "tests/use.py": use})
    code, data = run(root, MODULES)
    assert code == 0
    assert [r["line"] for r in found(data, "unresolved-use")] == [3]


def test_take_patterns_and_line_ranges(tmp_path: Path) -> None:
    src = '"""Doc."""\n\nS_ONE = 1\nS_TWO = 2\n\n\ndef f() -> int:\n    return S_ONE\n'
    modules = {
        "consts": {"doc": "Constants.", "take": ["S_*"]},
        "funcs": {"doc": "Functions.", "take": ["@L7-L8"]},
    }
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": src})
    code, data = run(root, modules, source="pkg/m.py", apply=True)
    assert code == 0
    assert data["modules"]["consts"]["statements"] == 2
    assert (root / "pkg/m/funcs.py").read_text() == (
        '"""Functions."""\n\nfrom .consts import S_ONE\n\n\n'
        "def f() -> int:\n    return S_ONE\n"
    )


CYCLE = '''\
"""Cycle."""

from __future__ import annotations


class A:
    def partner(self, other: B) -> B:
        return other


class B:
    def make(self, other: A) -> A:
        return other
'''


def test_type_checking_cycle_is_resolved(tmp_path: Path) -> None:
    modules = {
        "a": {"doc": "A.", "take": ["A"]},
        "b": {"doc": "B.", "take": ["B"]},
    }
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/cyc.py": CYCLE})
    code, _ = run(
        root, modules, source="pkg/cyc.py", apply=True, facade_extra=["A", "B"]
    )
    assert code == 0
    text = (root / "pkg/cyc/a.py").read_text()
    assert "from typing import TYPE_CHECKING\n" in text
    assert "if TYPE_CHECKING:\n    from .b import B\n" in text
    call = "import pkg.cyc as c; print(type(c.A().partner(c.B())).__name__)"
    assert python(root, "-c", call) == "B\n"


def test_runtime_cycle_fails(tmp_path: Path, capsys: Any) -> None:
    src = '"""Cycle."""\n\n\ndef one():\n    return two()\n\n\ndef two():\n    return one()\n'
    modules = {
        "a": {"doc": "A.", "take": ["one"]},
        "b": {"doc": "B.", "take": ["two"]},
    }
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/cyc.py": src})
    code, data = run(root, modules, source="pkg/cyc.py", apply=True)
    assert code == 1
    assert len(found(data, "import-cycle")) == 1
    assert "import-cycle" in capsys.readouterr().err
    assert (root / "pkg/cyc.py").exists()
    assert not (root / "pkg/cyc").exists()


def test_unassigned_statement_fails(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    modules = {k: v for k, v in MODULES.items() if k != "box"}
    code, data = run(root, modules, apply=True)
    assert code == 1
    (hit,) = found(data, "unassigned")
    assert hit["line"] == SOURCE.splitlines().index("class Box:") + 1
    assert (root / "pkg/sample.py").exists()


def test_global_cross_fails(tmp_path: Path) -> None:
    src = '"""G."""\n\nCOUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n'
    modules = {
        "consts": {"doc": "Constants.", "take": ["COUNT"]},
        "funcs": {"doc": "Functions.", "take": ["bump"]},
    }
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/g.py": src})
    code, data = run(root, modules, source="pkg/g.py")
    assert code == 1
    (hit,) = found(data, "global-cross")
    assert hit["line"] == 7


def test_undefined_name_fails(tmp_path: Path) -> None:
    src = '"""U."""\n\n\ndef f():\n    return missing\n'
    modules = {"funcs": {"doc": "Functions.", "take": ["f"]}}
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/u.py": src})
    code, data = run(root, modules, source="pkg/u.py")
    assert code == 1
    (hit,) = found(data, "undefined-name")
    assert hit["line"] == 5
    assert hit["text"] == "funcs: missing"


def test_main_module_needs_a_main_block(tmp_path: Path) -> None:
    modules = {"a": {"doc": "A.", "take": ["A"]}, "b": {"doc": "B.", "take": ["B"]}}
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/cyc.py": CYCLE})
    code, data = run(root, modules, source="pkg/cyc.py", main_module="a")
    assert code == 1
    assert len(found(data, "no-main-block")) == 1


def test_hazards_in_moved_code(tmp_path: Path) -> None:
    src = '"""H."""\n\nimport logging\n\nHERE = __file__\nLOG = logging.getLogger("pkg.h.sub")\n'
    modules = {"consts": {"doc": "Constants.", "take": ["HERE", "LOG"]}}
    root = make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/h.py": src})
    code, data = run(root, modules, source="pkg/h.py")
    assert code == 0
    (anchor,) = found(data, "file-anchor")
    assert anchor["file"] == "pkg/h/consts.py"
    assert anchor["text"] == "HERE = __file__"
    (named,) = found(data, "module-name")
    assert named["text"] == 'LOG = logging.getLogger("pkg.h.sub")'


def test_apply_needs_clean_tree(tmp_path: Path, capsys: Any) -> None:
    root = make_repo(tmp_path, FILES)
    (root / "pkg/sibling.py").write_text("def helper(x):\n    return x + 2\n")
    assert run(root, MODULES)[0] == 0
    assert run(root, MODULES, apply=True)[0] == 1
    assert "dirty-tree" in capsys.readouterr().err
    assert (root / "pkg/sample.py").exists()


def test_usage_errors(tmp_path: Path, capsys: Any) -> None:
    root = make_repo(tmp_path, FILES)
    plan_path = tmp_path / "bad.json"
    plan_path.write_text(json.dumps({"source": "pkg/sample.py", "modules": {}}))
    assert split_module.main([str(plan_path), "--root", str(root)]) == 2
    assert "cannot read the plan" in capsys.readouterr().err
