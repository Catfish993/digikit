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
# The DN2 ready-inputs directory: ready_dir in tools/ready_inputs.json (shared
# with the JS tools), overridden by DIGI_EMU_READY_DIR.
_READY_JSON = json.loads((ROOT / "tools" / "ready_inputs.json").read_text())
READY = ROOT / (os.environ.get("DIGI_EMU_READY_DIR") or _READY_JSON["ready_dir"])
# The ready directory of the other devices (`ready_dirs` in the same file).
READY_DIRS = {
    "dn2": READY,
    **{k: ROOT / v for k, v in _READY_JSON.get("ready_dirs", {}).items()},
}
DEFAULT_GENERATED = ROOT / "out" / "native" / "dn2-audio" / "gen"
DEFAULT_SYX = ROOT / "Digitone_II_OS1.11.syx"
FAST_TABLE = ROOT / "tools" / "fast_regions.json"
TOOLCHAIN: str = tomllib.loads((ROOT / "mise.toml").read_text())["tools"]["rust"]
TRIPLE = "aarch64-apple-darwin"

Environ = dict[str, str] | os._Environ[str]


GEN_INDEX = ROOT / "out" / "native" / "gen-index.json"


def _image_sha(image: pathlib.Path | str) -> str:
    return hashlib.sha256(pathlib.Path(image).read_bytes()).hexdigest()


def gen_for_image(image: pathlib.Path | str) -> pathlib.Path | None:
    """The generated core (block library) made for a packed DSP image, found by
    the image's SHA-256 in out/native/gen-index.json; None when none is
    registered (the image then runs interpreted on any core)."""
    try:
        index = json.loads(GEN_INDEX.read_text())
        found = pathlib.Path(index[_image_sha(image)])
    except (OSError, KeyError, ValueError):
        return None
    return found if found.is_dir() else None


def register_gen(image: pathlib.Path | str, gen: pathlib.Path | str) -> None:
    """Record GEN as the core for IMAGE (tools/aot_gen.py)."""
    try:
        index = json.loads(GEN_INDEX.read_text())
    except (OSError, ValueError):
        index = {}
    index[_image_sha(image)] = str(pathlib.Path(gen).resolve())
    GEN_INDEX.parent.mkdir(parents=True, exist_ok=True)
    GEN_INDEX.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")


def generated_dir(
    environ: Environ | None = None, image: pathlib.Path | str | None = None
) -> pathlib.Path:
    """The generated core to build against: SHARC_GEN_DIR, else the one
    registered for IMAGE (a packed DSP image), else the DN2 prod default."""
    environ = os.environ if environ is None else environ
    configured = environ.get("SHARC_GEN_DIR")
    if configured:
        return pathlib.Path(configured)
    if image is not None and (registered := gen_for_image(image)) is not None:
        return registered
    return DEFAULT_GENERATED


def firmware(environ: Environ | None = None) -> pathlib.Path:
    """The DN2 .syx: DIGI_EMU_SYX, then DIGI_COUPLED_SYX, then the repo-root default."""
    environ = os.environ if environ is None else environ
    return pathlib.Path(
        environ.get("DIGI_EMU_SYX") or environ.get("DIGI_COUPLED_SYX") or DEFAULT_SYX
    )


def device_for_syx(path: pathlib.Path) -> str | None:
    """The device short id (`dn2`, `dt2`) of a firmware file, by SHA-256 against
    devices/*.toml; None when no profile knows it."""
    try:
        digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    for toml_path in sorted((ROOT / "devices").glob("*.toml")):
        doc = tomllib.loads(toml_path.read_text())
        if any(f.get("sha256") == digest for f in doc.get("firmware", [])):
            return doc["device"]["short"]
    return None


def firmware_for(device: str, environ: Environ | None = None) -> pathlib.Path:
    """The .syx of a device: DIGI_EMU_SYX_<DEVICE>, else the repo-root default
    (DN2 also honours DIGI_EMU_SYX and DIGI_COUPLED_SYX, see `firmware`)."""
    environ = os.environ if environ is None else environ
    if device == "dn2":
        return firmware(environ)
    default = {"dt2": "Digitakt_II_OS1.16.syx"}[device]
    return pathlib.Path(environ.get(f"DIGI_EMU_SYX_{device.upper()}") or ROOT / default)


def ready_inputs(
    environ: Environ | None = None, device: str = "dn2"
) -> dict[str, pathlib.Path]:
    """The private audio inputs of a device, each overridable by its DIGI_EMU_*
    variable (DN2)."""
    environ = os.environ if environ is None else environ
    if device != "dn2":
        base = READY_DIRS[device]
        return {
            "firmware": firmware_for(device, environ),
            "DSP image": base / "digi-audio-dn2-image.bin",
            "DSP state": base / "digi-audio-m5.snap.dsp",
            "CF snapshot": base / "digi-audio-m5.snap",
        }
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
