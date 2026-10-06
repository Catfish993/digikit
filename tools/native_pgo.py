#!/usr/bin/env python3
"""Profile-guided optimisation for the coupled DN2 native desktop build.

State lives under ``out/native/pgo/dn2/<host-triple>/`` (ignored by git):

    manifest.json          what the current profile was trained from
    profiles/<sha>.profdata  merged profile, content addressed
    bench-<time>/          copied binaries, logs, bench.tsv, summary.json

Cargo target dirs come from tools/native_target.py, keyed by the generated
core (``out/native/target/<gen-key>``): the benchmark control is the plain
release build in the key dir itself (shared with the desktop app and the
fast-tier examples, so the generated core is not recompiled), the profile
builds differ by RUSTFLAGS and use ``pgo-instrument/`` and ``pgo-use/`` below it.

Commands: ``train``, ``check``, ``plan``, ``bench --pairs N``,
``bench-bins A B --pairs N`` (two prebuilt workload test binaries).

The training workload is the ``coupled_ready_exactness`` gate, run from the
instrumented desktop APP binary with ``--pgo-train`` (not the test binary:
cargo gives the test target's dependencies other crate hashes than the app's,
so a profile trained on it never matched the app). It needs the private ready fixtures and a generated DSP core (``SHARC_GEN_DIR``).
The launcher (``tools/native_emu.sh``) retrains a stale profile itself. The key
covers the emulation crates only (not packages/), so a desktop-crate edit
leaves the profile current.

The toolchain must be the rustup 1.98.1 distribution: a Homebrew rustc has the
same version string but different LLVM, and cannot read these profiles.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import runpy
import shutil
import statistics
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = "packages/desktop/src-tauri/Cargo.toml"
DEFAULT_GENERATED = ROOT / "out" / "native" / "dn2-audio" / "gen"
READY = ROOT / "snapshots" / "dn2-audio-ready-2026-10-03"
TOOLCHAIN = "1.98.1"
TRIPLE = "aarch64-apple-darwin"
FEATURES = "coupled-audio"
TEST_NAME = "desktop_runtime::coupled::tests::coupled_ready_exactness"
WORKLOAD = "desktop-bin"
EXPECTED_FRAMES = 1139
TRAIN_RUNS = 4
FLAGS_TEMPLATE = "-Cprofile-use={profdata}"
SKIP_DIRS = {"target", ".git", "node_modules"}
SKIP_FILES = {".DS_Store"}


def native_target():
    """tools/native_target.py: cargo target dirs keyed by generated core."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        import native_target as module
    finally:
        sys.path.pop(0)
    return module


