#!/usr/bin/env python3
"""Create a patchkit module from a template (`mise run patch:new -- category/name [--cf] [--dsp]`).

    uv run python -m tools.patchkit.pk_new fx/my-drive              # ColdFire and SHARC+ stubs
    uv run python -m tools.patchkit.pk_new fx/my-drive --dsp        # SHARC+ only: a master-bus hook that passes audio through
    uv run python -m tools.patchkit.pk_new test/my-demo --cf        # ColdFire only: code, data and bss in cf.expansion
    uv run python -m tools.patchkit.pk_new machines/my-synth --machine [--devices dt2,dn2]
                                                           # a machine type: a clone of type 0 with a permanent number

Writes `modules/<category>/<name>/`: `module.toml`, `src/cf/<name>.c` and/or `src/dsp/<name>.s`, `tests/smoke.toml`
(the quick tests `patch:dev` and `patch:test` run), `README.md`, `gallery/icon.svg`; then builds the objects with
tools/patchkit/pk_build.py, so the template is known to compile. The DSP stub reuses the anchor rules of fx/master-gain (rules
only: masked signatures and hashes, never firmware bytes). A new module claims `dsp.master.hook` when it has a DSP
part, so it conflicts with fx/master-gain: pick one at a time until the master-fx framework (Phase 3) exists.

A machine module (`--machine`) gets its permanent type number per device: the next free number in
modules/core/machines/types.toml, which the script appends and prints. The file is committed with the module; a number
is never reused or changed (a project stores only the number).
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
 * object). Anchors come from `[[anchor_rule]]` (masked signatures, never addresses); build with
 * `mise run patch:build -- modules/{id}`. */

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

SMOKE = """# Quick tests of {id}: `mise run patch:dev` runs them after every rebuild, `mise run patch:test` with the smoke tour.
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


def manifest(id_: str, sym: str, name: str, cf: bool, dsp: bool) -> str:
    parts = [
        f"""[module]
id = "{id_}"
version = "0.1.0"
name = "{name.replace("-", " ").capitalize()}"
category = "{id_.split("/")[0]}"
status = "experimental"
summary = "One short sentence for players, 120 characters at most, ending with a full stop."
requires = []
conflicts = []

# Test status per firmware; patchkit does not read it.
[targets]
"dt2-1.16" = "untested"
"dt2-1.17" = "untested"
"dn2-1.11" = "untested"
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
        donor = tomllib.loads(
            (MODULES / "fx" / "master-gain" / "module.toml").read_text()
        )
        for rule in donor["anchor_rule"]:
            lines = ["", "[[anchor_rule]]"]
            for k, v in rule.items():
                lines.append(
                    f'{k} = "{v}"'
                    if isinstance(v, str)
                    else f"{k} = {v:#x}"
                    if k == "offset"
                    else f"{k} = {v}"
                )
            parts.append("\n".join(lines) + "\n")
    return "".join(parts)


MACHINE_SECTION = """
# Typed section (modules/README.md, "Typed sections"): lowered per device into the machine, param and page rows.
# Give it knobs = [...] (stock codes and the ids of [[param]] sections), then [[param]], [[voice]] and, on a Digitone II,
# a [[page]]; `patchkit gen` writes the constants a DSP routine includes.
[[machine]]
id = "{eid}"
name = "{title}"
short = "{short}"
clone_of = 0            # the stock type this machine copies
devices = [{devices}]
"""


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
        n = max([STOCK_TYPES[dev] - 1, *alloc.values()]) + 1
        if n > d["last"]:
            raise SystemExit(
                f"{path}: [{dev}] is full: the next number {n} is above last = {d['last']}"
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


def machine_manifest(id_: str, name: str, devices: list[str]) -> str:
    title = name.replace("-", " ").capitalize()
    eid = name.replace("-", "_")
    head = f"""[module]
id = "{id_}"
version = "0.1.0"
name = "{title}"
category = "{id_.split("/")[0]}"
status = "experimental"
summary = "One short sentence for players, 120 characters at most, ending with a full stop."
requires = ["core/machines"]
conflicts = []

[targets]
"dt2-1.16" = "untested"
"dt2-1.17" = "untested"
"dn2-1.11" = "untested"
"dn2-1.12" = "untested"

# The machine's type number is permanent: modules/core/machines/types.toml, key "{id_}:{eid}".
"""
    short = re.sub(r"[^A-Z0-9]", "", name.upper())[:4] or "NEW"
    return head + MACHINE_SECTION.format(
        eid=eid,
        title=title[:23],
        short=short,
        devices=", ".join(f'"{d}"' for d in devices),
    )


def create(
    id_: str,
    cf: bool,
    dsp: bool,
    force: bool = False,
    machine: list[str] | None = None,
) -> Path:
    m = re.fullmatch(r"([a-z0-9]+)/([a-z0-9][a-z0-9-]*)", id_)
    if not m:
        raise SystemExit(
            f"module id is category/name (lowercase, digits, dashes): {id_!r}"
        )
    category, name = m.groups()
    if category not in TOP:
        print(
            f"note: category {category!r} is new (known: {', '.join(sorted(TOP))})",
            file=sys.stderr,
        )
    sym = "_" + re.sub(r"[^a-z0-9]+", "_", name)
    base = MODULES / category / name
    if base.exists() and not force:
        raise SystemExit(f"{base} exists (use --force to overwrite the stubs)")
    files = {
        "module.toml": machine_manifest(id_, name, machine)
        if machine
        else manifest(id_, sym, name, cf, dsp),
        "tests/smoke.toml": SMOKE.format(id=id_),
        "README.md": README.format(id=id_, title=name.replace("-", " ").capitalize()),
        "gallery/icon.svg": ICON,
    }
    if cf and not machine:
        files[f"src/cf/{name}.c"] = CF_C.format(id=id_, sym="pk" + sym)
    if dsp and not machine:
        files[f"src/dsp/{name}.s"] = DSP_S.format(id=id_, sym=sym)
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("id", help="category/name, for example fx/my-drive")
    ap.add_argument("--cf", action="store_true", help="ColdFire stub only")
    ap.add_argument("--dsp", action="store_true", help="SHARC+ stub only")
    ap.add_argument(
        "--machine", action="store_true", help="a machine type (clone of type 0)"
    )
    ap.add_argument(
        "--devices", default="dt2,dn2", help="with --machine: dt2, dn2 or dt2,dn2"
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)
    cf, dsp = (a.cf or not a.dsp), (a.dsp or not a.cf)
    machine = None
    if a.machine:
        machine = [d for d in a.devices.split(",") if d]
        if not machine or any(d not in STOCK_TYPES for d in machine):
            raise SystemExit("--devices is dt2, dn2 or dt2,dn2")
        cf = dsp = False
    base = create(a.id, cf, dsp, a.force, machine)
    print(f"created {base.relative_to(ROOT)}")
    if machine:
        eid = a.id.split("/")[1].replace("-", "_")
        for dev, n in allocate(f"{a.id}:{eid}", machine).items():
            print(
                f'type number on {dev}: {n} (appended to {TYPES.relative_to(ROOT)} as "{a.id}:{eid}"; commit it)'
            )
    if not a.no_build and not machine:
        from tools.patchkit import pk_build

        for line in pk_build.build_module(base, check=False):
            print(line)
    print(f"next: mise run patch:dev -- --modules {a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
