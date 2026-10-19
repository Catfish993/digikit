#!/usr/bin/env python3
"""Run cargo over the whole native workspace with the pinned toolchain.

    uv run python -m tools.devtools.rust_ws test [-- CARGO_ARGS...]   cargo test --workspace --release
    uv run python -m tools.devtools.rust_ws test --packages A,B       cargo test -p A -p B --release (verify's affected crates)
    uv run python -m tools.devtools.rust_ws check [-- CARGO_ARGS...]  cargo check --workspace --all-targets
    uv run python -m tools.devtools.rust_ws gates                     the ignored coupled gate (needs the private DN2 ready inputs)
    uv run python -m tools.devtools.rust_ws fmt                       cargo fmt --check on the clean crates

``test`` and ``check`` use the generated core of ``tools/emulator/digi_env.py`` and its
target dir (``tools/emulator/native_target.py``), so they reuse the compiled
``sharc-gen``. The desktop crate embeds ``apps/desktop/ui/dist``; it is left out
when the desktop console has not been built. ``fmt`` covers only ``CLEAN_CRATES``:
add a crate once ``cargo fmt -p CRATE --check`` passes; the list should only
grow.
"""

from __future__ import annotations

import functools
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

from tools.emulator import digi_env, native_target  # noqa: E402

CLEAN_CRATES = [
    "bootstrap-emu",
    "coldfire",
    "device-profile",
    "digiemu-desktop",
    "dt2-firmware-loader",
    "live-audio",
    "patchkit",
    "periph",
    "plusdrive-format",
    "sharc-gen",
]


# ---------------------------------------------------------------------------
# Test selection: which workspace packages a set of changed files can affect.
# ---------------------------------------------------------------------------

# Files whose change can affect any crate: the whole workspace runs.
WHOLE_WORKSPACE = re.compile(
    r"^(Cargo\.toml|Cargo\.lock|rust-toolchain(\.toml)?|\.cargo/.*)$"
)

# Non-Rust inputs that Rust code reads: (regex on the repo-relative path, packages that read it, closure).
# closure=True: the file is compiled in (build.rs, include_str!), so every dependent's tests can see it and
# the reverse dependency closure is added. closure=False: only the listed packages' tests read the file at
# run time, so only they run.
READS: list[tuple[str, list[str], bool]] = [
    # packages/patchkit/build.rs:10-30 embeds every devices/*.toml (rerun-if-changed on the dir and
    # each file); src/modgen.rs:241-242 include_str! the two device files; packages/patchkit/tests/devdata.rs:83-117
    # reads them. (Not devices/ports/.)
    (r"^devices/[^/]+\.toml$", ["patchkit"], True),
    # packages/emulator/device-profile/src/lib.rs:27-36 and :542-548 include_str! the two device TOMLs.
    (r"^devices/(digitakt-ii|digitone-ii)\.toml$", ["device-profile"], True),
    # patchkit tests (typed.rs:74,92 preflight.rs:25 frameworks.rs:23 conflicts.rs:22 prims.rs:34
    # dynamic.rs:47 devdata.rs:136) and src/main.rs:59 read devices/ports/*.toml;
    # packages/emulator/boot/tests/patched_boot.rs:14 reads it too.
    (r"^devices/ports/", ["patchkit", "elektron-native-boot"], False),
    # packages/emulator/control/src/load.rs:18 include_str! devices/cycle-factors.json.
    (r"^devices/cycle-factors\.json$", ["digi-control"], True),
    # Library::scan(modules/) reads every module.toml and its build/*.dko.json:
    # packages/patchkit/tests/devdata.rs:19, conflicts.rs:27, frameworks.rs:27, dynamic.rs:17,
    # typed.rs:186 (modules/machines/pluck); packages/emulator/boot/tests/patched_boot.rs:15.
    # Markdown under modules/ is documentation only.
    (r"^modules/(?!.*\.md$)", ["patchkit", "elektron-native-boot"], False),
    # packages/emulator/coupled/src/scenario.rs:12 include_str! tests/scenarios/dn2.json (the other
    # scenario files are passed to coupled_run by path at run time, never read by cargo tests).
    (r"^tests/scenarios/dn2\.json$", ["digi-coupled"], True),
    # Stock firmware (untracked, so never in a git diff; listed for local edits): patchkit tests
    # (typed.rs:93 preflight.rs:24 codec.rs:14 dynamic.rs:49 conflicts.rs:16), boot
    # (src/runtime.rs:3208.., packages/emulator/boot/tests/patched_boot.rs:44), plusdrive (src/project.rs:365),
    # control (packages/emulator/control/tests/card_roundtrip.rs:47, card_export.rs:39, card_load.rs:34, src/cards.rs:309).
    (
        r"^firmware/",
        ["patchkit", "elektron-native-boot", "plusdrive-format", "digi-control"],
        False,
    ),
    # packages/emulator/sharc-gen/build.rs:35-39 (rerun-if-changed on SHARC_GEN_DIR and its .rs
    # files, include! in src/lib.rs:106-121); packages/emulator/sharc/build.rs:26 reads the same
    # dir. out/native/*/gen is untracked; listed for local edits.
    (r"^out/native/[^/]+/gen/", ["sharc-gen", "sharc-native"], True),
    # apps/desktop/src-tauri/tauri.conf.json frontendDist = ../ui/dist (embedded by tauri_build).
    (r"^apps/desktop/ui/dist/", ["digiemu-desktop"], True),
]

