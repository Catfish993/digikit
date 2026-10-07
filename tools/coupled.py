#!/usr/bin/env python3
"""Build and run the headless coupled runner (``coupled-run``) and bisect DSP tiers.

``coupled-run`` (native/coupled/src/bin/coupled_run.rs) restores the DN2 ready
session and plays a scenario of panel events, reporting the PCM hash and RMS.
The binary is built into the prod generated-core target dir
(tools/native_target.py), so it reuses the compiled ``sharc-gen``.

    uv run python tools/coupled.py build
    uv run python tools/coupled.py scenario NAME [NAME ...] [--wav] [--out JSON] [-- EXTRA...]
    uv run python tools/coupled.py scenario --all [--device dn2|dt2]
    uv run python tools/coupled.py replay FILE [--wav] [--out JSON]
    uv run python tools/coupled.py bisect NAME [--seconds S]
    uv run python tools/coupled.py test-quick

Every command except ``build`` and ``test-quick`` builds first (a no-op when
current) unless ``--no-build`` is given. ``bisect`` finds which DSP tier changes
a scenario's PCM: the interpreter-only reference (cached under
``out/native/bisect/<name>/``) against the default tiers, then single changes
from the default (no fast regions, no AOT, one region or cluster root removed).
Exit codes: ``scenario`` and ``replay`` return coupled-run's (0 ok, 2 gate
failure, 1 error); ``bisect`` returns 0 when the default matches the reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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

TOOLCHAIN = digi_env.TOOLCHAIN
FEATURES = os.environ.get("DIGI_COUPLED_FEATURES", "coupled-audio")
BINARY_NAME = "coupled-run"
BISECT_DIR = ROOT / "out" / "native" / "bisect"
RUNS_DIR = digi_env.RUNS_ROOT


def benchlock(mode: str, label: str):
    """Lock for builds (shared) and timed runs (exclusive), see benchlock.py."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        import benchlock as module
    finally:
        sys.path.pop(0)
    return module.hold(mode, label)


# Pure helpers


