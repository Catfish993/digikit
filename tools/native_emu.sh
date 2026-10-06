#!/usr/bin/env python3
"""Build the shared panel and run the native desktop emulator.

With local DN2 ready inputs and a generated core, the bare launcher restores
the audible DN2 session. The private inputs stay out of the repository: every
path is overridable, and unavailable inputs leave the ordinary CF diagnostic
emulator available.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
READY = ROOT / "snapshots" / "dn2-audio-ready-2026-10-03"
DEFAULT_GENERATED = ROOT / "out" / "native" / "dn2-audio" / "gen"


# Fast-tier regions (DSP entry PCs, comma separated hex) verified exact for a
# firmware, keyed by the SHA-256 of the DN2 .syx whose DSP they belong to.
# Addresses only; they are not firmware content.
FAST_REGIONS = {
    # Digitone II OS 1.11
    "2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0": (
        "0x1c399a,0x1c3862,0x1c364f,0x1c5765,0xb823db,0x1c1884,0x1cdcef,0x1c3030,0xb85347,0x1c1da8"
    ),
}


# Block-translator cluster roots (SHARC_BLOCKTRANS) verified exact, same key.
# Measured A/B 0.958 idle, 0.963 chord16 (DSP CPU per audio s, non-PGO).
BLOCKTRANS_ROOTS = {
    # Digitone II OS 1.11
    "2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0": (
        "0xb8276c,0x1c369b,0xb809f2,0x1c5796,0xb84a8c,0xb8212b,"
        "0xb827b7,0x1c5110,0xb82568,0xb813cf,0xb8040f,0xb826a5"
    ),
}


def _lookup(table: dict[str, str], syx: pathlib.Path) -> str:
    try:
        digest = hashlib.sha256(syx.read_bytes()).hexdigest()
    except OSError:
        return ""
    return table.get(digest, "")


def fast_regions_for(syx: pathlib.Path) -> str:
    """The verified fast regions for this firmware file, or "" if unknown."""
    return _lookup(FAST_REGIONS, syx)


def blocktrans_roots_for(syx: pathlib.Path) -> str:
    """The verified block-translator cluster roots for this firmware, or ""."""
    return _lookup(BLOCKTRANS_ROOTS, syx)


def set_fast_regions(
    args: list[str], environ: dict[str, str] | os._Environ[str]
) -> None:
    """Enable the fast tier for a coupled run unless the user chose otherwise.

    SHARC_FAST_REGIONS (and SHARC_BLOCKTRANS) set by the user win;
    DIGI_EMU_FAST=0 leaves both off.
    """
    if environ.get("DIGI_EMU_FAST") == "0":
        return
    syx = (
        pathlib.Path(args[args.index("--audio-profile-syx") + 1])
        if "--audio-profile-syx" in args
        else pathlib.Path(environ.get("DIGI_EMU_SYX", ROOT / "Digitone_II_OS1.11.syx"))
    )
    if "SHARC_FAST_REGIONS" not in environ:
        regions = fast_regions_for(syx)
        if regions:
            environ["SHARC_FAST_REGIONS"] = regions
    # Cluster roots follow the same rules; a user-set SHARC_BLOCKTRANS wins.
    if "SHARC_BLOCKTRANS" not in environ:
        roots = blocktrans_roots_for(syx)
        if roots:
            environ["SHARC_BLOCKTRANS"] = roots


def generated_core() -> pathlib.Path:
    configured = os.environ.get("SHARC_GEN_DIR")
    return pathlib.Path(configured) if configured else DEFAULT_GENERATED


def default_audio_args(args: list[str] | None = None) -> list[str]:
    """Add local ready audio inputs to a normal launcher command line."""
    args = [] if args is None else list(args)
    paths = {
        "firmware": pathlib.Path(
            os.environ.get("DIGI_EMU_SYX", ROOT / "Digitone_II_OS1.11.syx")
        ),
        "DSP image": pathlib.Path(
            os.environ.get("DIGI_EMU_DSP_IMAGE", READY / "digi-audio-dn2-image.bin")
        ),
        "DSP state": pathlib.Path(
            os.environ.get("DIGI_EMU_DSP_STATE", READY / "digi-audio-m5.snap.dsp")
        ),
        "CF snapshot": pathlib.Path(
            os.environ.get("DIGI_EMU_CF_SNAPSHOT", READY / "digi-audio-m5.snap")
        ),
    }
    missing = [label for label, path in paths.items() if not path.is_file()]
    generated = generated_core()
    if not generated.is_dir():
        missing.append("generated DN2 core (set SHARC_GEN_DIR)")
    if missing:
        print(
            "Native audio is not set up (missing "
            + ", ".join(missing)
            + "); launching the CF-only diagnostic emulator. "
            "Use --cf-only to silence this notice.",
            file=sys.stderr,
        )
        return []
    os.environ.setdefault("SHARC_GEN_DIR", str(generated))
    value_options = {"--card-image", "--audio-buffer"}
    positional = next(
        (
            value
            for index, value in enumerate(args)
            if not value.startswith("--")
            and (index == 0 or args[index - 1] not in value_options)
        ),
        None,
    )
    if positional is None:
        args.insert(0, str(paths["firmware"]))
    args.extend(
        [
            "--auto-coupled",
            "--audio-profile-syx",
            str(paths["firmware"]),
            "--dsp-image",
            str(paths["DSP image"]),
            "--dsp-state",
            str(paths["DSP state"]),
            "--cf-snapshot",
            str(paths["CF snapshot"]),
        ]
    )
    # One second of prefill: below real time the DSP underruns at once with no buffer; pass --audio-buffer 0 for lowest latency.
    if "--audio-buffer" not in args:
        args.extend(["--audio-buffer", "1"])
    return args


def target_dir_for(gen: pathlib.Path | None) -> str:
    """The shared cargo target dir for a generated core (tools/native_target.py)."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import native_target
    finally:
        sys.path.pop(0)
    # "" means no generated core, whatever SHARC_GEN_DIR says.
    return str(native_target.target_dir(gen if gen is not None else ""))


