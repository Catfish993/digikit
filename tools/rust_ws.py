#!/usr/bin/env python3
"""Run cargo over the whole native workspace with the pinned toolchain.

    uv run python tools/rust_ws.py test [-- CARGO_ARGS...]   cargo test --workspace --release
    uv run python tools/rust_ws.py check [-- CARGO_ARGS...]  cargo check --workspace --all-targets
    uv run python tools/rust_ws.py gates                     the ignored coupled gate (needs the private DN2 ready inputs)
    uv run python tools/rust_ws.py fmt                       cargo fmt --check on the clean crates

``test`` and ``check`` use the generated core of ``tools/digi_env.py`` and its
target dir (``tools/native_target.py``), so they reuse the compiled
``sharc-gen``. The desktop crate embeds ``packages/web/dist``; it is left out
when the web package has not been built. ``fmt`` covers only ``CLEAN_CRATES``:
add a crate once ``cargo fmt -p CRATE --check`` passes; the list should only
grow.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))
try:
    import digi_env
    import native_target
finally:
    sys.path.pop(0)

CLEAN_CRATES = [
    "coldfire",
    "device-profile",
    "digiemu-desktop",
    "dt2-firmware-loader",
    "dt2-native-host",
    "live-audio",
    "periph",
    "plusdrive-format",
    "sharc-gen",
]


def workspace_args() -> list[str]:
    args = ["--workspace"]
    if not (ROOT / "packages/web/dist").is_dir():
        args += ["--exclude", "digiemu-desktop"]
    return args


def cargo(sub: list[str], env: dict[str, str]) -> int:
    command = ["rustup", "run", digi_env.TOOLCHAIN, "cargo", *sub]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("test", "check", "gates", "fmt"):
        print(__doc__)
        return 2
    mode, extra = argv[0], argv[1:]
    if extra[:1] == ["--"]:
        extra = extra[1:]
    env = digi_env.rust_env()
    if mode == "fmt":
        crates = [x for name in CLEAN_CRATES for x in ("-p", name)]
        return cargo(["fmt", "--check", *crates], env)
    gen = digi_env.generated_dir()
    if not gen.is_dir():
        raise SystemExit(f"rust_ws: generated core dir is missing: {gen}")
    digi_env.check_generated(gen)
    env["SHARC_GEN_DIR"] = str(gen)
    target = ["--target-dir", str(native_target.target_dir(gen)), "--locked"]
    if mode == "gates":
        # coupled_ready_exactness: restores the ready session, 100M instructions.
        env["DIGI_COUPLED_FIXTURES"] = str(digi_env.READY)
        env["DIGI_COUPLED_SYX"] = str(digi_env.firmware())
        sub = [
            "test",
            "--release",
            "-p",
            "digi-coupled",
            "--features",
            "coupled-audio",
            "--lib",
            *target,
            "gate_tests",
            "--",
            "--ignored",
        ]
        return cargo(sub, env)
    if mode == "check":
        sub = ["check", *workspace_args(), "--all-targets", *target]
    else:
        sub = ["test", *workspace_args(), "--release", *target]
    return cargo([*sub, *extra], env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