def run_args(
    binary: str | os.PathLike[str], inputs: dict[str, pathlib.Path], extra: list[str]
) -> list[str]:
    """The coupled-run argv: inputs first, then the extra arguments in order."""
    return [
        str(binary),
        "--syx", str(inputs["firmware"]),
        "--dsp-image", str(inputs["DSP image"]),
        "--dsp-state", str(inputs["DSP state"]),
        "--cf-snapshot", str(inputs["CF snapshot"]),
        *extra,
    ]  # fmt: skip


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def bisect_configs(fast: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    """Single-change configurations from the default (not reference or default)."""
    regions = split_list(fast.get("SHARC_FAST_REGIONS", ""))
    roots = split_list(fast.get("SHARC_BLOCKTRANS", ""))

    def env(rs: list[str], cs: list[str]) -> dict[str, str]:
        return {"SHARC_FAST_REGIONS": ",".join(rs), "SHARC_BLOCKTRANS": ",".join(cs)}

    configs = [
        ("no-fast", env([], [])),
        ("no-aot", {**env(regions, roots), "SHARC_AOT": "0"}),
    ]
    for region in regions:
        rest = [r for r in regions if r != region]
        configs.append((f"fast-minus-{region}", env(rest, roots)))
    for root in roots:
        rest = [c for c in roots if c != root]
        configs.append((f"roots-minus-{root}", env(regions, rest)))
    return configs


def first_jump(windows: list[float], db: float = 6.0) -> int | None:
    """First window whose RMS differs from the previous by more than ``db`` dB.

    RMS 0 is -inf dB: a jump from or to silence counts when the other side is
    above 1e-4.
    """
    for i in range(1, len(windows)):
        prev, cur = windows[i - 1], windows[i]
        if prev == 0 and cur == 0:
            continue
        if prev == 0 or cur == 0:
            if max(prev, cur) > 1e-4:
                return i
            continue
        if abs(20 * math.log10(cur / prev)) > db:
            return i
    return None


def first_divergence(a: list[float], b: list[float], rel: float = 1e-6) -> int | None:
    """First index where a and b differ by more than ``rel`` relative."""
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        scale = max(abs(x), abs(y))
        if scale > 0 and abs(x - y) > rel * scale:
            return i
    if len(a) != len(b):
        return min(len(a), len(b))
    return None


def scenario_results(report: dict) -> list[dict]:
    """name, sha256, ok and 100 ms RMS windows of each scenario in a report."""
    results = []
    for sc in report.get("scenarios", []):
        pcm = sc.get("pcm") or {}
        results.append(
            {
                "name": sc.get("name"),
                "sha256": pcm.get("sha256"),
                "ok": (sc.get("gates") or {}).get("ok"),
                "windows": list(pcm.get("window_rms_100ms") or []),
            }
        )
    return results


# Paths and processes


def generated() -> pathlib.Path:
    gen = digi_env.generated_dir()
    if not gen.is_dir():
        raise SystemExit(f"coupled: generated core dir is missing: {gen}")
    return gen


def inputs(device: str = "dn2") -> dict[str, pathlib.Path]:
    found = digi_env.ready_inputs(device=device)
    missing = [f"{k}: {v}" for k, v in found.items() if not pathlib.Path(v).exists()]
    if missing:
        raise SystemExit("coupled: missing inputs:\n  " + "\n  ".join(missing))
    return found


def binary_path() -> pathlib.Path:
    return native_target.target_dir(generated()) / "release" / BINARY_NAME


def cargo(args: list[str]) -> list[str]:
    return ["rustup", "run", TOOLCHAIN, "cargo", *args]


def build() -> pathlib.Path:
    gen = generated()
    target = native_target.target_dir(gen)
    env = {**digi_env.rust_env(), "SHARC_GEN_DIR": str(gen)}
    command = cargo(
        [
            "build", "--release", "--locked",
            "--manifest-path", "native/coupled/Cargo.toml",
            "--features", FEATURES,
            "--bin", BINARY_NAME,
            "--target-dir", str(target),
        ]
    )  # fmt: skip
    with benchlock("shared", "coupled-run build"):
        done = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if done.returncode != 0:
        raise SystemExit(done.returncode)
    binary = target / "release" / BINARY_NAME
    if not binary.is_file():
        raise SystemExit(f"coupled: build left no binary at {binary}")
    return binary


def ensure_binary(no_build: bool) -> pathlib.Path:
    if not no_build:
        return build()
    binary = binary_path()
    if not binary.is_file():
        raise SystemExit(f"coupled: no binary at {binary} (drop --no-build)")
    return binary


def run_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **digi_env.fast_env()}
    env.update(overrides or {})
    return env


def run_kind(selection: list[str]) -> str:
    """The out/runs/<kind> name of a run: its scenario names, or the replay file."""
    names = [
        b for a, b in zip(selection, selection[1:], strict=False) if a == "--scenario"
    ]
    if names:
        return "+".join(names)
    if not selection:
        return "all"
    return "replay-" + pathlib.Path(selection[-1]).stem


