#!/usr/bin/env python3
"""Deliberately broken patched images, for the teeth test of the proofs' quick tiers (never committed: out/broken/).

    uv run python -m tools.patchkit.pk_break persist|rows --modules a,b [--device dt2] [--fw 1.17] [-o OUT.syx]
    PK_PROOF_IMAGE=OUT.syx uv run python -m tools.patchkit.pk_proof_pluck --quick

The break is made in a copy of the module sources (out/broken/KIND/modules; the repository is untouched), built with
`patchkit --root`, so the image is what a module author's mistake would produce:

  persist  core/machines leaves the persistence immediate at the stock value: a saved project does not keep the
           machine type (the type falls back to SAMPLE or FM TONE when the project is loaded).
  rows     machines/pluck lists DECAY and PICK in each other's descriptor fields (a wrong parameter mapping): the
           knobs, the names and the sound of those two parameters are exchanged.

`PK_PROOF_IMAGE` makes every proof session that has modules load that image instead of the patch of its modules
(tools/patchkit/pk_proofkit.py); the plan (symbols) is read from OUT.plan.json next to it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

from tools.firmware import config  # noqa: E402
from tools.patchkit import patchkit  # noqa: E402

# kind -> (module manifest, old text, new text); the old text must occur exactly once
BREAKS = {
    "persist": (
        "core/machines/module.toml",
        'value = "image.u8(cf.machines.persist + 1) + machines.count"',
        'value = "image.u8(cf.machines.persist + 1)"',
    ),
    "rows": (
        "machines/pluck/module.toml",
        'f1 = "params.pluck_decay.index"\nf2 = "params.pluck_pick.index"',
        'f1 = "params.pluck_pick.index"\nf2 = "params.pluck_decay.index"',
    ),
}
FIRMWARE = {
    ("dt2", "1.17"): "Digitakt_II_OS1.17.syx",
    ("dt2", "1.16"): "Digitakt_II_OS1.16.syx",
    ("dn2", "1.12"): "Digitone_II_OS1.12.syx",
}


def build(kind: str, mods: str, device: str, fw: str, out: Path) -> Path:
    manifest, old, new = BREAKS[kind]
    root = ROOT / "out" / "broken" / kind
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(ROOT / "modules", root / "modules")
    (root / "devices").symlink_to(ROOT / "devices")
    path = root / "modules" / manifest
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(
            f"{manifest}: the text to break occurs {text.count(old)} times"
        )
    path.write_text(text.replace(old, new))
    out.parent.mkdir(parents=True, exist_ok=True)
    plan = out.with_suffix(".plan.json")
    r = subprocess.run(
        [str(patchkit.binary()), "--root", str(root), "apply", "--firmware", str(config.stock(FIRMWARE[(device, fw)])),
         "--modules", mods, "-o", str(out), "--plan", str(plan)],
        cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    if r.returncode:
        raise SystemExit(f"patchkit apply failed:\n{(r.stderr or r.stdout)[-1500:]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("kind", choices=sorted(BREAKS))
    ap.add_argument("--modules", required=True)
    ap.add_argument("--device", default="dt2")
    ap.add_argument("--fw", default="1.17")
    ap.add_argument("-o", type=Path)
    a = ap.parse_args()
    out = a.o or ROOT / "out" / "broken" / f"{a.kind}-{a.device}-{a.fw}.syx"
    print(build(a.kind, a.modules, a.device, a.fw, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
