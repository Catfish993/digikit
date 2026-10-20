#!/usr/bin/env python3
"""patchkit: the ColdFire memory audit of one run-time module in the emulator (docs/MODULE-AUTHORING.md, "module:audit").

    uv run python -m tools.patchkit.pk_modaudit NAME [--device dt2|dn2]      (mise run module:audit NAME)

NAME is a module id (`test/dyn-demo`) or its last part. The module is packed with the run-time modules it `requires`, loaded by the
digikit runtime on a ColdFire-only emulator, and the UI is driven for a bounded time (tools/patchkit/pk_digikit_memaudit.py): the DIGIKIT
menu, the machine list (each of the first entries is chosen on track 1 and its page turned, which reaches a machine the module adds),
a project save and a load. The write guard reports every ColdFire store into the free regions outside the module blocks by code other than
the patch objects. A hit names the program counter, the address and the code it belongs to. Exit code 0: no hit. A module that the
runtime cannot load (a patched-in module) is reported and skipped. One device when `--device` is given, else every device the module
targets.
"""

from __future__ import annotations

import argparse
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from tools.patchkit import modroots, patchkit, pk_modcheck

SWEEP = 14  # machine list entries chosen on track 1 (the stock list is shorter than this on both devices: the run-time machines follow it)


def run_set(mid: str) -> list[str]:
    """MID and the modules it requires, in load order, as far as they are run-time modules."""
    out: list[str] = []

    def add(m: str) -> None:
        if m in out:
            return
        man = tomllib.loads((modroots.module_dir(m) / "module.toml").read_text())
        for r in man["module"].get("requires", []):
            if r in modroots.manifests() and not r.startswith("core/"):
                add(r)
        out.append(m)

    add(mid)
    return out


def packs(mid: str, device: str) -> str | None:
    """None when MID packs for DEVICE, else the reason."""
    with tempfile.TemporaryDirectory(prefix="pk-modaudit-") as tmp:
        r = patchkit.run(
            ["pack", mid, "--device", device, "-o", str(Path(tmp) / "m.dkm")],
            capture=True,
        )
    if r.returncode:
        why = (r.stderr or r.stdout).strip().splitlines()
        return why[-1] if why else "pack failed"
    return None


def has_machine(mid: str) -> bool:
    man = tomllib.loads((modroots.module_dir(mid) / "module.toml").read_text())
    return man["module"].get("type") == "machine" or "machine" in man


def audit(mid: str, device: str) -> int:
    from tools.patchkit import pk_digikit_scen as S

    mods = run_set(mid)
    for m in mods:
        if why := packs(m, device):
            print(f"{device}: {m}: not a run-time module ({why}); skipped")
            return 0
    data: dict[str, Any] = {
        "scenario": {"name": f"module-audit-{mid.split('/')[-1]}"},
        "debug": {
            "part": "memaudit",
            "modules": mods,
            "must_run": [],
            **({"sweep": SWEEP} if any(has_machine(m) for m in mods) else {}),
        },
    }
    scen = S.Scenario(
        data["scenario"]["name"],
        "debug",
        "quick",
        [device],
        data,
        Path("module:audit"),
    )
    rep = S.run_scenario(scen, S.QUICK_IMAGES[device], None, "quick")
    ma = rep.get("memaudit", {})
    print(
        f"{device}: {mid}: {ma.get('total', '?')} store(s) outside the module blocks "
        f"({rep['seconds']} s) {'PASS' if rep['pass'] else 'FAIL'}"
    )
    for h in ma.get("hits", []):
        print(
            f"  pc {h['pc']:#x} ({h['writer']}) addr {h['addr']:#x} size {h['size']} in {h['label']}"
        )
    return 0 if rep["pass"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("--device", choices=("dt2", "dn2"))
    a = ap.parse_args()
    mid = pk_modcheck.resolve(a.name)
    man = tomllib.loads((modroots.module_dir(mid) / "module.toml").read_text())
    targets = {k.split("-")[0] for k in man.get("targets", {})}
    devices = [a.device] if a.device else [d for d in ("dt2", "dn2") if d in targets]
    rc = 0
    for dev in devices:
        rc |= audit(mid, dev)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