def benchlock(mode: str, label: str):
    """Lock for builds (shared) and timed runs (exclusive), see benchlock.py."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        import benchlock as module
    finally:
        sys.path.pop(0)
    return module.hold(mode, label)


def supported_host() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def state_dir(triple: str = TRIPLE) -> pathlib.Path:
    return ROOT / "out" / "native" / "pgo" / "dn2" / triple


def toolchain_dir() -> pathlib.Path:
    home = pathlib.Path(
        os.environ.get("RUSTUP_HOME") or pathlib.Path.home() / ".rustup"
    )
    return home / "toolchains" / f"{TOOLCHAIN}-{TRIPLE}"


def llvm_profdata() -> pathlib.Path:
    return toolchain_dir() / "lib" / "rustlib" / TRIPLE / "bin" / "llvm-profdata"


def generated_dir(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> pathlib.Path:
    environ = os.environ if environ is None else environ
    configured = environ.get("SHARC_GEN_DIR")
    return pathlib.Path(configured) if configured else DEFAULT_GENERATED


def rustc_version() -> str:
    rustc = toolchain_dir() / "bin" / "rustc"
    return subprocess.run(
        [str(rustc), "-vV"], capture_output=True, text=True, check=True
    ).stdout


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileHasher:
    """sha256 of files, memoised on (size, mtime_ns) in a JSON cache."""

    def __init__(self, cache_path: pathlib.Path) -> None:
        self.cache_path = cache_path
        try:
            self.old: dict[str, list[object]] = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            self.old = {}
        self.new: dict[str, list[object]] = {}

    def digest(self, path: pathlib.Path) -> str:
        st = path.stat()
        key = str(path)
        hit = self.old.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            self.new[key] = hit
            return str(hit[2])
        sha = sha256_file(path)
        self.new[key] = [st.st_size, st.st_mtime_ns, sha]
        return sha

    def save(self) -> None:
        if self.new == self.old:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.new))
        tmp.replace(self.cache_path)


def tree_files(top: pathlib.Path) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found.extend(
            pathlib.Path(dirpath) / n for n in sorted(filenames) if n not in SKIP_FILES
        )
    return found


def tree_digest(
    hasher: FileHasher, top: pathlib.Path, base: pathlib.Path, skip_top: set[str]
) -> tuple[str, int]:
    """Digest of (relative path, sha256) over a tree; skip_top names top-level dirs to leave out."""
    outer = hashlib.sha256()
    count = 0
    for path in tree_files(top):
        rel = path.relative_to(top)
        if rel.parts[0] in skip_top and len(rel.parts) > 1:
            continue
        outer.update(f"{path.relative_to(base)}\0{hasher.digest(path)}\n".encode())
        count += 1
    return outer.hexdigest(), count


DESKTOP_DIR = str(pathlib.PurePosixPath(MANIFEST_PATH).parent)


def is_emulation_package(rel: str) -> bool:
    """True for a crate that shapes hot code; the UI shell under packages/ does not.

    The profile is keyed by the emulation code only. A slightly stale profile is
    safe: LLVM ignores profile data for functions whose CFG hash changed, so a
    desktop-crate edit costs nothing, while a profile treated as stale makes the
    launcher fall back to a non-PGO build (about 30% slower DSP).
    """
    return pathlib.PurePosixPath(rel).parts[:1] != ("packages",)


def local_package_dirs() -> list[str]:
    """Repo-relative directories of the emulation crates in the coupled build graph."""
    out = subprocess.run(
        [
            "rustup",
            "run",
            TOOLCHAIN,
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--manifest-path",
            MANIFEST_PATH,
            "--features",
            FEATURES,
            "--filter-platform",
            TRIPLE,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    dirs = set()
    for package in json.loads(out)["packages"]:
        if package["source"] is None:
            dirs.add(
                str(pathlib.Path(package["manifest_path"]).parent.relative_to(ROOT))
            )
    return sorted(d for d in dirs if is_emulation_package(d))


def fast_regions(environ: dict[str, str] | os._Environ[str] | None = None) -> str:
    """The fast-tier region list a coupled run uses ("" when off).

    An explicit SHARC_FAST_REGIONS wins, DIGI_EMU_FAST=0 turns the tier off,
    otherwise the launcher's per-firmware table is read for DIGI_COUPLED_SYX
    (the launcher sets SHARC_FAST_REGIONS before it asks for a launch plan).
    """
    environ = os.environ if environ is None else environ
    if "SHARC_FAST_REGIONS" in environ:
        return environ["SHARC_FAST_REGIONS"]
    if environ.get("DIGI_EMU_FAST") == "0":
        return ""
    launcher = runpy.run_path(
        str(ROOT / "tools" / "native_emu.sh"), run_name="launcher"
    )
    syx = environ.get("DIGI_COUPLED_SYX", str(ROOT / "Digitone_II_OS1.11.syx"))
    return str(launcher["fast_regions_for"](pathlib.Path(syx)))


def blocktrans_roots(environ: dict[str, str] | os._Environ[str] | None = None) -> str:
    """The SHARC_BLOCKTRANS cluster roots a coupled run uses ("" when none).

    Same precedence as :func:`fast_regions`: explicit env, DIGI_EMU_FAST=0,
    then the launcher's per-firmware table.
    """
    environ = os.environ if environ is None else environ
    if "SHARC_BLOCKTRANS" in environ:
        return environ["SHARC_BLOCKTRANS"]
    if environ.get("DIGI_EMU_FAST") == "0":
        return ""
    launcher = runpy.run_path(
        str(ROOT / "tools" / "native_emu.sh"), run_name="launcher"
    )
    syx = environ.get("DIGI_COUPLED_SYX", str(ROOT / "Digitone_II_OS1.11.syx"))
    return str(launcher["blocktrans_roots_for"](pathlib.Path(syx)))


def compute_components(
    packages: list[str],
    gen: pathlib.Path,
    hasher: FileHasher,
    fast: str = "",
    blocktrans: str = "",
) -> tuple[dict[str, str], int]:
    outer = hashlib.sha256()
    files = 0
    for rel in packages:
        if not is_emulation_package(rel):
            continue
        digest, count = tree_digest(hasher, ROOT / rel, ROOT, {"gen"})
        outer.update(f"{rel}\0{digest}\n".encode())
        files += count
    gen_digest, gen_count = (
        tree_digest(hasher, gen, gen, set()) if gen.is_dir() else ("absent", 0)
    )
    workspace = hashlib.sha256()
    for name in ("Cargo.toml", "Cargo.lock"):
        workspace.update(hasher.digest(ROOT / name).encode())
    components = {
        "sources": outer.hexdigest(),
        "workspace": workspace.hexdigest(),
        "generated": gen_digest,
        "rustc": hashlib.sha256(rustc_version().encode()).hexdigest(),
        "triple": TRIPLE,
        "features": FEATURES,
        "workload": WORKLOAD,
        "flags_template": FLAGS_TEMPLATE,
        "fast_regions": fast,
        "blocktrans": blocktrans,
    }
    return components, files + gen_count


def key_of(components: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(components, sort_keys=True).encode()).hexdigest()


def read_manifest() -> dict[str, object] | None:
    try:
        return json.loads((state_dir() / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def desktop_digest(hasher: FileHasher) -> str:
    """Digest of the desktop crate: recorded for reporting, not part of the key."""
    return tree_digest(hasher, ROOT / DESKTOP_DIR, ROOT, set())[0]


def check_state(
    gen: pathlib.Path | None = None, fast: str = "", blocktrans: str = ""
) -> tuple[str, str, bool]:
    """Return (status, detail, desktop_changed); status is ok, stale or missing."""
    manifest = read_manifest()
    if manifest is None:
        return "missing", "no manifest", False
    profdata = pathlib.Path(str(manifest["profdata"]["path"]))  # type: ignore[index]
    if not profdata.is_file():
        return "missing", f"profile file {profdata} is gone", False
    if not toolchain_dir().is_dir():
        return "stale", f"rustup toolchain {TOOLCHAIN} is not installed", False
    hasher = FileHasher(state_dir() / "hash-cache.json")
    components, _ = compute_components(
        list(manifest["packages"]), gen or generated_dir(), hasher, fast, blocktrans
    )  # type: ignore[call-overload]
    desktop = desktop_digest(hasher)
    hasher.save()
    old = manifest["components"]
    changed = [name for name, value in components.items() if old.get(name) != value]  # type: ignore[attr-defined]
    if changed:
        return "stale", "changed: " + ", ".join(changed), False
    return "ok", str(profdata), manifest.get("desktop_sha256") != desktop


def check(
    gen: pathlib.Path | None = None, fast: str = "", blocktrans: str = ""
) -> tuple[str, str]:
    """Return (status, detail): status is ok, stale or missing."""
    status, detail, _ = check_state(gen, fast, blocktrans)
    return status, detail


def profile_use_flags(profdata: str) -> str:
    return FLAGS_TEMPLATE.format(profdata=profdata)


@dataclasses.dataclass
class LaunchPlan:
    prefix: list[str]
    cargo_args: list[str]
    env: dict[str, str]


def pgo_state(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str, str]:
    """The launcher's view of the profile: (status, detail).

    Status is off (opted out or unsupported host, stay silent), skipped
    (RUSTFLAGS is set), or what :func:`check` returns: ok, stale, missing.
    """
    environ = os.environ if environ is None else environ
    if environ.get("DIGI_EMU_PGO") == "0" or not supported_host():
        return "off", ""
    if environ.get("RUSTFLAGS") or environ.get("CARGO_ENCODED_RUSTFLAGS"):
        return "skipped", "RUSTFLAGS is set"
    return check(
        generated_dir(environ),
        environ.get("SHARC_FAST_REGIONS", ""),
        environ.get("SHARC_BLOCKTRANS", ""),
    )


def plan_for(
    profdata: str, environ: dict[str, str] | os._Environ[str] | None = None
) -> LaunchPlan:
    environ = os.environ if environ is None else environ
    return LaunchPlan(
        prefix=["rustup", "run", TOOLCHAIN],
        cargo_args=[
            "--target",
            TRIPLE,
            "--target-dir",
            str(native_target().target_dir(generated_dir(environ), "pgo-use")),
        ],
        env={"RUSTFLAGS": profile_use_flags(profdata), "RUSTC": toolchain_rustc()},
    )


def fallback_notice(status: str, detail: str) -> str:
    if status == "skipped":
        return f"WARNING: native PGO: skipped ({detail}); building without PGO"
    what = detail if status == "failed" else f"profile {status} ({detail})"
    return (
        f"WARNING: native PGO: {what}; building without PGO "
        "(about 30% slower DSP). Run tools/native_pgo.py train"
    )


def training_blocker(environ: dict[str, str] | os._Environ[str] | None = None) -> str:
    """Why `train` cannot run here ("" when it can)."""
    try:
        require_inputs(generated_dir(environ))
    except SystemExit as exc:
        return str(exc)
    return ""


def launch_plan(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[LaunchPlan | None, str]:
    """Decide whether the launcher can use the PGO build; returns (plan, notice).

    An empty notice means stay silent (opted out or unsupported host).
    """
    environ = os.environ if environ is None else environ
    status, detail = pgo_state(environ)
    if status == "off":
        return None, ""
    if status != "ok":
        return None, fallback_notice(status, detail)
    return plan_for(detail, environ), "native PGO: using profile " + pathlib.Path(
        detail
    ).name


# --- crate-hash guard ------------------------------------------------------------

# PGO matches functions by mangled name, and a v0 name embeds the crate
# disambiguator (`Cs<hash>_<len><name>`). A profile trained from a build whose
# crate hashes differ from the app's silently applies to nothing.
GUARDED_CRATES = ("sharc_native", "sharc_gen", "sharc_rt", "elektron_native_boot")
CRATE_NAME_RE = re.compile(
    r"C[sc]([0-9A-Za-z]+)_("
    + "|".join(f"{len(name)}{name}" for name in GUARDED_CRATES)
    + r")"
)


def crates_in(text: str) -> dict[str, set[str]]:
    """Crate name -> disambiguators seen in v0-mangled names in `text`."""
    found: dict[str, set[str]] = {}
    for match in CRATE_NAME_RE.finditer(text):
        name = match.group(2).lstrip("0123456789")
        found.setdefault(name, set()).add(match.group(1))
    return found


def profile_crates(profdata: str | os.PathLike[str]) -> dict[str, set[str]]:
    """Guarded crate disambiguators in a profile (cached as <profdata>.crates.json)."""
    profdata = pathlib.Path(profdata)
    cache = profdata.with_name(profdata.name + ".crates.json")
    try:
        if cache.stat().st_mtime >= profdata.stat().st_mtime:
            return {k: set(v) for k, v in json.loads(cache.read_text()).items()}
    except (OSError, ValueError):
        pass
    shown = subprocess.run(
        [str(llvm_profdata()), "show", "--all-functions", str(profdata)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = crates_in(shown)
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps({k: sorted(v) for k, v in found.items()}))
    return found


def binary_crates(path: str | os.PathLike[str]) -> dict[str, set[str]]:
    """Guarded crate disambiguators in a binary's symbols (via nm)."""
    shown = subprocess.run(
        ["nm", str(path)], check=True, capture_output=True, text=True
    ).stdout
    return crates_in(shown)