def cargo_command(args: list[str], pgo: object | None = None) -> list[str]:
    """The cargo command line; pgo is a native_pgo.LaunchPlan for the PGO build.

    The target dir is keyed by the generated core, so every build of the same
    core (this app, tests, examples) reuses one compiled copy of it.
    """
    command = [
        "cargo",
        "run",
        "--release",
        "--locked",
        "--manifest-path",
        "packages/desktop/src-tauri/Cargo.toml",
    ]
    if "--coupled" in args or "--auto-coupled" in args:
        command.extend(["--features", "coupled-audio"])
        if pgo is not None:
            command = [*pgo.prefix, *command, *pgo.cargo_args]  # type: ignore[attr-defined]
        else:
            command.extend(["--target-dir", target_dir_for(generated_core())])
    else:
        command.extend(["--target-dir", target_dir_for(None)])
    return [*command, "--", *args]


def train_profile() -> str:
    """Run `native_pgo.py train`; returns "" on success, else why it failed."""
    try:
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "native_pgo.py"), "train"],
            cwd=ROOT,
            check=False,
        )
    except OSError as exc:
        return f"could not start training: {exc}"
    return f"training failed (exit {done.returncode})" if done.returncode else ""


def pgo_plan(args: list[str]) -> object | None:
    """Profile-use plan for the coupled build, or None for the ordinary build.

    A stale or missing profile is retrained here when the training inputs
    exist (DIGI_EMU_PGO_TRAIN=0 skips that); DIGI_EMU_PGO=0 skips PGO. Any
    failure falls back to the ordinary build with a WARNING line: launching is
    never blocked.
    """
    if "--coupled" not in args and "--auto-coupled" not in args:
        return None
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import native_pgo
    except ImportError:
        return None
    finally:
        sys.path.pop(0)
    environ = os.environ
    status, detail = native_pgo.pgo_state(environ)
    if status == "off":
        return None
    if status in ("stale", "missing") and environ.get("DIGI_EMU_PGO_TRAIN") != "0":
        blocker = native_pgo.training_blocker(environ)
        if blocker:
            detail = f"{detail}; cannot train: {blocker}"
        else:
            print(
                f"native PGO: profile {status} ({detail}); training (~6 min, "
                "DIGI_EMU_PGO_TRAIN=0 to skip)",
                file=sys.stderr,
                flush=True,
            )
            failure = train_profile()
            if failure:
                status, detail = "failed", failure
            else:
                status, detail = native_pgo.pgo_state(environ)
    if status != "ok":
        print(native_pgo.fallback_notice(status, detail), file=sys.stderr)
        return None
    print("native PGO: using profile " + pathlib.Path(detail).name, file=sys.stderr)
    return native_pgo.plan_for(detail, environ)


PGO_MISMATCH_WARNING = (
    "WARNING: PGO profile does not match this binary (crate hashes differ); "
    "live DSP will run ~35% slower. Run tools/native_pgo.py train."
)


def build_command(command: list[str]) -> list[str]:
    """The `cargo run` command as a `cargo build` of the same binary."""
    cut = command.index("--")
    head = command[:cut]
    head[head.index("cargo") + 1] = "build"
    return head


def verify_pgo(command: list[str], env: dict[str, str]) -> None:
    """Build, then warn loudly if the profile's crate hashes miss the binary.

    Never blocks the launch: any failure here is ignored and cargo run goes on.
    """
    try:
        if subprocess.run(build_command(command), cwd=ROOT, env=env).returncode:
            return
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "native_pgo.py"), "verify"],
            cwd=ROOT,
            check=False,
        )
    except (OSError, ValueError):
        return
    if done.returncode == 1:
        print(PGO_MISMATCH_WARNING, file=sys.stderr, flush=True)


def main(args: list[str] | None = None) -> None:
    args = sys.argv[1:] if args is None else args
    force_cf = "--cf-only" in args
    args = [arg for arg in args if arg != "--cf-only"]
    if force_cf and ("--coupled" in args or "--auto-coupled" in args):
        raise SystemExit("--cf-only cannot be combined with coupled audio options")
    if not force_cf and "--coupled" not in args and "--auto-coupled" not in args:
        args = default_audio_args(args)
    if "--coupled" in args or "--auto-coupled" in args:
        generated = generated_core()
        if not generated.is_dir():
            raise SystemExit(
                "--coupled needs SHARC_GEN_DIR pointing to a local generated DN2 core"
            )
        os.environ.setdefault("SHARC_GEN_DIR", str(generated))
        set_fast_regions(args, os.environ)
    # The native shell serves the built panel; it does not load browser WASM.
    # Avoid rebuilding the browser core (and its local DSP code) for a desktop run.
    subprocess.run(
        ["pnpm", "--filter", "@digi/web", "exec", "astro", "build"],
        cwd=ROOT,
        check=True,
    )
    plan = pgo_plan(args)
    env = {**os.environ, **plan.env} if plan is not None else None  # type: ignore[attr-defined]
    command = cargo_command(args, plan)
    if plan is not None:
        verify_pgo(command, env)  # type: ignore[arg-type]
    subprocess.run(command, cwd=ROOT, check=True, env=env)


if __name__ == "__main__":
    main()
