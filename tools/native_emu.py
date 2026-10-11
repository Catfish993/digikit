#!/usr/bin/env python3
"""Build the shared panel and the desktop emulator once, then run the binary.

With local DN2 ready inputs and a generated core, the bare launcher restores
the audible DN2 session. The private inputs stay out of the repository: every
path is overridable, and unavailable inputs leave the ordinary CF diagnostic
emulator available.

The panel is rebuilt only when its sources changed (DIGI_EMU_PANEL=1 forces
it). The app is built once with cargo build and the binary is run directly;
--build-only stops after the build and prints the binary path. A stale PGO
profile is used with a warning; `mise run pgo` retrains it (or launch once
with DIGI_EMU_PGO_TRAIN=1).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import digi_env  # noqa: E402
finally:
    sys.path.pop(0)

# Fast-tier regions (SHARC_FAST_REGIONS) and block-translator cluster roots
# (SHARC_BLOCKTRANS), keyed by the SHA-256 of the DN2 .syx. The data lives in
# tools/fast_regions.json.
FAST_REGIONS = {k: v.get("fast_regions", "") for k, v in digi_env.fast_table().items()}
BLOCKTRANS_ROOTS = {
    k: v.get("blocktrans", "") for k, v in digi_env.fast_table().items()
}
PANEL_STAMP = ROOT / "out" / "native" / "panel-stamp.json"
PANEL_INPUTS = (
    "packages/web/src",
    "packages/web/public",
    "packages/web/package.json",
    "packages/web/astro.config.mjs",
    "pnpm-lock.yaml",
)


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
        else digi_env.firmware(environ)
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


def generated_core(args: list[str] | None = None) -> pathlib.Path:
    """The generated core for a launch: SHARC_GEN_DIR, else the one registered
    for the --dsp-image in ARGS (tools/aot_gen.py), else the DN2 default."""
    image = None
    if args and "--dsp-image" in args:
        position = args.index("--dsp-image") + 1
        image = args[position] if position < len(args) else None
    return digi_env.generated_dir(image=image)


def default_audio_args(args: list[str] | None = None) -> list[str]:
    """Add local ready audio inputs to a normal launcher command line."""
    args = [] if args is None else list(args)
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
    syx = pathlib.Path(positional) if positional else digi_env.firmware()
    device = digi_env.device_for_syx(syx) or "dn2"
    paths = digi_env.ready_inputs(device=device)
    if positional and device != "dn2":
        paths["firmware"] = syx
    card = (
        pathlib.Path(args[args.index("--card-image") + 1])
        if "--card-image" in args and args.index("--card-image") + 1 < len(args)
        else None
    )
    if device != "dn2" and (card is not None or not paths["CF snapshot"].is_file()):
        paths = ready_for(device, syx, card, paths)
    missing = [label for label, path in paths.items() if not path.is_file()]
    generated = digi_env.generated_dir(image=paths["DSP image"])
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
    digi_env.check_generated(generated)
    os.environ.setdefault("SHARC_GEN_DIR", str(generated))
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


def ready_for(
    device: str,
    syx: pathlib.Path,
    card: pathlib.Path | None,
    paths: dict[str, pathlib.Path],
) -> dict[str, pathlib.Path]:
    """The ready inputs of a device whose snapshot depends on the card (DT2:
    the sample list is in the restored RAM), built from the .syx alone by
    tools/ready.py on first use and reused after."""
    command = [
        sys.executable,
        str(ROOT / "tools" / "ready.py"),
        device,
        "--syx",
        str(syx),
    ]
    if card is not None:
        command += ["--card-image", str(card)]
    print(
        f"Preparing the {device.upper()} audio state for this card (first time only)...",
        file=sys.stderr,
    )
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    sys.stderr.write(done.stderr[-2000:])
    ready_dir = next(
        (
            line.partition("=")[2]
            for line in done.stdout.splitlines()
            if line.startswith("ready_dir=")
        ),
        None,
    )
    if done.returncode or ready_dir is None:
        print(
            f"tools/ready.py {device} failed:\n{done.stdout[-2000:]}", file=sys.stderr
        )
        return paths
    base = pathlib.Path(ready_dir)
    return {
        **paths,
        "DSP image": base / "digi-audio-dn2-image.bin",
        "DSP state": base / "digi-audio-m5.snap.dsp",
        "CF snapshot": base / "digi-audio-m5.snap",
    }


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
            command.extend(["--target-dir", target_dir_for(generated_core(args))])
    else:
        command.extend(["--target-dir", target_dir_for(None)])
    return [*command, "--", *args]


def device_for_args(args: list[str]) -> str:
    """The device a coupled launch runs (`dn2`, `dt2`), by its firmware file."""
    if "--audio-profile-syx" in args:
        position = args.index("--audio-profile-syx") + 1
        syx = pathlib.Path(args[position]) if position < len(args) else None
    else:
        syx = digi_env.firmware()
    return (digi_env.device_for_syx(syx) if syx else None) or "dn2"


def train_profile(device: str = "dn2") -> str:
    """Run `native_pgo.py train`; returns "" on success, else why it failed."""
    try:
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "native_pgo.py"),
                "train",
                *(["--device", device] if device != "dn2" else []),
            ],
            cwd=ROOT,
            check=False,
        )
    except OSError as exc:
        return f"could not start training: {exc}"
    return f"training failed (exit {done.returncode})" if done.returncode else ""


def pgo_plan(args: list[str]) -> object | None:
    """Profile-use plan for the coupled build, or None for the ordinary build.

    Each device has its own profile (`native_pgo.py train --device dt2`): a DN2
    profile gives a DT2 build only the shared-engine 3%, a DT2 profile 19%.
    The launcher never trains on its own. A stale profile is used anyway with a
    WARNING (unchanged functions keep their profile); a missing one falls back
    to the ordinary build. DIGI_EMU_PGO_TRAIN=1 trains a stale or missing
    profile here when the training inputs exist (`mise run pgo` is the usual
    way); DIGI_EMU_PGO=0 skips PGO. Any failure falls back to the ordinary
    build with a WARNING line: launching is never blocked.
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
    device = device_for_args(args)
    status, detail = native_pgo.pgo_state(environ, device)
    if status == "off":
        return None
    if status in ("stale", "missing"):
        if environ.get("DIGI_EMU_PGO_TRAIN") == "1":
            blocker = native_pgo.training_blocker(environ, device)
            if blocker:
                detail = f"{detail}; cannot train: {blocker}"
            else:
                print(
                    f"native PGO: profile {status} ({detail}); training (~6 min, "
                    "DIGI_EMU_PGO_TRAIN=1)",
                    file=sys.stderr,
                    flush=True,
                )
                failure = train_profile(device)
                if failure:
                    status, detail = "failed", failure
                else:
                    status, detail = native_pgo.pgo_state(environ, device)
        elif status == "stale":
            profile = native_pgo.current_profile(device)
            if profile:
                print(native_pgo.stale_notice(detail, device), file=sys.stderr)
                return native_pgo.plan_for(profile, environ)
        else:
            print(native_pgo.fallback_notice(status, detail, device), file=sys.stderr)
            return None
    if status != "ok":
        print(native_pgo.fallback_notice(status, detail, device), file=sys.stderr)
        return None
    print(
        f"native PGO: using {device.upper() + ' ' if device != 'dn2' else ''}profile "
        + pathlib.Path(detail).name,
        file=sys.stderr,
    )
    return native_pgo.plan_for(detail, environ)