def crate_mismatches(
    profile: dict[str, set[str]], binary: dict[str, set[str]]
) -> list[str]:
    """One "MISMATCH crate X: ..." line per crate whose hashes do not overlap."""
    lines = []
    for name in GUARDED_CRATES:
        want, have = profile.get(name, set()), binary.get(name, set())
        if want and have and not want & have:
            lines.append(
                f"MISMATCH crate {name}: profile {sorted(want)} binary {sorted(have)}"
            )
    return lines


def pgo_binary(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> pathlib.Path:
    """The app binary the launcher's PGO build produces."""
    target = native_target().target_dir(generated_dir(environ), "pgo-use")
    return pathlib.Path(target) / TRIPLE / "release" / "digiemu-desktop"


def verify_binary(binary: pathlib.Path, profdata: str | os.PathLike[str]) -> list[str]:
    """Mismatch lines for `binary` against `profdata` (empty when they match)."""
    return crate_mismatches(profile_crates(profdata), binary_crates(binary))


# --- training and benchmarking -------------------------------------------------


def toolchain_rustc() -> str:
    """The rustup toolchain's rustc, for RUSTC in every build.

    Cargo otherwise takes `rustc` from PATH. A Homebrew rustc of the same
    version (`1.98.1 (...) (Homebrew)`) mangles every crate differently, so a
    profile trained with one compiler never matches a build made with the
    other, and PGO silently does nothing.
    """
    done = subprocess.run(
        ["rustup", "which", "--toolchain", TOOLCHAIN, "rustc"],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def base_env(gen: pathlib.Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")
    }
    env["SHARC_GEN_DIR"] = str(gen)
    env["RUSTC"] = toolchain_rustc()
    return env


def run_env(deps_dir: pathlib.Path) -> dict[str, str]:
    return {
        "DYLD_LIBRARY_PATH": str(deps_dir),
        "NOTE_EVENTS": "trig",
        "DIGI_COUPLED_FIXTURES": os.environ.get("DIGI_COUPLED_FIXTURES", str(READY)),
        "DIGI_COUPLED_SYX": os.environ.get(
            "DIGI_COUPLED_SYX", str(ROOT / "Digitone_II_OS1.11.syx")
        ),
        "DN2_PROFILE_LINK": "1",
    }


def build_tests(
    target_dir: pathlib.Path,
    rustflags: str | None,
    gen: pathlib.Path,
    log: pathlib.Path,
    app: bool = False,
) -> pathlib.Path:
    """Build the workload test binary, or with app=True the desktop app binary."""
    env = base_env(gen)
    if rustflags:
        env["RUSTFLAGS"] = rustflags
    command = [
        "rustup",
        "run",
        TOOLCHAIN,
        "cargo",
        *(["build", "--bin", "digiemu-desktop"] if app else ["test", "--no-run"]),
        "--release",
        "--locked",
        "--manifest-path",
        MANIFEST_PATH,
        "--features",
        FEATURES,
        # RUSTFLAGS builds use --target so the flags skip build scripts; the
        # plain build has none, and shares its key dir with the desktop app.
        *(["--target", TRIPLE] if rustflags else []),
        "--target-dir",
        str(target_dir),
        "--message-format=json",
    ]
    log.parent.mkdir(parents=True, exist_ok=True)
    with benchlock("shared", "pgo cargo build"), log.open("w") as handle:
        proc = subprocess.run(
            command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=handle, text=True
        )
    if proc.returncode:
        tail = "".join(log.read_text().splitlines(keepends=True)[-30:])
        raise SystemExit(f"test build failed (see {log}):\n{tail}")
    found = []
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("executable")
            and bool(message.get("profile", {}).get("test")) != app
            and (not app or "bin" in message.get("target", {}).get("kind", []))
            and "digiemu-desktop" in message.get("package_id", "")
        ):
            found.append(pathlib.Path(message["executable"]))
    if len(found) != 1:
        raise SystemExit(
            f"expected one digiemu-desktop {'app' if app else 'test'} binary, found {found}"
        )
    return found[0]


@dataclasses.dataclass
class Result:
    elapsed: float
    dsp_ns: int
    frames: int
    user: float
    sys: float
    real: float
    line: str


def run_workload(
    binary: pathlib.Path,
    log: pathlib.Path,
    extra_env: dict[str, str] | None = None,
    app: bool = False,
) -> Result:
    """Run the workload under /usr/bin/time and enforce the gates."""
    env_pairs = {**run_env(binary.parent), **(extra_env or {})}
    command = [
        "/usr/bin/time",
        "-lp",
        "env",
        *(f"{k}={v}" for k, v in env_pairs.items()),
        str(binary),
        *(
            ["--pgo-train"]
            if app
            else [TEST_NAME, "--ignored", "--nocapture", "--test-threads=1"]
        ),
    ]
    proc = subprocess.run(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    log.write_text(proc.stdout)
    if proc.returncode:
        raise SystemExit(
            f"workload failed (exit {proc.returncode}), see {log}:\n"
            + "\n".join(proc.stdout.splitlines()[-25:])
        )
    text = proc.stdout

    def grab(pattern: str) -> str:
        found = re.search(pattern, text)
        if not found:
            raise SystemExit(f"gate: {pattern!r} not found in {log}")
        return found.group(1)

    result = Result(
        elapsed=float(grab(r"coupled_workload_elapsed_seconds=([0-9.]+)")),
        dsp_ns=int(grab(r"worker_dsp_ns: (\d+)")),
        frames=int(grab(r"frames_completed: (\d+)")),
        user=float(grab(r"(?m)^user\s+([0-9.]+)")),
        sys=float(grab(r"(?m)^sys\s+([0-9.]+)")),
        real=float(grab(r"(?m)^real\s+([0-9.]+)")),
        line=next(
            line
            for line in text.splitlines()
            if line.startswith("native_audio_link_timing_window=")
        ),
    )
    if result.frames != EXPECTED_FRAMES:
        raise SystemExit(
            f"gate: frames_completed {result.frames} != {EXPECTED_FRAMES} in {log}"
        )
    return result


def require_inputs(gen: pathlib.Path) -> None:
    if not supported_host():
        raise SystemExit("native PGO is only set up for aarch64 macOS")
    if not toolchain_dir().is_dir():
        raise SystemExit(f"missing rustup toolchain {toolchain_dir()}")
    if not gen.is_dir():
        raise SystemExit(f"generated DN2 core not found: {gen} (set SHARC_GEN_DIR)")
    for path in (
        run_env(pathlib.Path("."))["DIGI_COUPLED_FIXTURES"],
        run_env(pathlib.Path("."))["DIGI_COUPLED_SYX"],
    ):
        if not pathlib.Path(path).exists():
            raise SystemExit(f"missing private input {path}")


def cmd_train(args: argparse.Namespace) -> None:
    gen = generated_dir()
    require_inputs(gen)
    state = state_dir()
    timings: dict[str, float] = {}
    started = time.monotonic()

    def phase(name: str, since: float) -> float:
        now = time.monotonic()
        timings[name] = round(now - since, 2)
        print(f"phase {name}: {timings[name]} s", flush=True)
        return now

    t = time.monotonic()
    packages = local_package_dirs()
    hasher = FileHasher(state / "hash-cache.json")
    fast = fast_regions()
    blocktrans = blocktrans_roots()
    components, nfiles = compute_components(packages, gen, hasher, fast, blocktrans)
    desktop = desktop_digest(hasher)
    hasher.save()
    key = key_of(components)
    t = phase("key", t)

    raw = state / f"raw-{key[:16]}"
    shutil.rmtree(raw, ignore_errors=True)
    raw.mkdir(parents=True)
    binary = build_tests(
        native_target().target_dir(gen, "pgo-instrument"),
        f"-Cprofile-generate={raw}",
        gen,
        state / "instrument-build.log",
        app=True,
    )
    t = phase("instrumented-build", t)

    gates = []
    for number in range(1, args.runs + 1):
        with benchlock("shared", "pgo train"):
            result = run_workload(
                binary,
                state / f"train-{number}.log",
                {
                    "LLVM_PROFILE_FILE": str(raw / f"train-{number}-%m.profraw"),
                    "SHARC_FAST_REGIONS": fast,
                    "SHARC_BLOCKTRANS": blocktrans,
                },
                app=True,
            )
        gates.append(result.line)
        print(
            f"train run {number}: elapsed {result.elapsed:.3f} s frames {result.frames}",
            flush=True,
        )
    t = phase("training-runs", t)

    merged = raw / "merged.profdata"
    raws = sorted(str(p) for p in raw.glob("*.profraw"))
    if not raws:
        raise SystemExit("training produced no .profraw files")
    merge = [str(llvm_profdata()), "merge", "-o", str(merged), *raws]
    subprocess.run(merge, check=True)
    digest = sha256_file(merged)
    profiles = state / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    final = profiles / f"{digest}.profdata"
    shutil.move(str(merged), final)
    shutil.rmtree(raw, ignore_errors=True)
    t = phase("merge", t)

    manifest = {
        "key": key,
        "components": components,
        "packages": packages,
        "desktop_sha256": desktop,
        "generated_dir": str(gen),
        "fast_regions": fast,
        "blocktrans": blocktrans,
        "file_count": nfiles,
        "rustc_vV": rustc_version(),
        "sysroot": str(toolchain_dir()),
        "triple": TRIPLE,
        "features": FEATURES,
        "profdata": {
            "path": str(final),
            "sha256": digest,
            "size": final.stat().st_size,
        },
        "train_commands": {
            "build_rustflags": f"-Cprofile-generate={raw}",
            "runs": args.runs,
            "merge": merge[:4] + ["<raw>..."],
            "workload": f"{WORKLOAD} --pgo-train",
        },
        "gate_lines": gates,
        "phase_seconds": timings,
        "created": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    (state / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"trained {final} ({final.stat().st_size} bytes) key {key[:16]} in {time.monotonic() - started:.1f} s"
    )


def cmd_check(_: argparse.Namespace) -> int:
    began = time.monotonic()
    status, detail, desktop = check_state(None, fast_regions(), blocktrans_roots())
    if status == "ok" and desktop:
        print("ok (desktop crate changed; profile reused)")
    else:
        print(f"{status} {detail}" if status != "missing" else f"missing ({detail})")
    print(f"check took {time.monotonic() - began:.3f} s", file=sys.stderr)
    return 0 if status == "ok" else 1


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = read_manifest()
    profdata = pathlib.Path(str(manifest["profdata"]["path"])) if manifest else None  # type: ignore[index]
    if profdata is None or not profdata.is_file():
        print("no trained profile to verify against")
        return 1
    binary = pathlib.Path(args.binary) if args.binary else pgo_binary()
    if not binary.is_file():
        print(f"no binary at {binary}")
        return 1
    lines = verify_binary(binary, profdata)
    print("\n".join(lines) if lines else "ok")
    return 1 if lines else 0


def cmd_plan(_: argparse.Namespace) -> int:
    plan, notice = launch_plan(
        {
            **os.environ,
            "SHARC_FAST_REGIONS": fast_regions(),
            "SHARC_BLOCKTRANS": blocktrans_roots(),
        }
    )
    print(notice or "native PGO: not used")
    if plan:
        print(
            json.dumps(
                {"prefix": plan.prefix, "cargo_args": plan.cargo_args, "env": plan.env},
                indent=2,
            )
        )
    return 0 if plan else 1


def median(values: list[float]) -> float:
    return statistics.median(values)


def cmd_bench(args: argparse.Namespace) -> None:
    gen = generated_dir()
    require_inputs(gen)
    fast = fast_regions()
    blocktrans = blocktrans_roots()
    status, detail = check(gen, fast, blocktrans)
    if status != "ok":
        raise SystemExit(f"profile is not current: {status} {detail}; run train first")
    state = state_dir()
    out = state / ("bench-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True)
    t = time.monotonic()
    control_src = build_tests(
        native_target().target_dir(gen), None, gen, out / "control-build.log"
    )
    print(f"control build {time.monotonic() - t:.1f} s", flush=True)
    t = time.monotonic()
    use_src = build_tests(
        native_target().target_dir(gen, "pgo-use"),
        profile_use_flags(detail),
        gen,
        out / "use-build.log",
    )
    print(f"profile-use build {time.monotonic() - t:.1f} s", flush=True)
    binaries = {}
    hashes = {}
    for label, src in (("control", control_src), ("use", use_src)):
        dst = out / f"{label}-{src.name}"
        shutil.copy2(src, dst)
        binaries[label] = (dst, src.parent)
        hashes[label] = sha256_file(dst)
        print(f"{label} binary sha256 {hashes[label]}")
    paired_bench(
        out,
        binaries,
        "control",
        "use",
        args.pairs,
        {
            "binary_sha256": hashes,
            "profdata": detail,
            "fast_regions": fast,
            "blocktrans": blocktrans,
        },
        {"SHARC_FAST_REGIONS": fast, "SHARC_BLOCKTRANS": blocktrans},
    )


def _label_env(
    env: dict[str, str] | dict[str, dict[str, str]] | None, label: str
) -> dict[str, str]:
    """Extra env for one label: either shared, or a {label: env} map."""
    if not env:
        return {}
    if label in env and isinstance(env[label], dict):
        return env[label]  # type: ignore[return-value]
    if all(isinstance(v, dict) for v in env.values()):
        return {}
    return env  # type: ignore[return-value]


def paired_bench(
    out: pathlib.Path,
    binaries: dict[str, tuple[pathlib.Path, pathlib.Path]],
    label_a: str,
    label_b: str,
    pairs: int,
    extra: dict[str, object],
    env: dict[str, str] | dict[str, dict[str, str]] | None = None,
) -> None:
    """Alternate A,B / B,A runs of the workload; write bench.tsv, summary.json."""
    rows = []
    with benchlock("exclusive", "pgo bench"):
        for pair in range(1, pairs + 1):
            order = [label_a, label_b] if pair % 2 else [label_b, label_a]
            results = {}
            for label in order:
                dst, deps = binaries[label]
                results[label] = run_workload(
                    dst,
                    out / f"run-{pair:02d}-{label}.log",
                    {
                        "DYLD_LIBRARY_PATH": str(deps),
                        **_label_env(env, label),
                    },
                )
            for label in (label_a, label_b):
                r = results[label]
                rows.append(
                    (pair, label, r.elapsed, r.dsp_ns, r.frames, r.real, r.user, r.sys)
                )
            print(
                f"pair {pair} order {','.join(order)}: ratio wall {results[label_b].elapsed / results[label_a].elapsed:.3f}",
                flush=True,
            )
    with (out / "bench.tsv").open("w") as handle:
        handle.write(
            "pair\tlabel\telapsed_s\tworker_dsp_ns\tframes\treal_s\tuser_s\tsys_s\n"
        )
        for row in rows:
            handle.write("\t".join(str(v) for v in row) + "\n")

    def ratios(index: int | tuple[int, int]) -> list[float]:
        def value(row: tuple[object, ...]) -> float:
            if isinstance(index, tuple):
                return float(row[index[0]]) + float(row[index[1]])  # type: ignore[arg-type]
            return float(row[index])  # type: ignore[arg-type]

        out_ratios = []
        for pair in range(1, pairs + 1):
            by = {row[1]: value(row) for row in rows if row[0] == pair}
            out_ratios.append(by[label_b] / by[label_a])
        return out_ratios

    summary = {
        "pairs": pairs,
        "order": f"{label_a},{label_b} then {label_b},{label_a} alternating",
        **extra,
        f"median_ratio_{label_b}_over_{label_a}": {
            "workload_wall": median(ratios(2)),
            "worker_dsp": median(ratios(3)),
            "user_plus_sys": median(ratios((6, 7))),
        },
        "ratios": {
            "workload_wall": ratios(2),
            "worker_dsp": ratios(3),
            "user_plus_sys": ratios((6, 7)),
        },
        "rustc_vV": rustc_version(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"pair  {label_a}_wall  {label_b}_wall  {label_a}_dsp_ns  {label_b}_dsp_ns")
    for pair in range(1, pairs + 1):
        c = next(r for r in rows if r[0] == pair and r[1] == label_a)
        u = next(r for r in rows if r[0] == pair and r[1] == label_b)
        print(f"{pair:>4}  {c[2]:>12.3f}  {u[2]:>8.3f}  {c[3]:>14}  {u[3]:>10}")
    for name, value in summary[f"median_ratio_{label_b}_over_{label_a}"].items():  # type: ignore[attr-defined]
        print(f"median {label_b}/{label_a} {name}: {value:.3f}")
    print(f"results in {out}")


def cmd_bench_bins(args: argparse.Namespace) -> None:
    """Paired comparison of two prebuilt workload test binaries."""
    gen = generated_dir()
    require_inputs(gen)
    state = state_dir()
    out = state / ("bench-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True)
    labels = {"a": args.label_a, "b": args.label_b}
    if labels["a"] == labels["b"]:
        raise SystemExit("--label-a and --label-b must differ")
    binaries = {}
    hashes = {}
    for key, source in (("a", args.a), ("b", args.b)):
        src = pathlib.Path(source).resolve()
        if not src.is_file():
            raise SystemExit(f"not a file: {src}")
        label = labels[key]
        dst = out / f"{label}-{src.name}"
        shutil.copy2(src, dst)
        binaries[label] = (dst, src.parent)
        hashes[label] = sha256_file(dst)
        print(f"{label} binary sha256 {hashes[label]} ({src})")
    env = {
        labels["a"]: {"SHARC_FAST_REGIONS": args.fast_a, **parse_env(args.env_a)},
        labels["b"]: {"SHARC_FAST_REGIONS": args.fast_b, **parse_env(args.env_b)},
    }
    paired_bench(
        out,
        binaries,
        labels["a"],
        labels["b"],
        args.pairs,
        {
            "binary_sha256": hashes,
            "sources": [args.a, args.b],
            "env": env,
        },
        env,
    )


def parse_env(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--env-*: expected KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def cmd_run(args: argparse.Namespace) -> None:
    """Run the workload once under the exclusive lock; print the gates."""
    gen = generated_dir()
    require_inputs(gen)
    binary = pathlib.Path(args.binary).resolve()
    log = pathlib.Path(args.log).resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    deps = pathlib.Path(args.deps).resolve() if args.deps else binary.parent
    extra = {"DYLD_LIBRARY_PATH": str(deps), **parse_env(args.env)}
    with benchlock("exclusive", "pgo run"):
        r = run_workload(binary, log, extra)
    print(
        f"elapsed_s={r.elapsed} worker_dsp_ns={r.dsp_ns} frames={r.frames} "
        f"real={r.real} user={r.user} sys={r.sys}"
    )


def cmd_build(args: argparse.Namespace) -> None:
    """Plain release coupled test binary for a generated core (SHARC_GEN_DIR)."""
    gen = pathlib.Path(args.gen).resolve()
    target = (
        pathlib.Path(args.target_dir).resolve()
        if args.target_dir
        else native_target().target_dir(gen)
    )
    binary = build_tests(target, None, gen, target / "build.log")
    print(binary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser(
        "train", help="build instrumented, run the workload, merge a profile"
    )
    train.add_argument("--runs", type=int, default=TRAIN_RUNS)
    sub.add_parser("check", help="print ok/stale/missing for the current profile")
    verify = sub.add_parser(
        "verify", help="check the profile's crate hashes against the app binary"
    )
    verify.add_argument("--binary", help="default: the launcher's pgo-use app binary")
    sub.add_parser("plan", help="show what the launcher would do")
    bench = sub.add_parser("bench", help="paired control vs profile-use benchmark")
    bench.add_argument("--pairs", type=int, default=5)
    build = sub.add_parser(
        "build", help="plain release workload test binary for a generated core"
    )
    build.add_argument("--gen", required=True, help="generated core directory")
    build.add_argument(
        "--target-dir", help="default: out/native/target/<gen-key> (native_target.py)"
    )
    run = sub.add_parser("run", help="run the workload once (exclusive lock)")
    run.add_argument("binary")
    run.add_argument("--log", required=True)
    run.add_argument("--deps", help="directory with the dylibs (default: the binary's)")
    run.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    bins = sub.add_parser(
        "bench-bins", help="paired benchmark of two prebuilt workload test binaries"
    )
    bins.add_argument("a", help="baseline test binary")
    bins.add_argument("b", help="test binary compared against it")
    bins.add_argument("--pairs", type=int, default=5)
    bins.add_argument("--label-a", default="a")
    bins.add_argument("--label-b", default="b")
    bins.add_argument(
        "--fast-a", default="", help="SHARC_FAST_REGIONS for the baseline binary"
    )
    bins.add_argument(
        "--fast-b", default="", help="SHARC_FAST_REGIONS for the compared binary"
    )
    for side in ("a", "b"):
        bins.add_argument(
            f"--env-{side}",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help=f"extra environment for the {side} binary (e.g. SHARC_FAST_AUTO=64,64)",
        )
    args = parser.parse_args(argv)
    if args.command == "run":
        cmd_run(args)
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "bench":
        cmd_bench(args)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "bench-bins":
        cmd_bench_bins(args)
    elif args.command == "check":
        return cmd_check(args)
    elif args.command == "verify":
        return cmd_verify(args)
    else:
        return cmd_plan(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
