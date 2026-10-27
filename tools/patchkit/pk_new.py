#!/usr/bin/env python3
"""Create a patchkit module from a template (`mise run module:new -- category/name [--cf] [--dsp]`).

    uv run python -m tools.patchkit.pk_new fx/my-drive              # ColdFire and SHARC+ stubs
    uv run python -m tools.patchkit.pk_new fx/my-drive --dsp        # SHARC+ only: a master-bus hook that passes audio through
    uv run python -m tools.patchkit.pk_new test/my-demo --cf        # ColdFire only: code, data and bss in cf.expansion
    uv run python -m tools.patchkit.pk_new em/my-synth --machine [--dir DIR]
                                                           # a run-time machine of the SDK contract (a copy of test/sine)

Writes `modules/<category>/<name>/`: `module.toml`, `src/cf/<name>.c` and/or `src/dsp/<name>.s`, `tests/smoke.toml`
(the quick tests `module:dev` and `patch:test` run), `README.md`, `gallery/icon.svg`; then builds the objects with
tools/patchkit/pk_build.py, so the template is known to compile. The DSP stub uses the master anchors declared in devices/fw/dsp/master.toml. A new module claims `dsp.master.hook` when it has a DSP
part, so it conflicts with digikit/master-gain: pick one at a time until the master-fx framework (Phase 3) exists.

`--machine` takes `AUTHOR/NAME` and writes a run-time machine of the SDK contract: a copy of tests/fixtures/modules/sine (one DSP
body for both devices, one parameter) with the ids and symbols renamed, in `modules/machines/<name>/` or, with `--dir DIR`, in
`DIR/<name>/` (a folder that PATCHKIT_MODULES names finds it). A run-time machine has no permanent type number: the runtime
assigns one at load (docs/design/module-sdk.md 11.5 D7). `core` is refused as author.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

MODULES = ROOT / "modules"
TYPES = MODULES / "core" / "machines" / "types.toml"
STOCK_TYPES = {"dt2": 7, "dn2": 5}  # the first number that is not a stock type
TOP = {"fx", "machines", "test", "ui", "tools"}

CF_C = """/* {id}: ColdFire code in the expansion region (cf.expansion).
 *
 * The core/expansion loader copies code, data and bss here before the reset clear; nothing calls this module yet.
 * Hook it with an `[[op]]` in module.toml (op = "retarget": rewrite a jsr/bsr/jmp at an anchor to a symbol of this
 * object). Anchors are declared in devices/fw/ (masked signatures, never addresses); build with
 * `mise run module:build -- modules/{id}`. */

const unsigned {sym}_version = 1;
unsigned {sym}_calls;

unsigned {sym}_entry(unsigned arg)
{{
    {sym}_calls++;
    return arg + {sym}_version;
}}
"""

DSP_S = """// {id}: SHARC+ master-bus hook (placed in free L2, dsp.l2).
//
// patchkit retargets the one CALL of the firmware's Q31 converter to {sym}_hook. The converter reads 64 planar
// floats (32 left, then 32 right) from the master work buffer; process them here and tail-jump to the converter so
// its frame, return address and the arguments in R4 and R8 stay untouched. R0, R1 and I4 are free at this site;
// M5 = 0 and M6 = 1 are the firmware's modifier constants (docs/findings/06, "Master hook"). This stub passes the
// audio through unchanged: add the processing between the label and the JUMP.

.SECTION/PM seg_pmco;
.GLOBAL {sym}_hook;
.EXTERN _fw_q31_convert;
.EXTERN _fw_master_buf;

{sym}_hook:
    JUMP _fw_q31_convert;

.SECTION/DM seg_dmda;
.GLOBAL {sym}_param;
.VAR {sym}_param = 0x3f800000;
"""

SMOKE = """# Quick tests of {id}: `mise run module:dev` runs them after every rebuild, `mise run patch:test` with the smoke tour.
# Steps (tools/patchkit/pk_harness.py): tap, hold, turn, wait_ms, expect_frame, expect_pcm_db, expect_no_fault.
# Frame goldens are recorded with `uv run python -m tools.patchkit.pk_harness run --update` (hashes only are committed).
[test]
name = "audible"
devices = ["dt2", "dn2"]
quick = true

[[step]]
tap = "TRIG 1"
[[step]]
wait_ms = 400
[[step]]
expect_pcm_db = {{ min = -60, max = -3 }}
[[step]]
expect_no_fault = true
"""

README = """# {title}

{id}: what the module does, for a player, in one or two sentences.

- Devices and firmware: Digitakt II 1.17, Digitone II 1.12 (status in `module.toml` `[targets]`).
- What changes on the device, and how to hear or see it.
- Known limits.

