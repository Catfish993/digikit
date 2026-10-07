#!/usr/bin/env python3
"""Build the DN2 ready inputs from the .syx alone (no captured state).

    uv run python tools/ready_dn2.py [--out DIR] [--syx SYX] [--force]
    mise run ready-dn2

Steps (each skipped when its output exists and --force is not given):
  1. digi-audio-dn2-image.bin  packed section 7 image (tools/sharc_pack_image.py)
  2. cold.state                DSP cold-start state (tools/sharc_cold_state.py)
  3. cold-idle.dsp             `dsp_cold` example: the cold DSP run alone (about
                               60 s) to its first rest in the DN2 idle range
  4. digi-audio-m5.snap(.dsp)  `sharc_live`: ColdFire cold boot with that DSP
                               state attached at frame 0; saves both at ready

Output goes to a git-ignored snapshots/ directory (default
snapshots/dn2-audio-ready-cold-2026-10-07); set `ready_dir` in
tools/ready_inputs.json to use it. Everything written is firmware-derived.
The firmware's SHA-256 is recorded in SOURCE.json next to the files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import benchlock
    import digi_env
    import native_target
    import sharc_cold_state
finally:
    sys.path.pop(0)

DEFAULT_OUT = ROOT / "snapshots" / "dn2-audio-ready-cold-2026-10-07"
IMAGE_NAME = "dn2-1.11"
# After ready the example runs a little further so the snapshot is not at the
# very first ready instruction; the QA inputs start after the save.
EXTRA_AFTER_READY = "1000000"


def build_examples() -> pathlib.Path:
    gen = digi_env.generated_dir()
    target = native_target.target_dir(gen)
    env = {**digi_env.rust_env(), "SHARC_GEN_DIR": str(gen)}
    for example in ("dsp_cold", "sharc_live"):
        cmd = [
            "rustup", "run", digi_env.TOOLCHAIN, "cargo", "build", "--release",
            "--locked", "--manifest-path", "native/boot/Cargo.toml",
            "--features", "sharc", "--example", example,
            "--target-dir", str(target),
        ]  # fmt: skip
        with benchlock.hold("shared", f"ready-dn2 build {example}"):
            if subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode:
                raise SystemExit(f"ready-dn2: building {example} failed")
    return target / "release" / "examples"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    p.add_argument("--syx", type=pathlib.Path, default=digi_env.firmware())
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    out = a.out if a.out.is_absolute() else ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    syx_sha = hashlib.sha256(a.syx.read_bytes()).hexdigest()

    image = out / "digi-audio-dn2-image.bin"
    state = out / "cold.state"
    idle = out / "cold-idle.dsp"
    snap = out / "digi-audio-m5.snap"
    todo = lambda path: a.force or not path.exists()  # noqa: E731

    if todo(image):
        import sharc_pack_image

        sharc_pack_image.main(["sharc_pack_image", IMAGE_NAME, str(image)])
    if todo(state):
        blob, entry = sharc_cold_state.cold_blob(IMAGE_NAME)
        state.write_bytes(blob)
        print(f"{state}: entry sw {entry:#x}")
    examples = build_examples()
    env = {**os.environ, "SHARC_FAST_REGIONS": ""}
    if todo(idle):
        with benchlock.hold("shared", "ready-dn2 cold DSP"):
            subprocess.run(
                [str(examples / "dsp_cold"), str(image), str(state), str(idle)],
                cwd=ROOT, env=env, check=True,
            )  # fmt: skip
    sidecar = idle.read_bytes()
    clock = int.from_bytes(sidecar[8:16], "little")
    raw = out / "cold-idle.state"
    raw.write_bytes(sidecar[24:])
    if todo(snap):
        live_env = {
            **env,
            "DSP_CLOCK_BASE": str(clock),
            "CF_SNAPSHOT": str(snap),
            "NOTE_EVENTS": "trig",
        }
        with benchlock.hold("shared", "ready-dn2 CF boot"):
            subprocess.run(
                [
                    str(examples / "sharc_live"), str(a.syx), str(raw), str(image),
                    str(out / "boot.wav"), EXTRA_AFTER_READY,
                ],
                cwd=ROOT, env=live_env, check=True,
            )  # fmt: skip
    for scratch in (raw, *out.glob("boot.wav*")):
        scratch.unlink(missing_ok=True)
    (out / "SOURCE.json").write_text(
        json.dumps(
            {"syx_sha256": syx_sha, "dsp_idle_clock": clock, "image": IMAGE_NAME},
            indent=1,
        )
        + "\n"
    )
    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(
        f"ready inputs in {rel}; set ready_dir in tools/ready_inputs.json to use them"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