def read_report(path: pathlib.Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def summarize(report_path: pathlib.Path) -> None:
    if not report_path.is_file():
        print(f"no report at {report_path}")
        return
    for r in scenario_results(read_report(report_path)):
        jump = first_jump(r["windows"])
        sha = (r["sha256"] or "-")[:16]
        print(
            f"{r['name']:<16} sha256 {sha} ok={r['ok']} "
            f"first >6dB window {'-' if jump is None else jump}"
        )
    print(f"report: {report_path}")


def run_and_report(binary: pathlib.Path, selection: list[str], args) -> int:
    """Run into out/runs/<kind>/<stamp>/ (report.json, audio.wav, logs/); --out
    names the report instead and the rest goes next to it."""
    out = (
        pathlib.Path(args.out)
        if args.out
        else digi_env.run_dir(run_kind(selection)) / "report.json"
    )
    diag = digi_env.parse_diag(args.diag or "")
    wav = args.wav or any(n == "pcm" for n, _ in diag)
    extra = [*selection, "--out", str(out)]
    if wav:
        extra += ["--wav", str(out.parent / "audio.wav")]
    if getattr(args, "seconds", None) is not None:
        extra += ["--seconds", str(args.seconds)]
    extra += args.extra
    device = getattr(args, "device", "dn2")
    if device != "dn2":
        extra = ["--scenarios", str(ROOT / "scenarios" / f"{device}.json"), *extra]
    argv = run_args(binary, inputs(device), extra)
    env = run_env(digi_env.diag_env(diag, out.parent))
    if digi_env.diag_stderr(diag):
        with (out.parent / "logs" / "stderr.log").open("wb") as err:
            done = subprocess.run(argv, cwd=ROOT, env=env, stderr=err, check=False)
    else:
        done = subprocess.run(argv, cwd=ROOT, env=env, check=False)
    summarize(out)
    if diag:
        print(f"diagnostics: {out.parent / 'logs'}")
    return done.returncode


def cmd_build(args) -> int:
    binary = build()
    print(binary)
    return 0


def want_dump(args) -> None:
    """`--diag dump` needs the fast-dump build feature."""
    global FEATURES
    if any(n == "dump" for n, _ in digi_env.parse_diag(args.diag or "")):
        FEATURES += ",fast-dump"


def cmd_scenario(args) -> int:
    want_dump(args)
    binary = ensure_binary(args.no_build)
    if not args.names and not args.all:
        raise SystemExit("coupled: give scenario names or --all")
    # No --scenario means every scenario of the file; coupled-run exits 2 on
    # any expect_sha256 mismatch or failed gate.
    selection = [x for name in args.names for x in ("--scenario", name)]
    return run_and_report(binary, selection, args)


def cmd_replay(args) -> int:
    want_dump(args)
    binary = ensure_binary(args.no_build)
    return run_and_report(binary, ["--replay", args.file], args)


def file_digest(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bisect_run(binary, name, seconds, label, overrides) -> pathlib.Path:
    """Run one configuration, writing out/native/bisect/<name>/<label>.json."""
    out = BISECT_DIR / name / f"{label}.json"
    return bisect_run_to(binary, name, seconds, out, overrides)


def bisect_run_to(binary, name, seconds, out, overrides) -> pathlib.Path:
    extra = ["--scenario", name, "--out", str(out)]
    if seconds is not None:
        extra += ["--seconds", str(seconds)]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    done = subprocess.run(
        run_args(binary, inputs(), extra),
        cwd=ROOT,
        env=run_env(overrides),
        check=False,
    )
    if done.returncode == 1 or not out.is_file():
        raise SystemExit(f"coupled: run failed for {out.stem} ({done.returncode})")
    return out


def result_of(path: pathlib.Path, name: str) -> dict:
    for r in scenario_results(read_report(path)):
        if r["name"] == name:
            return r
    raise SystemExit(f"coupled: no scenario {name} in {path}")


def cmd_bisect(args) -> int:
    binary = ensure_binary(args.no_build)
    name, seconds = args.name, args.seconds
    key = hashlib.sha256(
        "\0".join(
            [file_digest(binary), name, str(seconds), str(generated().resolve())]
        ).encode()
    ).hexdigest()
    ref_path = BISECT_DIR / name / f"reference-{key[:16]}.json"
    if not ref_path.is_file():
        print("interpreter-only reference (slow, cached)", flush=True)
        bisect_run_to(
            binary,
            name,
            seconds,
            ref_path,
            {"SHARC_FAST_REGIONS": "", "SHARC_BLOCKTRANS": "", "SHARC_AOT": "0"},
        )
    ref = result_of(ref_path, name)

    fast = digi_env.fast_env()
    base = {
        "SHARC_FAST_REGIONS": fast.get("SHARC_FAST_REGIONS", ""),
        "SHARC_BLOCKTRANS": fast.get("SHARC_BLOCKTRANS", ""),
    }
    default_path = bisect_run(binary, name, seconds, "default", base)
    default = result_of(default_path, name)
    if default["sha256"] == ref["sha256"]:
        print("all tiers agree with the interpreter")
        return 0
    print(
        f"default differs from the reference ({ref['sha256']} vs {default['sha256']})"
    )

    suspects = []
    for label, overrides in bisect_configs(fast):
        path = bisect_run(binary, name, seconds, label, overrides)
        matches = result_of(path, name)["sha256"] == ref["sha256"]
        print(f"{label:<28} {'matches reference' if matches else 'differs'}")
        if matches:
            suspects.append(label)
    print("suspects (hash equals the reference): " + (", ".join(suspects) or "none"))
    index = first_divergence(default["windows"], ref["windows"])
    if index is None:
        print("window_rms_100ms: no divergence above 1e-6 relative")
    else:
        print(f"first diverging 100 ms window: {index} (t = {index / 10:.1f} s)")
    return 1


def cmd_test_quick(args) -> int:
    gen = generated()
    nogen = native_target.target_dir("")
    prod = native_target.target_dir(gen)
    base_env = digi_env.rust_env()
    base_env.pop("SHARC_GEN_DIR", None)
    steps = [
        (
            "native/live",
            base_env,
            ["--manifest-path", "native/live/Cargo.toml", "--lib",
             "--target-dir", str(nogen)],
        ),
        (
            "native/boot",
            base_env,
            ["--manifest-path", "native/boot/Cargo.toml", "--features", "play",
             "--lib", "--target-dir", str(nogen)],
        ),
        (
            "native/coupled",
            {**base_env, "SHARC_GEN_DIR": str(gen)},
            ["--manifest-path", "native/coupled/Cargo.toml",
             "--features", FEATURES, "--lib", "--bins",
             "--target-dir", str(prod)],
        ),
    ]  # fmt: skip
    for label, env, tail in steps:
        command = cargo(["test", "--release", "--locked", *tail])
        with benchlock("shared", f"cargo test {label}"):
            done = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if done.returncode != 0:
            return done.returncode
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = top.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)
        return p

    def run_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-build", action="store_true", help="skip the cargo build")
        p.add_argument(
            "--wav", action="store_true", help="write audio.wav next to the report"
        )
        p.add_argument(
            "--out",
            help="report JSON (default out/runs/<scenario>/<stamp>/report.json)",
        )
        p.add_argument(
            "--diag",
            metavar="LIST",
            help="diagnostics into <run>/logs/: "
            + ",".join(digi_env.DIAG)
            + " (docs/INSTRUMENTATION.md)",
        )

    add("build", cmd_build, "build coupled-run")
    p = add("scenario", cmd_scenario, "run named scenarios")
    p.add_argument("names", nargs="*", metavar="NAME")
    p.add_argument("--all", action="store_true", help="run every scenario of the file")
    p.add_argument("--device", choices=["dn2", "dt2"], default="dn2")
    p.add_argument("--seconds", type=float)
    run_flags(p)
    p = add("replay", cmd_replay, "replay a scenario or diagnostics export")
    p.add_argument("file", metavar="FILE")
    run_flags(p)
    p = add("bisect", cmd_bisect, "find which DSP tier changes a scenario's PCM")
    p.add_argument("name", metavar="NAME")
    p.add_argument("--seconds", type=float)
    p.add_argument("--no-build", action="store_true", help="skip the cargo build")
    add("test-quick", cmd_test_quick, "fast cargo tests with the pinned toolchain")
    return top


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1 :]
    args = parser().parse_args(argv)
    args.extra = extra
    if extra and args.command not in ("scenario", "replay"):
        raise SystemExit(
            "coupled: extra arguments after -- only apply to scenario/replay"
        )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