The web gallery shows `summary` from module.toml (one short sentence) and `gallery/icon.svg`. Keep this file free
of firmware bytes and vendor tool names.
"""

ICON = """<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <rect x="8" y="8" width="32" height="32" rx="6"/>
  <circle cx="24" cy="24" r="4" style="stroke: var(--icon-accent); fill: var(--icon-accent)"/>
</svg>
"""


def kind_of(id_: str, machine: bool) -> str:
    """The manifest `type`: machine, fx (category fx) or raw. `framework` is for core modules and is never generated."""
    return "machine" if machine else ("fx" if id_.startswith("fx/") else "raw")


def manifest(id_: str, sym: str, name: str, cf: bool, dsp: bool) -> str:
    parts = [
        f"""[module]
id = "{id_}"
version = "0.1.0"
name = "{name.replace("-", " ").capitalize()}"
category = "{id_.split("/")[0]}"
type = "{kind_of(id_, False)}"
author = "digikit"
status = "experimental"
summary = "One short sentence for players, 120 characters at most, ending with a full stop."
requires = []
conflicts = []

# Test status per firmware; patchkit does not read it.
[targets]
"dt2-1.17" = "untested"
"dn2-1.12" = "untested"
"""
    ]
    if cf:
        parts.append(f"""
[[object]]
path = "build/{name}-cf.dko.json"
sources = ["src/cf/{name}.c"]
place = "cf.expansion"
""")
    if dsp:
        parts.append(f"""
[[object]]
path = "build/{name}-dsp.dko.json"
sources = ["src/dsp/{name}.s"]
place = "dsp.l2"

[object.externs]
_fw_q31_convert = "dsp.master.q31_converter"
_fw_master_buf = "dsp.master.float_buf"

[[op]]
op = "retarget"
anchor = "dsp.master.q31_call"
to = "{sym}_hook"

