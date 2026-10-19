"""Finer routing for tools/devtools/verify.py: which steps and test files one changed path needs.

verify.py asks `fine(path)` first. A path this module knows gets its own steps and its own test
files; any other path falls through to the prefix rules of verify.py (RULES, PY_SUITES). The
functions read the working tree (module manifests, test files, scenario TOMLs); only
`mise_paths` takes texts, so the mise.toml routing can be tested without git.

Rules (docs/TESTING.md "What verify runs"):

- a file of a module under modules/: python-tests and rust-tests; web only for the files the web
  gallery bundle reads (module.toml, build/, gallery/, registry tables, not src/, sdk/, tests/).
  Framework modules (FRAMEWORK: every patched image includes them) select all of tests/patchkit
  and tests/digikit. Any other module selects the test files that name it or a module that
  requires it (transitively) and the digikit test files of the scenarios that name it. No PCM step.
- a machine module (a DSP routine behind a `[[voice]]` or a voices `[[entry]]`, tools/patchkit/pk_conform.py), a module that one
  depends on or builds from, and core/dsp-voice (the wrapper every routine runs through) also select the machine conformance
  suite, CONFORMANCE (docs/TESTING.md "Machine conformance"). Its cases for every machine run in one file.
- a module of tools/device: the unit tests that import it (and the importers of those) plus the
  digikit test files of the scenario kinds whose runners call it (DEVICE_KINDS).
- a scenario file of tests/digikit/scenarios: the test files that run the scenario's kind.
- `tests/digikit/test_*.py`, `tests/patchkit/test_*.py`: the file itself.
- `mise.toml`: the tasks that changed, routed like the tool their `run` names (`mise_paths`).
"""

from __future__ import annotations

import ast
import re
import shlex
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.patchkit import pk_conform  # noqa: E402