# Files that no Rust code reads. tools/emulator/fast_regions.json is passed to the engine through
# the ABI by Python (packages/emulator/boot/src/abi.rs:501 only mentions it in a comment); the other
# out/ inputs (mmio-trace, native/live, card images) are untracked, env-gated test inputs.
NO_RUST = re.compile(
    r"^(docs/|tools/|tests/|apps/web/|apps/desktop/(ui/|package\.json)|samples/|plus_drive/|out/|"
    r"packages/(control-client|test-support)/|\.claude/|node_modules/)"
    r"|\.md$"
    r"|^(mise\.toml|pyproject\.toml|uv\.lock|package\.json|pnpm-.*|LICENSE|NOTICE|\.gitignore|"
    r"\.gitattributes|\.editorconfig|\.git-blame-ignore-revs|\.oxlintrc\.json|\.prettier.*)$"
)


@functools.cache
def _metadata() -> dict:
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def _graph() -> tuple[dict[str, str], dict[str, set[str]]]:
    """(package name -> manifest dir relative to ROOT, package name -> its workspace dependents)."""
    packages = _metadata()["packages"]
    dirs = {
        p["name"]: pathlib.Path(p["manifest_path"]).parent.relative_to(ROOT).as_posix()
        for p in packages
    }
    dependents: dict[str, set[str]] = {name: set() for name in dirs}
    for p in packages:
        for dep in p["dependencies"]:  # normal, dev and build dependencies
            if dep["name"] in dirs and dep.get("path"):
                dependents[dep["name"]].add(p["name"])
    return dirs, dependents


def affected_packages(changed: list[str]) -> list[str] | None:
    """Workspace packages whose tests the changed files (repo-relative) can affect.

    Returns a sorted list of package names (empty: nothing Rust is affected), or None when the
    whole workspace must run: root Cargo.toml / Cargo.lock / rust-toolchain* / .cargo/*, or a
    file that fits no package, no READS row and no NO_RUST pattern.
    """
    dirs, dependents = _graph()
    seeds: set[str] = set()  # packages whose dependents also run
    readers: set[str] = set()  # packages that run alone (run-time reads)
    for raw in changed:
        path = raw.strip().removeprefix("./")
        if not path:
            continue
        if WHOLE_WORKSPACE.match(path):
            return None
        owners = [n for n, d in dirs.items() if path.startswith(d + "/")]
        if owners:
            seeds.add(max(owners, key=lambda n: len(dirs[n])))
            continue
        rows = [(pkgs, closure) for rx, pkgs, closure in READS if re.search(rx, path)]
        for pkgs, closure in rows:
            (seeds if closure else readers).update(pkgs)
        if not rows and not NO_RUST.search(path):
            return None
    todo, closure = list(seeds), set(seeds)
    while todo:
        for user in dependents[todo.pop()]:
            if user not in closure:
                closure.add(user)
                todo.append(user)
    return sorted(closure | readers)


def workspace_args() -> list[str]:
    args = ["--workspace"]
    if not (ROOT / "apps/desktop/ui/dist").is_dir():
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
    packages: list[str] = []
    if extra[:1] == ["--packages"] and len(extra) > 1:
        packages = [p for p in extra[1].split(",") if p]
        extra = extra[2:]
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
    elif packages:
        if not (ROOT / "apps/desktop/ui/dist").is_dir():
            packages = [p for p in packages if p != "digiemu-desktop"]
        sub = ["test", *(x for p in packages for x in ("-p", p)), "--release", *target]
    else:
        sub = ["test", *workspace_args(), "--release", *target]
    return cargo([*sub, *extra], env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