PGO_MISMATCH_WARNING = (
    "WARNING: PGO profile does not match this binary (crate hashes differ); "
    "live DSP will run ~35% slower. Run tools/native_pgo.py train."
)
PGO_MISMATCH_WARNING_DT2 = PGO_MISMATCH_WARNING.replace("train.", "train --device dt2.")


def build_command(command: list[str]) -> list[str]:
    """The `cargo run` command as a `cargo build` of the same binary."""
    cut = command.index("--")
    head = command[:cut]
    head[head.index("cargo") + 1] = "build"
    return head


def verify_pgo(command: list[str], env: dict[str, str], device: str = "dn2") -> None:
    """Build, then warn loudly if the profile's crate hashes miss the binary.

    A failed build stops the launch; a failed check only warns.
    """
    built = subprocess.run(build_command(command), cwd=ROOT, env=env)
    if built is not None and built.returncode:
        raise SystemExit(built.returncode)
    try:
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "native_pgo.py"),
                "verify",
                "--device",
                device,
                *(
                    ["--binary", str(app_binary(command, True))]
                    if "--target-dir" in command
                    else []
                ),
            ],
            cwd=ROOT,
            check=False,
        )
    except (OSError, ValueError):
        return
    if done.returncode == 1:
        warning = PGO_MISMATCH_WARNING_DT2 if device == "dt2" else PGO_MISMATCH_WARNING
        print(warning, file=sys.stderr, flush=True)


