"""Build the windowless daemons (`digi-emud`) once for a verify run.

    uv run python -m tools.devtools.verify_build daemons [--device dt2,dn2] [--env-out FILE]

Before this, every python-tests process that drove a daemon asked cargo for the binary itself
(tools/patchkit/pk_harness.py `daemon_binary`): the first build of a new generated core ran inside a
test file, in parallel with the other files and the other steps. verify now runs this once, before the
steps that use a daemon (`build-daemons` in tools/devtools/verify.py), after the coupled-run builds, never in
parallel with them.

For each device that has its ready inputs it builds the profile-guided daemon when a PGO profile exists
(`pgo-emud-<device>` variant; the launcher's `pgo-use` is never touched), else the plain daemon (the target dir
of the device's generated core, shared with the coupled-run build). A PGO daemon is copied to an immutable
file (tools/devtools/stablebin.py) and `DIGI_EMUD_<DEV>=<copy>` is written to --env-out (JSON), so the test
processes run that copy and ask cargo for nothing; `pk_harness.daemon_binary` takes that variable before
anything else, so a device with a pinned PGO daemon never uses the plain one and it is not built (it used to
be, first, for every device). The plain daemon is not pinned the same way: the harness builds it for the stock
image it is given, and an image with another generated core has another binary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from tools.devtools import stablebin  # noqa: E402
from tools.patchkit import pk_emud_pgo, pk_harness  # noqa: E402


def build_daemons(devices: list[str]) -> dict[str, str]:
    """Build the daemons of DEVICES; the environment that pins the PGO ones."""
    env: dict[str, str] = {}
    for device in devices:
        cell = pk_harness.Cell(device, pk_emud_pgo.FIRMWARE[device])
        try:
            syx, _ready = pk_harness.firmware_inputs(cell)
        except pk_harness.HarnessError as exc:
            print(f"SKIP {device}: {exc}", flush=True)
            continue
        if pk_emud_pgo.native_pgo.current_profile(device) is None:
            plain = pk_harness.daemon_binary(device, syx, pgo=False)
            print(f"{device} plain: {plain}", flush=True)
            print(f"{device} pgo: no profile; the harness uses the plain daemon")
            continue
        pgo_path = pk_emud_pgo.build(device)
        target = pgo_path.parent.parent
        with stablebin.build_lock(target):
            copy = stablebin.stable_copy(pgo_path, target)
        print(f"{device} pgo: {copy}", flush=True)
        env[f"DIGI_EMUD_{device.upper()}"] = str(copy)
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("what", choices=["daemons"])
    ap.add_argument(
        "--device", default="dt2,dn2", help="comma-separated (default: dt2,dn2)"
    )
    ap.add_argument(
        "--env-out",
        type=Path,
        help="write the pinned DIGI_EMUD_<DEV> variables here (JSON)",
    )
    args = ap.parse_args(argv)
    devices = [d for d in args.device.split(",") if d]
    unknown = sorted(set(devices) - set(pk_emud_pgo.FIRMWARE))
    if unknown:
        ap.error(f"unknown devices: {', '.join(unknown)}")
    try:
        env = build_daemons(devices)
    except pk_harness.HarnessError as exc:
        print(f"verify_build: {exc}", file=sys.stderr)
        return 1
    if args.env_out:
        args.env_out.write_text(json.dumps(env, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