# Modules that every patched image or every runtime build includes: a change selects the whole
# patchkit and digikit suites.
FRAMEWORK = frozenset(
    {
        "core/runtime",
        "core/dsp-voice",
        "core/dsp-link",
        "core/machines",
        "core/params",
        "core/dynreg",
        "core/dyndsp",
        "core/digikit-menu",
        "core/health",
        "core/expansion",
    }
)
# tools/device module -> the digikit scenario kinds whose runners (tools/patchkit/pk_digikit_*.py)
# call it. A module of tools/device that is not listed here fails tests/devtools/test_verify.py.
DEVICE_KINDS: dict[str, tuple[str, ...]] = {
    "__init__": (),
    "dkupload": ("upload", "menu", "debug", "crash", "card"),
    "dkrun": ("menu",),
    "dkscreen": ("upload",),
    "dklog": ("debug", "crash", "card"),
    "devrpc": ("upload",),
    "midisniff": ("upload", "menu", "debug", "crash", "card"),
    "mmon": (),
}
BROAD_TASK_GROUPS = ("verify", "build", "gen", "setup")
ALL_SUITES = ("tests/patchkit", "tests/digikit")
CONFORMANCE = "tests/modules/test_machine_conformance.py"
CONFORMANCE_WRAPPERS = frozenset({"core/dsp-voice"})
# a path built from parts: ROOT / "modules" / "machines" / "pluck"
SPLIT_ID_RE = re.compile(
    r"""["'](core|fx|machines|test)["']\s*[/,]\s*["']([a-z0-9][a-z0-9-]*)["']"""
)
ID_RE = re.compile(r"\b(?:core|fx|machines|test)/[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class Fine:
    """The steps and pytest paths (files or directories) one path needs."""

    label: str
    steps: tuple[str, ...]
    tests: tuple[str, ...]


@cache
def manifests() -> dict[str, tuple[str, ...]]:
    """Module id (its directory under modules/) -> the modules it depends on: those it
    `requires` and those whose files its build sources name (`sources = ["../../x/y/..."]`)."""
    root = ROOT / "modules"
    out: dict[str, tuple[str, ...]] = {}
    for toml in sorted(root.glob("*/*/module.toml")):
        data = tomllib.loads(toml.read_text(encoding="utf-8"))
        mid = toml.parent.relative_to(root).as_posix()
        deps = list(data.get("module", {}).get("requires", []))
        for table in data.get("object", []):
            for source in table.get("sources", []):
                target = (toml.parent / source).resolve()
                if not target.is_relative_to(root.resolve()):
                    continue
                parts = target.relative_to(root.resolve()).parts
                if len(parts) >= 2 and "/".join(parts[:2]) != mid:
                    deps.append("/".join(parts[:2]))
        out[mid] = tuple(dict.fromkeys(deps))
    return out


def dependents(ids: set[str]) -> set[str]:
    """IDS and every module that depends on one of them (manifests), directly or not."""
    mods = manifests()
    found = set(ids)
    grew = True
    while grew:
        grew = False
        for mid, requires in mods.items():
            if mid not in found and found.intersection(requires):
                found.add(mid)
                grew = True
    return found


def refs(text: str) -> set[str]:
    """Module ids named in TEXT (comment lines of a TOML file are not read)."""
    lines = (line for line in text.splitlines() if not line.lstrip().startswith("#"))
    body = "\n".join(lines)
    found = set(ID_RE.findall(body)) | {
        f"{a}/{b}" for a, b in SPLIT_ID_RE.findall(body)
    }
    return {m for m in found if m in manifests()}


# The scenario runners (tools/patchkit/pk_digikit_*.py) name modules in code, not only in the scenario
# TOMLs (test/dyn-dsp-hang in the debug runner). A module a runner names selects the kinds the runner
# serves; a runner not listed here serves every kind.
RUNNER_KINDS: dict[str, tuple[str, ...]] = {
    "pk_digikit_debug.py": ("card", "debug", "crash"),
    "pk_digikit_upload.py": ("upload",),
    "pk_digikit_rungc.py": ("rungc",),
    "pk_digikit_pages.py": ("pages",),
}


def code_refs(text: str) -> set[str]:
    """Module ids in the string literals of Python TEXT, docstrings left out (examples in a
    docstring name modules the code never builds)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return refs(text)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            found |= refs(node.value)
    return found


@cache
def pk_refs(name: str) -> frozenset[str]:
    """Module ids named in tools/patchkit/NAME.py (empty when there is no such file)."""
    path = ROOT / "tools/patchkit" / f"{name}.py"
    return (
        frozenset(refs(path.read_text(encoding="utf-8")))
        if path.is_file()
        else frozenset()
    )


def runner_kinds(hit: set[str]) -> set[str]:
    """The scenario kinds whose runner code names a module of HIT."""
    kinds: set[str] = set()
    for path in sorted((ROOT / "tools/patchkit").glob("pk_digikit_*.py")):
        if pk_refs(path.stem) & hit:
            kinds |= set(
                RUNNER_KINDS.get(path.name, tuple(kind_files()) + ("pages", "rungc"))
            )
    return kinds


@cache
def test_files() -> tuple[str, ...]:
    """Every tests/**/test_*.py, repo-relative (node_modules skipped)."""
    return tuple(
        rel
        for rel in (
            p.relative_to(ROOT).as_posix()
            for p in sorted((ROOT / "tests").rglob("test_*.py"))
        )
        if "node_modules" not in rel
    )


@cache
def test_refs() -> dict[str, frozenset[str]]:
    """Pytest file -> the module ids it names."""
    out: dict[str, frozenset[str]] = {}
    for rel in test_files():
        if rel.startswith("tests/devtools/"):  # the routing tests name modules as data
            continue
        text = (ROOT / rel).read_text(encoding="utf-8")
        found = refs(text).union(
            *(pk_refs(n) for n in set(re.findall(r"\bpk_[a-z0-9_]+\b", text)))
        )
        if found:
            out[rel] = frozenset(found)
    return out


@cache
def kind_files() -> dict[str, tuple[str, ...]]:
    """Scenario kind -> the tests/digikit files that run it (their `cases((kinds), device)` call)."""
    out: dict[str, list[str]] = {}
    for path in sorted((ROOT / "tests/digikit").glob("test_*.py")):
        match = re.search(r"cases\(\(([^)]*)\)", path.read_text(encoding="utf-8"))
        if not match:
            continue
        for kind in re.findall(r'"([a-z]+)"', match.group(1)):
            out.setdefault(kind, []).append(path.relative_to(ROOT).as_posix())
    return {kind: tuple(files) for kind, files in out.items()}


@cache
def scenario_refs() -> dict[str, tuple[str, frozenset[str]]]:
    """Scenario file -> (kind, module ids it names)."""
    out: dict[str, tuple[str, frozenset[str]]] = {}
    for path in sorted((ROOT / "tests/digikit/scenarios").glob("*.toml")):
        text = path.read_text(encoding="utf-8")
        kind = tomllib.loads(text).get("scenario", {}).get("kind", "")
        out[path.relative_to(ROOT).as_posix()] = (kind, frozenset(refs(text)))
    return out


def scenario_tests(kinds: set[str]) -> list[str]:
    """The digikit test files that run scenarios of KINDS, plus the well-formedness test."""
    files = {f for k in kinds for f in kind_files().get(k, ())}
    return ["tests/digikit/test_scenarios.py", *sorted(files)]


def module_tests(mid: str) -> list[str]:
    """Pytest paths for a change in module MID (see the module docstring)."""
    hit = dependents({mid})
    if hit & FRAMEWORK:
        return [*ALL_SUITES, *([CONFORMANCE] if mid in CONFORMANCE_WRAPPERS else [])]
    tests = [t for t, ids in test_refs().items() if ids & hit]
    if any(pk_conform.is_machine_module(m) for m in hit) and CONFORMANCE not in tests:
        tests.append(CONFORMANCE)
    kinds = {
        kind for kind, ids in scenario_refs().values() if ids & hit
    } | runner_kinds(hit)
    tests += [t for t in scenario_tests(kinds) if t not in tests]
    return tests


def web_reads(rel: str) -> bool:
    """Whether the web gallery bundle can read RELATIVE (to the module directory): the manifest,
    built objects, registry tables and the gallery files, not sources, the SDK, smoke tests."""
    top = rel.split("/", 1)[0]
    return top not in ("src", "sdk", "tests") and not rel.endswith(".md")


def module_fine(path: str) -> Fine | None:
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "modules":
        return None
    if path.endswith(".md"):
        return Fine("modules docs", (), ())
    mid = "/".join(parts[1:3])
    if len(parts) < 4 or mid not in manifests():
        # a file outside any module, or a module that is gone: stay broad
        return Fine(
            "modules (unknown module)",
            ("python-tests", "rust-tests", "web"),
            ALL_SUITES,
        )
    steps = ["python-tests", "rust-tests"]
    if web_reads("/".join(parts[3:])):
        steps.append("web")
    label = f"module {mid}" + (" (framework)" if mid in FRAMEWORK else "")
    return Fine(label, tuple(steps), tuple(module_tests(mid)))


def importers(stem: str) -> list[str]:
    """tools/device modules that import tools.device.STEM, and the tests that import it."""
    pat = re.compile(
        rf"^\s*(?:from\s+tools\.device\s+import\s+[^\n]*\b{stem}\b"
        rf"|from\s+tools\.device\.{stem}\s+import|import\s+tools\.device\.{stem}\b)",
        re.M,
    )
    users = [
        p.relative_to(ROOT).as_posix() for p in sorted(ROOT.glob("tools/device/*.py"))
    ]
    return [
        rel
        for rel in [*users, *test_files()]
        if rel != f"tools/device/{stem}.py"
        and pat.search((ROOT / rel).read_text(encoding="utf-8"))
    ]


def device_fine(path: str) -> Fine | None:
    if not (path.startswith("tools/device/") and path.endswith(".py")):
        return None
    stem = path.rsplit("/", 1)[-1].removesuffix(".py")
    seen, todo = {stem}, [stem]
    tests: list[str] = []
    while todo:  # the importers of the importers, inside tools/device
        cur = todo.pop()
        own = ROOT / f"tests/digikit/test_{cur}.py"
        if own.is_file():
            tests.append(own.relative_to(ROOT).as_posix())
        for user in importers(cur):
            if user.startswith("tests/"):
                tests.append(user)
            else:
                name = user.rsplit("/", 1)[-1].removesuffix(".py")
                if name not in seen:
                    seen.add(name)
                    todo.append(name)
    kinds = {k for s in seen for k in DEVICE_KINDS.get(s, ())}
    if kinds or stem not in DEVICE_KINDS:
        tests += scenario_tests(kinds) if stem in DEVICE_KINDS else list(ALL_SUITES)
    ordered = list(dict.fromkeys(tests))
    return Fine(f"tools/device/{stem}", ("python-tests",), tuple(ordered))


def digikit_fine(path: str) -> Fine | None:
    if path.startswith("tests/digikit/scenarios/") and path.endswith(".toml"):
        kind = scenario_refs().get(path, ("", frozenset()))[0]
        if not kind:  # deleted or unreadable: the whole suite
            return Fine(
                "tests/digikit/scenarios", ("python-tests",), ("tests/digikit",)
            )
        return Fine(
            f"scenario kind {kind}", ("python-tests",), tuple(scenario_tests({kind}))
        )
    name = path.rsplit("/", 1)[-1]
    for suite in ALL_SUITES:
        if (
            path.startswith(suite + "/")
            and path.count("/") == 2
            and name.startswith("test_")
            and name.endswith(".py")
        ):
            return Fine(f"{suite}/{name}", ("python-tests",), (path,))
    return None


def fine(path: str) -> Fine | None:
    """The finer route of PATH, or None to use the prefix rules of verify.py."""
    return module_fine(path) or device_fine(path) or digikit_fine(path)


# ---- mise.toml ---------------------------------------------------------------------------------


def run_lines(task: dict) -> list[str]:
    run = task.get("run", [])
    return [run] if isinstance(run, str) else [str(x) for x in run]


def paths_of_run(lines: list[str]) -> list[str] | None:
    """The repo paths a task's run lines name: `-m a.b.c` (a/b/c.py or a package), `node FILE`,
    test paths of `-m pytest`, pnpm and npx (the web workspace). None when a line names none."""
    found: list[str] = []
    for line in lines:
        try:
            words = shlex.split(line)
        except ValueError:
            return None
        mine: list[str] = []
        for i, word in enumerate(words):
            if word == "-m" and i + 1 < len(words) and words[i + 1] != "pytest":
                base = words[i + 1].replace(".", "/")
                for cand in (f"{base}.py", f"{base}/__init__.py"):
                    if (ROOT / cand).is_file():
                        mine.append(cand)
                        break
            elif word in ("pnpm", "npx"):
                mine.append("apps/web/package.json")
            elif (ROOT / word).exists() and "/" in word:
                mine.append(word)
        if not mine:
            return None
        found += mine
    return found


def mise_paths(
    old: str | None, new: str | None
) -> tuple[list[str], dict[str, str]] | None:
    """Repo paths to route in place of a changed mise.toml, with the task each came from; None
    when the change is broad: a section other than [tasks], a verify/build/gen/setup task, a task
    key other than run/depends/description/alias, a run that names no known file, a bad TOML."""
    if old is None or new is None:
        return None
    try:
        before, after = tomllib.loads(old), tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        return None
    tasks_old, tasks_new = before.pop("tasks", {}), after.pop("tasks", {})
    if before != after:
        return None
    paths: list[str] = []
    origin: dict[str, str] = {}
    for name in sorted(set(tasks_old) | set(tasks_new)):
        if tasks_old.get(name) == tasks_new.get(name):
            continue
        if name.split(":", 1)[0] in BROAD_TASK_GROUPS:
            return None
        task = tasks_new.get(name)
        if task is None:  # a removed task: only the path check reads it
            continue
        other = {
            k: v
            for k, v in task.items()
            if k not in ("run", "depends", "description", "alias")
        }
        if other != {
            k: v for k, v in (tasks_old.get(name) or {}).items() if k in other
        }:
            return None
        lines = run_lines(task)
        for dep in task.get("depends", []):
            lines += run_lines(tasks_new.get(dep, {}))
        named = paths_of_run(lines)
        if named is None:
            return None
        for path in named:
            origin.setdefault(path, f"mise.toml task {name}")
            paths.append(path)
    return list(dict.fromkeys(paths)), origin
