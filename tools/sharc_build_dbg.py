#!/usr/bin/env python3
"""Build coupled-run with line tables (same code generation) into its own
target dir, for address-level profiles (xctrace + atos). Prints the binary.

    uv run python tools/sharc_build_dbg.py
"""

import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import coupled  # noqa: E402
import digi_env  # noqa: E402
import native_target  # noqa: E402

gen = coupled.generated()
target = native_target.target_dir(gen, variant="dbg")
env = {**digi_env.rust_env(), "SHARC_GEN_DIR": str(gen), "CARGO_PROFILE_RELEASE_DEBUG": "line-tables-only"}
cmd = coupled.cargo(
    [
        "build", "--release", "--locked",
        "--manifest-path", "native/coupled/Cargo.toml",
        "--features", coupled.FEATURES,
        "--bin", coupled.BINARY_NAME,
        "--target-dir", str(target),
    ]
)  # fmt: skip
subprocess.run(cmd, cwd=ROOT, env=env, check=True)
print(target / "release" / coupled.BINARY_NAME)
os.sync()
