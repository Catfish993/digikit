#!/usr/bin/env python3
"""Shared configuration for the native build, launch, PGO and run tools.

One place for the pinned Rust toolchain (read from mise.toml), the default
paths of the private inputs, the firmware file, and the per-firmware fast-tier
tables (tools/fast_regions.json). tools/native_emu.sh, native_pgo.py,
native_target.py, native_wasm.sh and coupled.py import it.

    uv run python tools/digi_env.py        # print the resolved settings
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
READY = ROOT / "snapshots" / "dn2-audio-ready-2026-10-03"
DEFAULT_GENERATED = ROOT / "out" / "native" / "dn2-audio" / "gen"
DEFAULT_SYX = ROOT / "Digitone_II_OS1.11.syx"
FAST_TABLE = ROOT / "tools" / "fast_regions.json"
TOOLCHAIN: str = tomllib.loads((ROOT / "mise.toml").read_text())["tools"]["rust"]
TRIPLE = "aarch64-apple-darwin"

Environ = dict[str, str] | os._Environ[str]


def generated_dir(environ: Environ | None = None) -> pathlib.Path:
    """The generated DN2 core: SHARC_GEN_DIR, else the prod default."""
    environ = os.environ if environ is None else environ
    configured = environ.get("SHARC_GEN_DIR")
    return pathlib.Path(configured) if configured else DEFAULT_GENERATED


def firmware(environ: Environ | None = None) -> pathlib.Path:
    """The DN2 .syx: DIGI_EMU_SYX, then DIGI_COUPLED_SYX, then the repo-root default."""
    environ = os.environ if environ is None else environ
    return pathlib.Path(
        environ.get("DIGI_EMU_SYX") or environ.get("DIGI_COUPLED_SYX") or DEFAULT_SYX
    )


def ready_inputs(environ: Environ | None = None) -> dict[str, pathlib.Path]:
    """The private DN2 audio inputs, each overridable by its DIGI_EMU_* variable."""
    environ = os.environ if environ is None else environ
    return {
        "firmware": firmware(environ),
        "DSP image": pathlib.Path(
            environ.get("DIGI_EMU_DSP_IMAGE", READY / "digi-audio-dn2-image.bin")
        ),
        "DSP state": pathlib.Path(
            environ.get("DIGI_EMU_DSP_STATE", READY / "digi-audio-m5.snap.dsp")
        ),
        "CF snapshot": pathlib.Path(
            environ.get("DIGI_EMU_CF_SNAPSHOT", READY / "digi-audio-m5.snap")
        ),
    }


@functools.cache
def _syx_digest(path: str) -> str:
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def fast_table() -> dict[str, dict[str, str]]:
    table = json.loads(FAST_TABLE.read_text())
    return {k: v for k, v in table.items() if not k.startswith("_")}


def _entry(syx: pathlib.Path) -> dict[str, str]:
    return fast_table().get(_syx_digest(str(syx)), {})


def fast_regions_for(syx: pathlib.Path) -> str:
    """The verified fast regions for this firmware file, or "" if unknown."""
    return _entry(syx).get("fast_regions", "")


def blocktrans_roots_for(syx: pathlib.Path) -> str:
    """The verified block-translator cluster roots for this firmware, or ""."""
    return _entry(syx).get("blocktrans", "")


def fast_env(environ: Environ | None = None) -> dict[str, str]:
    """SHARC_FAST_REGIONS / SHARC_BLOCKTRANS for a coupled run.

    User-set values win; DIGI_EMU_FAST=0 leaves both off; otherwise the table
    entry for the firmware. Keys with nothing to set are left out.
    """
    environ = os.environ if environ is None else environ
    out: dict[str, str] = {}
    if environ.get("DIGI_EMU_FAST") == "0":
        return out
    syx = firmware(environ)
    for name, lookup in (
        ("SHARC_FAST_REGIONS", fast_regions_for),
        ("SHARC_BLOCKTRANS", blocktrans_roots_for),
    ):
        if name in environ:
            out[name] = environ[name]
        elif value := lookup(syx):
            out[name] = value
    return out


@functools.cache
def toolchain_rustc() -> str:
    """The pinned rustup toolchain's rustc ("" when rustup cannot find it).

    Cargo otherwise takes `rustc` from PATH; a Homebrew rustc of the same
    version mangles crates differently, so PGO profiles silently stop matching.
    """
    try:
        done = subprocess.run(
            ["rustup", "which", "--toolchain", TOOLCHAIN, "rustc"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def rust_env(environ: Environ | None = None) -> dict[str, str]:
    """A copy of the environment with RUSTC/RUSTDOC pinned to the toolchain."""
    environ = os.environ if environ is None else environ
    env = dict(environ)
    rustc = toolchain_rustc()
    if rustc:
        env["RUSTC"] = rustc
        env["RUSTDOC"] = str(pathlib.Path(rustc).with_name("rustdoc"))
        env["PATH"] = f"{pathlib.Path(rustc).parent}:{env.get('PATH', '')}"
    else:
        print(
            f"WARNING: rustup toolchain {TOOLCHAIN} not found; cargo will use the "
            "rustc on PATH (PGO profiles may not match)",
            file=sys.stderr,
        )
    return env


def main() -> int:
    print(f"toolchain   {TOOLCHAIN} ({toolchain_rustc() or 'not installed'})")
    print(f"generated   {generated_dir()}")
    for label, path in ready_inputs().items():
        print(f"{label:<11} {path} ({'ok' if path.is_file() else 'missing'})")
    for key, value in fast_env().items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