def daemon_command(args: list[str]) -> list[str]:
    """`cargo run` for the windowless daemon `digi-emud` (native/control).

    Same target-dir rules as the desktop app, so a coupled daemon and the app
    share one compiled copy of the generated core. No PGO: the profile is
    trained on the desktop binary.
    """
    command = [
        "cargo",
        "run",
        "--release",
        "--locked",
        "--manifest-path",
        "native/control/Cargo.toml",
        "--bin",
        "digi-emud",
    ]
    if "--coupled" in args or "--auto-coupled" in args:
        command.extend(
            [
                "--features",
                "coupled-audio",
                "--target-dir",
                target_dir_for(generated_core(args)),
            ]
        )
    else:
        command.extend(["--target-dir", target_dir_for(None)])
    return [*command, "--", *args]


def app_binary(command: list[str], plan: object | None) -> pathlib.Path:
    """The binary the build command produces (digiemu-desktop, or digi-emud)."""
    target = pathlib.Path(command[command.index("--target-dir") + 1])
    name = (
        command[command.index("--bin") + 1]
        if "--bin" in command[: command.index("--")]
        else "digiemu-desktop"
    )
    if plan is not None:
        return target / digi_env.TRIPLE / "release" / name
    return target / "release" / name


def panel_digest() -> str:
    """Digest of the panel's sources (the browser WASM core is left out)."""
    outer = hashlib.sha256()
    for rel in PANEL_INPUTS:
        top = ROOT / rel
        if top.is_file():
            files = [top]
        else:
            files = sorted(p for p in top.rglob("*") if p.is_file())
        for path in files:
            if path.name.startswith("emulator-core") and path.suffix == ".wasm":
                continue
            outer.update(f"{path.relative_to(ROOT)}\0".encode())
            outer.update(hashlib.sha256(path.read_bytes()).digest())
    return outer.hexdigest()


def build_panel(environ: dict[str, str] | os._Environ[str]) -> None:
    """`astro build` the shared panel unless its sources match the last build.

    DIGI_EMU_PANEL=1 forces the build.
    """
    digest = panel_digest()
    dist = ROOT / "packages" / "web" / "dist"
    try:
        same = json.loads(PANEL_STAMP.read_text()).get("digest") == digest
    except (OSError, ValueError):
        same = False
    if same and dist.is_dir() and environ.get("DIGI_EMU_PANEL") != "1":
        return
    subprocess.run(
        ["pnpm", "--filter", "@digi/web", "exec", "astro", "build"],
        cwd=ROOT,
        check=True,
    )
    PANEL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    PANEL_STAMP.write_text(json.dumps({"digest": digest}) + "\n")