[[claim]]
resource = "dsp.master.hook"
""")
    return "".join(parts)


def allocate(key: str, devices: list[str], path: Path = TYPES) -> dict[str, int]:
    """Give `key` ("module id:entry id") the next free number on each device: appended to `[<dev>.alloc]` of `path`."""
    text = path.read_text()
    data = tomllib.loads(text)
    out: dict[str, int] = {}
    for dev in devices:
        d = data.get(dev)
        if d is None:
            raise SystemExit(f"{path}: no [{dev}] section")
        alloc = d.get("alloc", {})
        if key in alloc:
            out[dev] = alloc[key]
            continue
        # numbers from `dynamic_first` up belong to run-time modules (core/dynreg reserves them)
        top = d.get("dynamic_first", d["last"] + 1) - 1
        n = max([STOCK_TYPES[dev] - 1, *(v for v in alloc.values() if v <= top)]) + 1
        if n > top:
            raise SystemExit(
                f"{path}: [{dev}] is full: the next number {n} is above {top} "
                f"(last = {d['last']}, run-time range from {d.get('dynamic_first', 'none')})"
            )
        lines = text.split("\n")
        start = lines.index(f"[{dev}.alloc]")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("[")),
            len(lines),
        )
        at = start + 1
        for i in range(start + 1, end):
            if lines[i].strip():
                at = i + 1
        lines.insert(at, f'"{key}" = {n}')
        text = "\n".join(lines)
        out[dev] = n
    path.write_text(text)
    return out


SINE = ROOT / "tests" / "fixtures" / "modules" / "sine"
SINE_PLACEHOLDER = (
    "One short sentence for players, 120 characters at most, ending with a full stop."
)


def machine_files(author: str, name: str) -> dict[str, str]:
    """The files of a run-time machine: a copy of test/sine with the module id, the machine, parameter and page ids, the file
    names and the DSP symbols renamed (no build outputs)."""
    id_ = f"{author}/{name}"
    eid = name.replace("-", "_")
    sym = "_" + eid
    title = name.replace("-", " ").capitalize()
    short = re.sub(r"[^A-Z0-9]", "", name.upper())[:4] or "NEW"
    man = (SINE / "module.toml").read_text()
    for old, new in (
        ('id = "test/sine"', f'id = "{id_}"'),
        ('name = "SINE"', f'name = "{title[:23]}"'),
        ('category = "test"', 'category = "machines"'),
        ('author = "digikit"', f'author = "{author}"'),
        ("-o sine.dkm", f"-o {name}.dkm"),
        ("test/sine", id_),
        ("build/sine-dsp", f"build/{name}-dsp"),
        ("src/dsp/sine.s", f"src/dsp/{name}.s"),
        ("sine_level", f"{eid}_level"),
        ("sine_syn", f"{eid}_syn"),
        ('machine = "sine"', f'machine = "{eid}"'),
        ('id = "sine"', f'id = "{eid}"'),
        ('name = "Sine"', f'name = "{title[:23]}"'),
        ('short = "SIN"', f'short = "{short}"'),
        ('name = "SINE"', f'name = "{title[:23].upper()}"'),
        ('title = "Sine"', f'title = "{title}"'),
        ("_sn_", sym + "_"),
    ):
        man = man.replace(old, new)
    man = re.sub(r'summary = "[^"]*"', f'summary = "{SINE_PLACEHOLDER}"', man, count=1)
    dsp = (SINE / "src" / "dsp" / "sine.s").read_text()
    dsp = dsp.replace("_sn_", sym + "_").replace("test/sine", id_)
    return {
        "module.toml": man,
        f"src/dsp/{name}.s": dsp,
        "README.md": f"# {title}\n\n{id_}: what the machine sounds like, for a player, in one or two sentences.\n\n"
        "Built from the template of `mise run module:new -- AUTHOR/NAME --machine` (docs/MODULE-AUTHORING.md).\n",
    }


def create(
    id_: str,
    cf: bool,
    dsp: bool,
    force: bool = False,
    machine: bool = False,
    dir_: Path | None = None,
) -> Path:
    m = re.fullmatch(r"([a-z0-9]+)/([a-z0-9][a-z0-9-]*)", id_)
    if not m:
        raise SystemExit(
            f"module id is {'author' if machine else 'category'}/name (lowercase, digits, dashes): {id_!r}"
        )
    category, name = m.groups()
    if category == "core":
        raise SystemExit(
            "core modules are internal: pick another "
            + ("author" if machine else "category")
        )
    if machine and category == "framework":
        raise SystemExit(
            "framework is a module type of the core modules, not an author"
        )
    if not machine and category not in TOP:
        print(
            f"note: category {category!r} is new (known: {', '.join(sorted(TOP))})",
            file=sys.stderr,
        )
    sym = "_" + re.sub(r"[^a-z0-9]+", "_", name)
    if machine:
        base = (dir_ if dir_ is not None else MODULES / "machines") / name
        files = machine_files(category, name)
    else:
        base = (dir_ if dir_ is not None else MODULES / category) / name
        files = {
            "module.toml": manifest(id_, sym, name, cf, dsp),
            "tests/smoke.toml": SMOKE.format(id=id_),
            "README.md": README.format(
                id=id_, title=name.replace("-", " ").capitalize()
            ),
            "gallery/icon.svg": ICON,
        }
        if cf:
            files[f"src/cf/{name}.c"] = CF_C.format(id=id_, sym="pk" + sym)
        if dsp:
            files[f"src/dsp/{name}.s"] = DSP_S.format(id=id_, sym=sym)
    if base.exists() and not force:
        raise SystemExit(f"{base} exists (use --force to overwrite the stubs)")
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "id", help="category/name (for example fx/my-drive); with --machine author/name"
    )
    ap.add_argument("--cf", action="store_true", help="ColdFire stub only")
    ap.add_argument("--dsp", action="store_true", help="SHARC+ stub only")
    ap.add_argument(
        "--machine",
        action="store_true",
        help="a run-time machine of the SDK contract (a copy of test/sine)",
    )
    ap.add_argument(
        "--dir",
        type=Path,
        help="write DIR/<name> instead of modules/<category>/<name> (list DIR in PATCHKIT_MODULES to use it)",
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)
    cf, dsp = (a.cf or not a.dsp), (a.dsp or not a.cf)
    if a.machine:
        cf = dsp = False
    base = create(a.id, cf, dsp, a.force, a.machine, a.dir)
    shown = base.relative_to(ROOT) if base.is_relative_to(ROOT) else base
    print(f"created {shown}")
    if not a.no_build:
        from tools.patchkit import pk_build

        for line in pk_build.build_module(base, check=False):
            print(line)
        if a.machine:  # the generated constants and includes of the module (build/gen)
            import os

            from tools.patchkit import patchkit

            roots = [str(base.parent.resolve()), os.environ.get("PATCHKIT_MODULES", "")]
            os.environ["PATCHKIT_MODULES"] = os.pathsep.join(r for r in roots if r)
            patchkit.run(["gen"])
    where = f"PATCHKIT_MODULES={base.parent} " if a.dir else ""
    if a.machine:
        print(f"next: {where}mise run module:check -- {a.id}")
    else:
        print(f"next: mise run module:dev -- --modules {a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
