#!/usr/bin/env python3
"""The V8 gate of the wasm kernel backend (packages/emulator/sharc-fast-wasm).

Captures real fast-tier kernel calls from a coupled run (SHARC_FAST_DUMP,
packages/emulator/sharc/src/fast/dump.rs), turns the kernels into WebAssembly, replays
every call through Cranelift (native) and through V8 (Node), checks the
results against the recorded ones and prints ns per instruction and compile
time. The gate is <= 1.2 ns per instruction.

    uv run python -m tools.devtools.wasm_gate [capture] [build] [run] [--seconds 1.5] [--budget-ms 20]
    mise run wasm:gate

With no step named, all three run. Everything lands in out/native/wasmgate/
(dump/, wasm/, results.txt). Needs the DN2 firmware and ready inputs like
tools/devtools/fastcov.py (DIGI_EMU_SYX), the shared core (`mise run core`, made here if
missing), node, and the pinned Rust toolchain.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools import python_env  # noqa: E402
from tools.emulator import digi_env, native_target  # noqa: E402

OUT = ROOT / "out" / "native" / "wasmgate"
DUMP = OUT / "dump"
WASM = OUT / "wasm"
CRATE = ROOT / "native" / "sharc-fast-wasm"
CORE = ROOT / "out" / "native" / "shared-core" / "gen"
TARGET = native_target.TARGET_ROOT / "wasmgate"
V8_MODES = [
    ("TurboFan only", ["--no-liftoff", "--no-wasm-lazy-compilation"]),
    ("Liftoff only", ["--liftoff-only", "--no-wasm-lazy-compilation"]),
    ("default tiering", ["--no-wasm-lazy-compilation"]),
]


def sh(cmd, **kw):
    return subprocess.run(
        cmd,
        cwd=kw.pop("cwd", ROOT),
        env=python_env(kw.pop("env", None)),
        check=True,
        **kw,
    )


def capture(seconds: float) -> None:
    """Run the static-blocks+follow coverage scenario with the dump on."""
    shutil.rmtree(DUMP, ignore_errors=True)
    env = {**os.environ, "SHARC_FAST_DUMP": str(DUMP)}
    sh(
        [sys.executable, "-m", "tools.emulator.coupled", "build"],
        env=env,
        stdout=subprocess.DEVNULL,
    )
    sh(
        [
            sys.executable, "-m", "tools.devtools.fastcov", "--no-build", "--no-lock", "--diag", "no",
            "--seconds", str(seconds), "--config", "static-blocks+follow",
            "--out", str(OUT / "fastcov"),
        ],
        env=env,
    )  # fmt: skip
    rows = [l.split("\t") for l in (DUMP / "totals.tsv").read_text().splitlines()]  # noqa: E741
    calls = [l.split("\t") for l in (DUMP / "calls.tsv").read_text().splitlines()]  # noqa: E741
    total = sum(int(r[2]) for r in rows)
    caught = {c[1] for c in calls}
    covered = sum(int(r[2]) for r in rows if r[0] in caught)
    print(
        f"captured {len(calls)} calls of {len(caught)} regions, "
        f"{100 * covered / max(total, 1):.1f}% of fast-tier instructions"
    )


def build() -> pathlib.Path:
    if not (CORE / "core_g.rs").is_file():
        sh([sys.executable, "-m", "tools.sharc_dev.sharc_transpile", "--strict", "--out", str(CORE),
            "--work", str(CORE.parent / "work")], stdout=subprocess.DEVNULL)  # fmt: skip
    env = {
        **digi_env.rust_env(),
        "SHARC_GEN_DIR": str(CORE),
        "CARGO_TARGET_DIR": str(TARGET),
    }
    sh(
        ["rustup", "run", digi_env.TOOLCHAIN, "cargo", "build", "--release", "--offline",
         "--features", "native-bench"],
        cwd=CRATE,
        env=env,
    )  # fmt: skip
    return TARGET / "release"


def run(bin_dir: pathlib.Path, budget: float) -> str:
    WASM.mkdir(parents=True, exist_ok=True)
    out = []
    gen = sh(
        [str(bin_dir / "wasmgen"), str(DUMP), str(WASM)], capture_output=True, text=True
    )
    out.append(gen.stdout.strip())
    cl = sh(
        [str(bin_dir / "clbench"), str(DUMP), str(budget)],
        capture_output=True,
        text=True,
    )
    cl_lines = cl.stdout.strip().splitlines()
    out.append("Cranelift (native): " + cl_lines[-1])
    for name, flags in V8_MODES:
        r = sh(
            [
                "node",
                *flags,
                str(CRATE / "gate.mjs"),
                str(DUMP),
                str(WASM),
                str(budget),
            ],
            capture_output=True,
            text=True,
        )
        lines = r.stdout.strip().splitlines()
        out.append(f"V8 {name}: {lines[0]}")
        out.append(f"V8 {name}: {lines[-1]}")
    text = "\n".join(out)
    m = re.search(r"TOTAL wasm/V8: \d+ insns, ([0-9.]+) ns/insn", text)
    gate = float(m[1]) if m else 99.0
    text += f"\ngate (<= 1.2 ns/insn, first V8 mode): {'PASS' if gate <= 1.2 else 'FAIL'} ({gate:.3f})"
    (OUT / "results.txt").write_text(text + "\n")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("steps", nargs="*", choices=["capture", "build", "run"])
    ap.add_argument("--seconds", type=float, default=1.5)
    ap.add_argument("--budget-ms", type=float, default=20)
    args = ap.parse_args()
    steps = args.steps or ["capture", "build", "run"]
    OUT.mkdir(parents=True, exist_ok=True)
    if "capture" in steps:
        capture(args.seconds)
    bin_dir = build() if ("build" in steps or "run" in steps) else None
    if "run" in steps:
        print(run(bin_dir, args.budget_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
