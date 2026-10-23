"""Prove that one `digi-emud` with both generated cores boots either device coupled.

The daemon starts on the first device (DN2) with the second device's ready inputs passed as
`--also-coupled`. Then it opens the other firmware (`firmware.load replace`, as the app's Open does),
runs it, restarts it and goes back. Each phase must report `status.dsp == "sharc"`, the audio
report's `dsp_core` of that device, advancing DSP frames, no DSP halt and (with `--pcm`) a PCM tap.

    DIGI_EMUD=out/native/target-wt/NAME/dual/release/digi-emud \\
        uv run python -m tools.devtools.dual_core_check [--runtime] [--steps N] [--pcm] [--module digikit/pluck [--play]]

The binary comes from `cargo build --release -p digi-control --features second-core --bin digi-emud`
with SHARC_GEN_DIR (DN2) and SHARC_GEN_DIR_2 (DT2) set. It stops its daemon on exit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from typing import Any

from tools import python_env
from tools.control.digi_client import ROOT, Emu
from tools.emulator import digi_env
from tools.patchkit import pk_harness as H

STEP = 40_000_000  # ColdFire instructions per run slice (about 450 DSP frames)


def phase(
    emu: Emu, label: str, steps: int, expect_core: str, pcm: bool
) -> dict[str, Any]:
    """Run STEPS slices and check the DSP session; returns the facts."""
    if pcm:
        emu.pcm_start()
    before = emu.status()["audio"] or {}
    first = int(before.get("frames") or 0)
    for _ in range(steps):
        emu.step(STEP)
    status = emu.status()
    audio = status["audio"] or {}
    facts: dict[str, Any] = {
        "phase": label,
        "dsp": status["dsp"],
        "dsp_core": audio.get("dsp_core"),
        "frames_advanced": int(audio.get("frames") or 0) - first,
        "halted": audio.get("halted"),
        "firmware": (status["firmware"] or {}).get("syx_sha256", "")[:12],
    }
    if pcm and status["dsp"] == "sharc":
        report = emu.pcm_report()
        facts["pcm_peak"] = report.get("peak")
        facts["pcm_rms"] = report.get("rms")
    ok = (
        facts["dsp"] == "sharc"
        and str(facts["dsp_core"]).startswith(expect_core)
        and facts["frames_advanced"] > 0
        and not facts["halted"]
    )
    facts["ok"] = ok
    print(json.dumps(facts), flush=True)
    return facts


def chord(emu: Emu, names: list[str], hold: int = 3_000_000) -> None:
    """Press the buttons together, run, release them."""
    for name in names:
        emu.button(H.button(name), True)
    emu.step(hold)
    for name in reversed(names):
        emu.button(H.button(name), False)
    emu.step(1_000_000)


def run_module(emu: Emu, run_dir: str, module: str) -> dict[str, Any]:
    """`dk run MODULE --emu`: upload, select, restart (the guest's soft reset: a cold boot on this device's core),
    and read the boot's module results from the log."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.device.dkrun",
            module,
            "--emu",
            "--run-dir",
            run_dir,
            "--seconds",
            "1",
        ],
        cwd=ROOT,
        env=python_env(),
        capture_output=True,
        text=True,
    )
    out = done.stdout
    facts = {
        "module": module,
        "exit": done.returncode,
        "booted": "the device booted" in out,
        "mod_load": re.search(r"MOD_LOAD\s+index=0 result=1", out) is not None,
        "mod_dsp_ok": re.search(r"MOD_DSP\s+index=0 result=0", out) is not None,
    }
    print(json.dumps(facts), flush=True)
    if done.returncode:
        print(out[-1500:] + done.stderr[-1500:], file=sys.stderr)
    return facts


def play_pluck(emu: Emu, downs: int = 7) -> dict[str, Any]:
    """PLUCK on track 1 (FUNC+SRC, DOWN, YES) and TRIG 1 held with a PCM tap: the note must be audible."""
    chord(emu, ["FUNC", "SRC"])
    for _ in range(downs):
        chord(emu, ["DOWN"], 1_000_000)
    for _ in range(2):
        chord(emu, ["YES"])
    emu.pcm_start()
    emu.button(H.button("TRIG 1"), True)
    emu.step(60_000_000)
    emu.button(H.button("TRIG 1"), False)
    emu.step(10_000_000)
    report = emu.pcm_report()
    facts = {"pluck_rms": report.get("rms"), "pluck_peak": report.get("peak")}
    facts["ok"] = bool((report.get("peak") or 0) > 1e-4)
    print(json.dumps(facts), flush=True)
    return facts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--steps", type=int, default=3, help="40M-instruction slices per phase"
    )
    ap.add_argument(
        "--runtime",
        action="store_true",
        help="open each device with the runtime patched in",
    )
    ap.add_argument(
        "--pcm", action="store_true", help="report the PCM level of each phase"
    )
    ap.add_argument("--keep", help="keep the daemon's run directory here")
    ap.add_argument(
        "--module",
        help="after each open, load this run-time module with dk run (implies --runtime), restart, and check the DSP session again",
    )
    ap.add_argument(
        "--play",
        action="store_true",
        help="with --module digikit/pluck: choose PLUCK on DT2 track 1 and hold a note (the PCM must not be silent)",
    )
    args = ap.parse_args()
    args.runtime = args.runtime or bool(args.module)
    keep = args.keep or tempfile.mkdtemp(prefix="digi-dual-")

    dn2, dt2 = (digi_env.ready_inputs(device=d) for d in ("dn2", "dt2"))
    first = [
        str(dn2["firmware"]),
        "--auto-coupled",
        "--audio-profile-syx",
        str(dn2["firmware"]),
        "--dsp-image",
        str(dn2["DSP image"]),
        "--dsp-state",
        str(dn2["DSP state"]),
        "--cf-snapshot",
        str(dn2["CF snapshot"]),
        "--also-coupled",
        str(dt2["DSP image"]),
        str(dt2["DSP state"]),
        str(dt2["CF snapshot"]),
        str(dt2["firmware"]),
        "--no-audio",
    ]
    results: list[dict[str, Any]] = []
    with Emu.spawn(*first, run_dir=keep, timeout=300.0) as emu:
        results.append(phase(emu, "dn2 at start", args.steps, "dn2", args.pcm))
        for device, inputs, other in (("dt2", dt2, "dn2"), ("dn2", dn2, "dt2")):
            report = emu.call(
                "firmware.load",
                syx=str(inputs["firmware"]),
                replace=True,
                **({"runtime": True} if args.runtime else {}),
            )
            print(
                json.dumps(
                    {
                        "load": device,
                        "audio": report.get("audio"),
                        "start_used": report.get("start_used"),
                    }
                ),
                flush=True,
            )
            results.append(
                phase(
                    emu,
                    f"{device} after open (from {other})",
                    args.steps,
                    device,
                    args.pcm,
                )
            )
            if args.module:
                loaded = run_module(emu, keep, args.module)
                results.append(
                    {
                        "ok": loaded["exit"] == 0
                        and loaded["booted"]
                        and loaded["mod_dsp_ok"]
                    }
                )
                results.append(
                    phase(
                        emu,
                        f"{device} after {args.module} and restart",
                        args.steps,
                        device,
                        args.pcm,
                    )
                )
                if args.play and device == "dt2":
                    results.append(play_pluck(emu))
    ok = all(r["ok"] for r in results)
    print("DUAL CORE OK" if ok else "DUAL CORE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
