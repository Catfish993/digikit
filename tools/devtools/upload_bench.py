"""Throughput of the digikit upload and VERIFY on a free-running emulator (the desktop app's setup), split into guest work and protocol.

    uv run python -m tools.devtools.upload_bench [--device dn2|dt2] [--bytes N] [--chunk N] [--runs N] [--settle S] [--args ...]

Holds the benchmark lock exclusively while the bench runs (`--no-lock`: shared). Starts one coupled `digi-emud` daemon with the runtime patched in (as `pk_digikit_scen.live_boot` does), runs `tools/devtools/upload_bench.mjs` against it
(DN2: BEGIN/DATA/END and ranged VERIFY; DT2: the stock +Drive MidiRpc write of `upload_bench_rpc.mjs`) and stops the daemon. One JSON line per run.
The real-time factor and the guest instruction counts come from `status`; the host is shared, so compare runs by guest instructions per byte and
run before and after paired in the same minutes (see AGENTS.md: one paired A/B per claim).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from tools import python_env
from tools.control.digi_client import Emu
from tools.devtools import benchlock
from tools.firmware import config
from tools.patchkit import pk_harness as H

ROOT = Path(__file__).resolve().parents[2]
IMAGE = {"dn2": "1.12", "dt2": "1.17"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", choices=sorted(IMAGE), default="dn2")
    ap.add_argument(
        "--settle",
        type=float,
        default=12.0,
        help="seconds the daemon runs free before the bench starts",
    )
    ap.add_argument(
        "--no-lock",
        action="store_true",
        help="hold the benchmark lock shared, not exclusive (tools/devtools/benchlock.py), while the bench runs",
    )
    ap.add_argument(
        "--script",
        help="node script (default: upload_bench.mjs for dn2, upload_bench_rpc.mjs for dt2)",
    )
    ap.add_argument(
        "args", nargs=argparse.REMAINDER, help="after `--`: flags of the node script"
    )
    ns = ap.parse_args(argv)
    dev, ver = ns.device, IMAGE[ns.device]
    spec = H.FIRMWARE[(dev, ver)]
    syx = config.stock(spec["syx"])
    script = ns.script or (
        "tools/devtools/upload_bench.mjs"
        if dev == "dn2"
        else "tools/devtools/upload_bench_rpc.mjs"
    )
    rest = [a for a in ns.args if a != "--"]
    with Emu.spawn_coupled(
        syx,
        H.ROOT / spec["ready"],
        timeout=300,
        binary=H.daemon_binary(dev, syx),
        run_dir=None,
    ) as emu:
        if dev == "dt2":
            from tools.emulator import card_check

            samples = H.OUT / "samples"
            card_check.make_samples(samples)
            card = emu.call("card.load", dir=str(samples))["card"]
            emu.load(syx, runtime=True, start="auto", card=card)
        else:
            emu.load(syx, runtime=True, start="auto")
        emu.call("run.mode", mode="free")
        time.sleep(ns.settle)
        with benchlock.hold(
            "shared" if ns.no_lock else "exclusive", f"upload bench {dev}"
        ):
            t0 = time.monotonic()
            r = subprocess.run(
                ["node", script, emu.path, *rest], cwd=ROOT, env=python_env()
            )
            print(
                f"# {dev} {ver}: bench {time.monotonic() - t0:.1f} s, exit {r.returncode}",
                file=sys.stderr,
            )
        return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
