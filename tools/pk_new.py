#!/usr/bin/env python3
"""Create a patchkit module from a template (`mise run patch:new -- category/name [--cf] [--dsp]`).

    uv run python tools/pk_new.py fx/my-drive              # ColdFire and SHARC+ stubs
    uv run python tools/pk_new.py fx/my-drive --dsp        # SHARC+ only: a master-bus hook that passes audio through
    uv run python tools/pk_new.py test/my-demo --cf        # ColdFire only: code, data and bss in cf.expansion

Writes `modules/<category>/<name>/`: `module.toml`, `src/cf/<name>.c` and/or `src/dsp/<name>.s`, `tests/smoke.toml`
(the quick tests `patch:dev` and `patch:test` run), `gallery/README.md`; then builds the objects with
tools/pk_build.py, so the template is known to compile. The DSP stub reuses the anchor rules of fx/master-gain (rules
only: masked signatures and hashes, never firmware bytes). A new module claims `dsp.master.hook` when it has a DSP
part, so it conflicts with fx/master-gain: pick one at a time until the master-fx framework (Phase 3) exists.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ROOT / "modules"
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
# Steps (tools/pk_harness.py): tap, hold, turn, wait_ms, expect_frame, expect_pcm_db, expect_no_fault.
# Frame goldens are recorded with `uv run python tools/pk_harness.py run --update` (hashes only are committed).
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

{id}: describe what the module does for a player in one or two sentences.

- Devices and firmware: Digitakt II 1.17, Digitone II 1.12 (status in `module.toml` `[targets]`).
- What changes on the device, and how to hear or see it.
- Known limits.

The gallery shows this text. Keep it free of firmware bytes and vendor tool names.
"""


def manifest(id_: str, sym: str, name: str, cf: bool, dsp: bool) -> str:
    parts = [
        f"""[module]
id = "{id_}"
version = "0.1.0"
name = "{name.replace("-", " ").capitalize()}"
category = "{id_.split("/")[0]}"
status = "experimental"
summary = "Describe the module in one line"
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


def create(id_: str, cf: bool, dsp: bool, force: bool = False) -> Path:
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
        "module.toml": manifest(id_, sym, name, cf, dsp),
        "tests/smoke.toml": SMOKE.format(id=id_),
        "gallery/README.md": README.format(
            id=id_, title=name.replace("-", " ").capitalize()
        ),
    }
    if cf:
        files[f"src/cf/{name}.c"] = CF_C.format(id=id_, sym="pk" + sym)
    if dsp:
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
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args(argv)
    cf, dsp = (a.cf or not a.dsp), (a.dsp or not a.cf)
    base = create(a.id, cf, dsp, a.force)
    print(f"created {base.relative_to(ROOT)}")
    if not a.no_build:
        import pk_build

        for line in pk_build.build_module(base, check=False):
            print(line)
    print(f"next: mise run patch:dev -- --modules {a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
