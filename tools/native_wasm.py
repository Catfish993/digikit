#!/usr/bin/env python3
"""Build the portable native runtime for the browser without changing toolchains.

Writes two files:
  packages/web/public/emulator-core.wasm
      CF-only core (small), used for the normal boot.
  packages/web/.local/emulator-core-sharc.wasm
      coupled ColdFire + SHARC+ core (large, firmware-derived, gitignored). The
      dev server serves it; it must never be in public/ or dist/. Built when a
      generated core is available and --cf-only is not given. A stale copy in
      packages/web/public is deleted on every run.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import digi_env  # noqa: E402
finally:
    sys.path.pop(0)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--diagnostics",
    action="store_true",
    help="include bounded PC profiling and event history",
)
parser.add_argument(
    "--cf-only",
    action="store_true",
    help="build only the CF-only browser core, even when a generated core is available",
)
parser.add_argument(
    "--sharc",
    action="store_true",
    help="fail unless the coupled SHARC+ core (emulator-core-sharc.wasm) can be built "
    "(needs SHARC_GEN_DIR or the default generated core)",
)
parser.add_argument(
    "--no-fast",
    action="store_true",
    help="leave the fast tier (feature fast-wasm, off until the page turns it on) out of the coupled core",
)
parser.add_argument(
    "--out",
    help="output path of the coupled SHARC+ core (default packages/web/.local/emulator-core-sharc.wasm)",
)
args = parser.parse_args()
configured_generated = os.environ.get("SHARC_GEN_DIR")
generated = configured_generated or str(digi_env.DEFAULT_GENERATED)
if generated and not pathlib.Path(generated).is_dir():
    if configured_generated or args.sharc:
        raise SystemExit("SHARC_GEN_DIR must name a local generated DN2 core directory")
    generated = None
if args.sharc and args.cf_only:
    raise SystemExit("--sharc and --cf-only cannot be used together")
coupled = bool(generated) and not args.cf_only
if coupled:
    digi_env.check_generated(pathlib.Path(generated))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import native_target  # noqa: E402


def resolve_target_dir(gen: str | None) -> pathlib.Path:
    # Shared target dir keyed by the generated core (see tools/native_target.py).
    # CARGO_TARGET_DIR still wins for both builds; with it set the two builds
    # share one dir, which works but rebuilds the crate each time.
    path = pathlib.Path(os.environ.get("CARGO_TARGET_DIR") or native_target.target_dir(gen))
    return path if path.is_absolute() else root / path


rustc = digi_env.toolchain_rustc()
if not rustc:
    rustc = shutil.which("rustc")
    if rustc is not None:
        print(
            f"WARNING: pinned rustup toolchain {digi_env.TOOLCHAIN} not found; "
            "using the rustc on PATH",
            file=sys.stderr,
        )
if rustc is None:
    raise SystemExit("rustc is required; install the pinned mise Rust toolchain first")
sysroot = subprocess.check_output([rustc, "--print", "sysroot"], text=True).strip()
base_env = os.environ | {
    "PATH": f"{pathlib.Path(rustc).parent}:{os.environ['PATH']}",
    "DYLD_FALLBACK_LIBRARY_PATH": f"{sysroot}/lib",
    "RUSTC": rustc,
    "RUSTDOC": str(pathlib.Path(rustc).with_name("rustdoc")),
}
public = root / "packages/web/public"
public.mkdir(parents=True, exist_ok=True)
local_dir = root / "packages/web/.local"
local_dir.mkdir(parents=True, exist_ok=True)
stale = public / "emulator-core-sharc.wasm"
if stale.exists():
    stale.unlink()
    print(f"removed stale {stale} (the coupled core now lives in packages/web/.local)")


def build(features: list[str], target_dir: pathlib.Path, env: dict[str, str], dest: pathlib.Path) -> None:
    command = [
        "cargo",
        "build",
        "--manifest-path",
        "native/boot/Cargo.toml",
        "--locked",
        "--offline",
        "--release",
        "--lib",
        "--target",
        "wasm32-unknown-unknown",
        "--target-dir",
        str(target_dir),
    ]
    if features:
        command.extend(["--features", ",".join(features)])
    try:
        subprocess.run(command, cwd=root, env=env, check=True)
    except subprocess.CalledProcessError:
        raise SystemExit(
            "WASM build failed; install wasm32-unknown-unknown for the pinned Rust toolchain"
        ) from None
    shutil.copyfile(target_dir / "wasm32-unknown-unknown/release/elektron_native_boot.wasm", dest)


# The plain core has no fast tier. The coupled core has one (feature fast-wasm,
# off until the page calls digi_sharc_fast or digi_sharc_fast_regions): it
# reads no SHARC_FAST_REGIONS (wasm32-unknown-unknown has no environment) and
# its kernels are wasm modules the page compiles (packages/web/src/core-host.ts).
diagnostics = ["diagnostic-profile", "diagnostic-events"] if args.diagnostics else []
plain_env = {k: v for k, v in base_env.items() if k != "SHARC_GEN_DIR"}
build(diagnostics, resolve_target_dir(None), plain_env, public / "emulator-core.wasm")
sharc_wasm = pathlib.Path(args.out) if args.out else local_dir / "emulator-core-sharc.wasm"
if coupled:
    build(
        [*diagnostics, "sharc", *([] if args.no_fast else ["fast-wasm"])],
        resolve_target_dir(generated),
        base_env | {"SHARC_GEN_DIR": generated},
        sharc_wasm,
    )
else:
    sharc_wasm.unlink(missing_ok=True)
    print("coupled SHARC+ core skipped (--cf-only or no generated core); removed any stale emulator-core-sharc.wasm")
