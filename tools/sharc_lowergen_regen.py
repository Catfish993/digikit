#!/usr/bin/env python3
"""Regenerate everything derived from tools/sharc_core for the generated
lowering, in one step (`mise run regen`):

1. the shared transpiled core (`mise run core`: out/native/shared-core/gen),
2. native/sharc/src/fast/lower/gen_forms.rs for the forms in its GENERATED
   list (add a form by listing it in FORMS_EXTRA, or on the command line),
3. the fuzz vectors and the test programs.

    uv run python tools/sharc_lowergen_regen.py [--add FORM,...] [--no-core]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / "native/sharc/src/fast/lower/gen_forms.rs"
sys.path.insert(0, str(ROOT / "tools"))

import sharc_lowergen_rs as r  # noqa: E402


def generated_list() -> list[str]:
    text = GEN.read_text()
    m = re.search(r"pub const GENERATED: &\[&str\] = &\[(.*?)\];", text)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def run(*cmd: str) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", default="", help="forms to add to the list")
    ap.add_argument("--no-core", action="store_true")
    a = ap.parse_args()
    forms = generated_list()
    for f in filter(None, a.add.split(",")):
        if f not in forms:
            forms.append(f)
    forms = sorted(forms, key=lambda n: (re.sub(r"\D", "", n) == "", int(re.sub(r"\D.*", "", n) or 0), n))
    if not a.no_core:
        run(
            "uv", "run", "python", "tools/sharc_transpile.py", "--strict",
            "--out", "out/native/shared-core/gen",
            "--work", "out/native/shared-core/work",
        )  # fmt: skip
    GEN.write_text(r.emit_file(forms))
    print("wrote", GEN.relative_to(ROOT), len(forms), "forms")
    run(
        "uv", "run", "python", "tools/sharc_lowergen_vectors.py",
        "--out", "native/sharc/src/tests_gen_vectors.rs", "--n", "24", *forms,
    )  # fmt: skip
    run(
        "uv", "run", "python", "tools/sharc_lowergen_programs.py",
        "--out", "native/sharc/src/tests_gen_programs.rs",
    )  # fmt: skip


if __name__ == "__main__":
    main()