def plusdrive_card(
    samples: pathlib.Path, syx: pathlib.Path, env: dict[str, str]
) -> pathlib.Path:
    """Build (or reuse) a +Drive card image from a folder of WAV files.

    Uses the native `plusdrive build` (native/plusdrive). The image lives in
    out/plusdrive/native/<key12>/dt2.img, keyed by the SHA-256 of the firmware,
    the plusdrive binary and every sample's name and bytes, so unchanged
    inputs reuse the image.
    """
    if not samples.is_dir():
        raise SystemExit(f"--samples: {samples} is not a directory")
    names = sorted(
        p.name for p in samples.iterdir() if p.suffix.lower() == ".wav" and p.is_file()
    )
    if not names:
        raise SystemExit(f"--samples: no .wav files directly in {samples}")
    manifest = ROOT / "native" / "plusdrive" / "Cargo.toml"
    # Share the CF-only target dir with the desktop app: one dependency build.
    target = target_dir_for(None)
    done = subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(manifest),
            "--bin",
            "plusdrive",
            "--target-dir",
            target,
        ],
        cwd=ROOT,
        env=env,
    )
    if done.returncode:
        raise SystemExit(done.returncode)
    binary = pathlib.Path(target) / "release" / "plusdrive"
    h = hashlib.sha256()
    h.update(b"firmware\0" + hashlib.sha256(syx.read_bytes()).digest())
    h.update(b"plusdrive\0" + hashlib.sha256(binary.read_bytes()).digest())
    for name in names:
        h.update(b"sample\0" + name.encode() + b"\0")
        h.update(hashlib.sha256((samples / name).read_bytes()).digest())
    key = h.hexdigest()[:12]
    image = ROOT / "out" / "plusdrive" / "native" / key / "dt2.img"
    if image.is_file():
        print(f"+Drive card: reusing {image} ({len(names)} sample(s))", file=sys.stderr)
        return image
    image.parent.mkdir(parents=True, exist_ok=True)
    partial = image.with_suffix(".part")
    partial.unlink(missing_ok=True)
    done = subprocess.run(
        [str(binary), "build", str(samples), "-o", str(partial), "--syx", str(syx)],
        cwd=ROOT,
        env=env,
    )
    if done.returncode:
        partial.unlink(missing_ok=True)
        raise SystemExit(done.returncode)
    partial.rename(image)
    print(f"+Drive card: built {image} ({len(names)} sample(s))", file=sys.stderr)
    return image


def main(args: list[str] | None = None) -> None:
    args = sys.argv[1:] if args is None else args
    force_cf = "--cf-only" in args
    build_only = "--build-only" in args
    daemon = "--daemon" in args
    args = [arg for arg in args if arg not in ("--cf-only", "--build-only", "--daemon")]
    if "--samples" in args:
        i = args.index("--samples")
        if i + 1 >= len(args):
            raise SystemExit("--samples needs a directory of .wav files")
        samples = pathlib.Path(args[i + 1])
        del args[i : i + 2]
        if "--card-image" in args:
            raise SystemExit("--samples and --card-image cannot be combined")
        value_options = {"--card-image", "--audio-buffer"}
        syx_arg = next(
            (
                v
                for j, v in enumerate(args)
                if not v.startswith("--")
                and (j == 0 or args[j - 1] not in value_options)
            ),
            None,
        )
        syx = pathlib.Path(syx_arg) if syx_arg else digi_env.firmware(os.environ)
        env = digi_env.rust_env(dict(os.environ))
        card = plusdrive_card(samples, syx, env)
        args = [*([] if syx_arg else [str(syx)]), *args, "--card-image", str(card)]
    if force_cf and ("--coupled" in args or "--auto-coupled" in args):
        raise SystemExit("--cf-only cannot be combined with coupled audio options")
    if not force_cf and "--coupled" not in args and "--auto-coupled" not in args:
        args = default_audio_args(args)
    if "--coupled" in args or "--auto-coupled" in args:
        generated = generated_core(args)
        if not generated.is_dir():
            raise SystemExit(
                "--coupled needs SHARC_GEN_DIR pointing to a local generated DN2 core"
            )
        digi_env.check_generated(generated)
        os.environ.setdefault("SHARC_GEN_DIR", str(generated))
        set_fast_regions(args, os.environ)
    # The native shell serves the built panel; it does not load browser WASM.
    # Avoid rebuilding the browser core (and its local DSP code) for a desktop run.
    # The daemon has no window: no panel, no PGO.
    if not daemon:
        build_panel(os.environ)
    plan = None if daemon else pgo_plan(args)
    env = digi_env.rust_env({**os.environ, **(plan.env if plan is not None else {})})  # type: ignore[attr-defined]
    command = daemon_command(args) if daemon else cargo_command(args, plan)
    if plan is not None:
        verify_pgo(command, env, device_for_args(args))
    else:
        done = subprocess.run(build_command(command), cwd=ROOT, env=env)
        if done is not None and done.returncode:
            raise SystemExit(done.returncode)
    binary = app_binary(command, plan)
    if build_only:
        print(binary)
        return
    subprocess.run([str(binary), *args], cwd=ROOT, check=True, env=env)


if __name__ == "__main__":
    main()
