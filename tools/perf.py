#!/usr/bin/env python3
"""The standard DSP performance report: where the CPU goes, not only which tier.

For each configuration (tools/fastcov.py names) it runs the coupled scenario
twice -- once clean (DSP CPU per audio second, PCM hash, tier split, kernel
calls) and once with the diagnostics on (SHARC_FAST_HIST and SHARC_FAST_COV:
interpreted instructions per PC, calls and instructions per region) -- and
ranks the DSP functions by an estimated cost:

    cost(f) = interpreted insns(f) * ns_interp
            + region calls(f)      * ns_call      (the engine round trip)
            + kernel insns(f)      * ns_kernel

The three constants are measured, not assumed: `--calibrate` runs a set of
tier configurations and fits CPU = a*interp + b*calls + k*kernel by
least squares (out/native/perf/calibration.json). Runs are unlocked unless
`--timed` (then the exclusive benchmark lock is taken, for milestone numbers).

    uv run python tools/perf.py [--config NAME ...] [--seconds 1.5] [--timed]
    uv run python tools/perf.py --calibrate
    mise run perf -- --config static-blocks+follow --seconds 1.5

Configurations are those of tools/fastcov.py (`default`, `static-blocks`,
`static-blocks+follow`, `fast-auto:256,64`, ...). The firmware comes from
DIGI_EMU_SYX like fastcov. Each run writes out/native/perf/<stamp>/report.md
and report.json.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
try:
    import coupled
    import fastcov
finally:
    sys.path.pop(0)

OUT = ROOT / "out" / "native" / "perf"
CALIB = OUT / "calibration.json"
# Used until `--calibrate` has run: the profile of 2026-10-07 (docs/notes/laneB.md).
DEFAULT_COSTS = {
    "interp": 119.0,
    "call": 100.0,
    "kernel": 0.5,
    "aot": 4.3,
    "audio": 0.0,
}
CALIBRATION_CONFIGS = [
    "static-blocks",
    "static-blocks+follow",
    "static-insns",
    "static-insns+follow",
    "fast-auto:256,64",
]


def load_costs() -> dict[str, float]:
    if CALIB.is_file():
        return {**DEFAULT_COSTS, **json.loads(CALIB.read_text())["costs"]}
    return dict(DEFAULT_COSTS)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (n x n)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular fit")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(samples: list[dict], kernel_ns: float = 0.5) -> dict[str, float]:
    """Least squares of cpu_ns - k*kernel = a*interp + b*calls.

    The kernel cost cannot be fitted: kernel instructions are about the same
    in every configuration, so they are confounded with the constant. It is
    the measured gate figure (docs/notes/laneB.md, 0.435-0.5 ns/insn). A
    per-audio-second constant is not fitted: it is collinear with the others on
    these runs and came out negative.
    """
    keys = ["interp", "call"]
    rows = [[s[k] for k in keys] for s in samples]
    ys = [s["cpu_ns"] - kernel_ns * s["kernel"] for s in samples]
    n = len(keys)
    sc = [max(abs(r[i]) for r in rows) or 1.0 for i in range(n)]
    ata = [
        [sum(r[i] * r[j] for r in rows) / (sc[i] * sc[j]) for j in range(n)]
        for i in range(n)
    ]
    aty = [
        sum(r[i] * y for r, y in zip(rows, ys, strict=True)) / sc[i] for i in range(n)
    ]
    x = solve(ata, aty)
    out = {k: x[i] / sc[i] for i, k in enumerate(keys)}
    out["kernel"] = kernel_ns
    out["aot"] = DEFAULT_COSTS["aot"]
    return out


def counts(sc: dict) -> dict:
    """Per audio second counts and CPU of a scenario report."""
    d = sc["dsp_instructions"]
    audio = sc.get("audio_seconds") or 1.0
    return {
        "cpu_ns": sc["cpu"]["dsp_thread_s"] * 1e9,
        "audio": audio,
        "interp": d.get("interpreter", 0),
        "call": d.get("fast_calls", 0),
        "kernel": d.get("fast_tier", 0),
        "aot": d.get("aot_block", 0),
        "idle": d.get("idle_skip", 0),
        "executed": d.get("instructions", 0) - d.get("idle_skip", 0),
        "pcm": (sc.get("pcm") or {}).get("sha256"),
        "ok": (sc.get("gates") or {}).get("ok"),
    }


def timed_run(binary, scenario, seconds, env, label, out, timed, timeout):
    fastcov.NO_LOCK = not timed
    report, errfile, code = fastcov.run_once(
        binary, scenario, seconds, env, label, True, out, timeout
    )
    sc = fastcov.scenario_report(report, scenario)
    if sc is None:
        raise SystemExit(f"perf: run of {label} failed (exit {code}), see {errfile}")
    return counts(sc)


def diag_run(binary, scenario, seconds, env, label, out, timeout):
    report, errfile, code = fastcov.run_once(
        binary,
        scenario,
        seconds,
        {**env, **fastcov.DIAG_ENV},
        label,
        False,
        out,
        timeout,
    )
    return fastcov.Diag(errfile.read_text(errors="replace"))


def rank(diag, funcs, costs: dict[str, float], audio: float):
    """Per-function estimated cost (ms per audio second) from a diagnostic run."""
    rows: dict[int, dict] = collections.defaultdict(
        lambda: {"name": "", "interp": 0, "calls": 0, "kernel": 0}
    )
    for pc, n in diag.hist.items():
        entry, name = funcs.of(pc)
        rows[entry]["name"] = name
        rows[entry]["interp"] += n
    for run in diag.runs:
        entry, name = funcs.of(run["root"])
        rows[entry]["name"] = name
        rows[entry]["calls"] += run["calls"]
        rows[entry]["kernel"] += run["insns"]
    out = []
    for entry, r in rows.items():
        ns = (
            r["interp"] * costs["interp"]
            + r["calls"] * costs["call"]
            + r["kernel"] * costs["kernel"]
        )
        out.append({"entry": entry, **r, "ms_per_audio_s": ns / 1e6 / audio})
    out.sort(key=lambda r: -r["ms_per_audio_s"])
    return out


def report_config(label, t, ranking, costs, top) -> str:
    audio = t["audio"]
    ex = max(t["executed"], 1)
    lines = [
        f"## {label}",
        "",
        f"- DSP CPU per audio s: **{t['cpu_ns'] / 1e9 / audio:.3f}**, PCM `{str(t['pcm'])[:16]}`, gate ok={t['ok']}",
        f"- instructions executed per audio s: {ex / audio / 1e6:.0f}M "
        f"(kernel {100 * t['kernel'] / ex:.1f}%, AOT {100 * t['aot'] / ex:.1f}%, "
        f"interpreter {100 * t['interp'] / ex:.2f}%; idle skipped {t['idle'] / audio / 1e6:.0f}M)",
        f"- region calls (engine round trips) per audio s: {t['call'] / audio / 1e6:.2f}M "
        f"({t['kernel'] / max(t['call'], 1):.1f} kernel insns per call)",
        f"- model: {t['cpu_ns'] / 1e9 / audio:.3f} measured vs "
        f"{(t['interp'] * costs['interp'] + t['call'] * costs['call'] + t['kernel'] * costs['kernel'] + t['aot'] * costs['aot'] + t['audio'] * costs['audio']) / 1e9 / audio:.3f} "
        f"estimated CPU s per audio s (ns/insn interp {costs['interp']:.0f}, "
        f"ns/call {costs['call']:.0f}, ns/kernel insn {costs['kernel']:.2f}, ns/AOT insn {costs['aot']:.2f})",
        "",
        "| function | interp insns/s | calls/s | kernel insns/s | est. ms per audio s | share |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    total = sum(r["ms_per_audio_s"] for r in ranking) or 1.0
    for r in ranking[:top]:
        lines.append(
            f"| {r['entry']:#x} {r['name']} | {r['interp'] / audio / 1e6:.2f}M | {r['calls'] / audio / 1e6:.3f}M "
            f"| {r['kernel'] / audio / 1e6:.1f}M | {r['ms_per_audio_s']:.0f} | {100 * r['ms_per_audio_s'] / total:.1f}% |"
        )
    return "\n".join(lines) + "\n"


def calibrate(binary, scenario, seconds, out, timeout, image) -> None:
    samples = []
    for spec in CALIBRATION_CONFIGS:
        label, env = fastcov.parse_config(spec)
        t = timed_run(binary, scenario, seconds, env, label, out, False, timeout)
        samples.append(t)
        print(
            f"  {label}: cpu {t['cpu_ns'] / 1e9 / t['audio']:.2f} s/audio s, "
            f"interp {t['interp'] / 1e6:.0f}M calls {t['call'] / 1e6:.1f}M kernel {t['kernel'] / 1e6:.0f}M aot {t['aot'] / 1e6:.0f}M"
        )
    costs = fit(samples)
    resid = []
    for s in samples:
        est = sum(costs[k] * s[k] for k in costs)
        resid.append(est / s["cpu_ns"])
    CALIB.parent.mkdir(parents=True, exist_ok=True)
    CALIB.write_text(
        json.dumps(
            {"costs": costs, "est_over_measured": resid, "seconds": seconds}, indent=1
        )
    )
    print("fitted ns:", {k: round(v, 2) for k, v in costs.items()})
    print("estimate / measured per run:", [round(x, 2) for x in resid])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", default="trig1-taps")
    ap.add_argument("--seconds", type=float, default=1.5)
    ap.add_argument("--config", action="append", default=None, metavar="NAME")
    ap.add_argument(
        "--timed", action="store_true", help="take the exclusive benchmark lock"
    )
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument(
        "--no-diag", action="store_true", help="skip the per-function ranking run"
    )
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--image", default="dn2-1.11")
    ap.add_argument("--gen", choices=["shared", "prod"], default="shared")
    ap.add_argument(
        "--refresh-core", action="store_true", help="regenerate the shared core first"
    )
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--timeout", type=float, default=2400)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # The shared core (`mise run core`): current semantics, no firmware-derived
    # blocks, so no AOT configs; `--gen prod` keeps the prod generated core.
    if args.gen == "shared":
        core = ROOT / "out" / "native" / "shared-core" / "gen"
        if not (core / "core_g.rs").is_file() or args.refresh_core:
            import subprocess

            subprocess.run(
                [sys.executable, str(HERE / "sharc_transpile.py"), "--strict", "--out", str(core),
                 "--work", str(core.parent / "work")],
                cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
            )  # fmt: skip
        os.environ["SHARC_GEN_DIR"] = str(core)
    binary = coupled.ensure_binary(args.no_build)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    out = pathlib.Path(args.out) if args.out else OUT / stamp
    out.mkdir(parents=True, exist_ok=True)
    if args.calibrate:
        calibrate(binary, args.scenario, args.seconds, out, args.timeout, args.image)
        return 0
    costs = load_costs()
    funcs = fastcov.Functions(args.image)
    configs = [
        fastcov.parse_config(c) for c in (args.config or ["static-blocks+follow"])
    ]
    md = [
        f"# DSP performance, {args.scenario}, {args.seconds} s, {'timed (locked)' if args.timed else 'unlocked'}",
        f"firmware {os.environ.get('DIGI_EMU_SYX', 'default')}, {stamp}",
        "",
    ]
    results = []
    for label, env in configs:
        t = timed_run(
            binary,
            args.scenario,
            args.seconds,
            env,
            label,
            out,
            args.timed,
            args.timeout,
        )
        ranking = []
        if not args.no_diag:
            diag = diag_run(
                binary, args.scenario, args.seconds, env, label, out, args.timeout
            )
            ranking = rank(diag, funcs, costs, t["audio"])
        md.append(report_config(label, t, ranking, costs, args.top))
        results.append({"config": label, "env": env, "run": t, "ranking": ranking[:50]})
    text = "\n".join(md)
    (out / "report.md").write_text(text)
    (out / "report.json").write_text(json.dumps(results, indent=1))
    print(text)
    print(f"report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
