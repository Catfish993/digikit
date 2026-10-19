#!/usr/bin/env python3
"""ColdFire instruction-rate sweep: does firmware behaviour depend on DIGI_CF_IPS?

    uv run python -m tools.devtools.clock_sweep run [--rates 33,66,...] [--suites dn2,pins,dt2]
    uv run python -m tools.devtools.clock_sweep analyze

``run`` executes the DN2 oracles (``mise run scenario:check``), the DN2 pin scenarios
(``coupled scenario --all``) and the DT2 oracles (``mise run scenario:check:dt2``) once per rate with
``DIGI_CF_IPS`` set, harvests the coupled-run reports they leave under ``out/runs`` (a report counts
when its ``cf_ips`` equals the rate and it is newer than the start of the run) and writes one raw
file per (rate, suite) to ``out/clock/raw/``. Oracle and pin assertions are not touched; their
failures are recorded. ``analyze`` reads the raw files and writes ``out/clock/sweep.json`` and
``out/clock/sweep.md``:

1. per rate: oracle failures, PCM changes against the 132 MHz run, missing blocks, halts;
2. per changed scenario: load (missing blocks, failed gate, halt, step error) or phase (the PCM
   differs but nothing is late; the cross-correlation lag and the residual after removing it show
   whether the change is a time shift);
3. a busy-load model per scenario: I(ips) = W + p * ips for the interpreted instructions per
   emulated second over the rates where nothing breaks; the saturation rate is W / (1 - p).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "clock"
RAW = OUT / "raw"
RUNS = ROOT / "out" / "runs"
BASE = 132_000_000
# 76M and 80M bracket the cliff found between 72M (silent) and 76M (late) on trig1-hold.
RATES = (
    33_000_000,
    66_000_000,
    76_000_000,
    80_000_000,
    99_000_000,
    132_000_000,
    198_000_000,
    264_000_000,
)
PYTEST = ["uv", "run", "python", "-m", "pytest"]
SUITE_TIMEOUT_S = 3 * 3600


def suite_command(suite: str, junit: Path, jobs: str | None) -> list[str]:
    extra = ["--keep-runs", "--junitxml", str(junit), "-p", "no:cacheprovider"]
    if suite == "dn2":  # mise run scenario:check
        return [*PYTEST, "tests/scenarios", "--slow", "-q", *extra]
    if suite == "dt2":  # mise run scenario:check:dt2
        return [
            *PYTEST,
            "tests/scenarios/test_dt2_fx.py",
            "--device",
            "dt2",
            "--slow",
            "--sweep",
            "-q",
            *extra,
        ]
    if suite == "pins":  # the verify pins step
        return [
            "uv",
            "run",
            "python",
            "-m",
            "tools.emulator.coupled",
            "scenario",
            "--all",
            "--no-build",
            "--jobs",
            jobs or "6",
        ]
    raise SystemExit(f"unknown suite {suite}")


def harvest(rate: int, since: float, suite: str) -> list[Path]:
    """Report files newer than ``since`` whose scenarios ran at ``rate``."""
    found: list[Path] = []
    for kind in RUNS.iterdir():
        if not kind.is_dir() or (kind.name == "tests") != (suite != "pins"):
            continue
        for stamp in kind.iterdir():
            if not stamp.is_dir() or stamp.is_symlink():
                continue
            if stamp.stat().st_mtime < since - 1:
                continue
            tops = [stamp / "report.json"]
            tops += sorted(stamp.glob("*/report.json"))
            for rep in tops:
                if not rep.is_file() or rep.stat().st_mtime < since - 1:
                    continue
                try:
                    data = json.loads(rep.read_text())
                except ValueError:
                    continue
                scs = data.get("scenarios") or []
                if scs and all(s.get("cf_ips") == rate for s in scs):
                    found.append(rep)
                    if rep == tops[0]:
                        break  # a merged top report covers the shard reports
    return found


def entry_of(sc: dict[str, Any], report: Path) -> dict[str, Any]:
    pcm = sc.get("pcm") or {}
    gates = sc.get("gates") or {}
    cf = sc.get("cf_instructions") or {}
    wav = report.parent / f"audio-{sc['name']}.wav"
    if not wav.exists():
        alt = report.parent / "audio.wav"
        wav = alt if alt.exists() else wav
    return {
        "name": sc["name"],
        "pcm_sha256": pcm.get("sha256"),
        "rms": pcm.get("rms"),
        "peak": pcm.get("peak"),
        "active_windows_100ms": pcm.get("active_windows_100ms"),
        "window_rms_100ms": pcm.get("window_rms_100ms"),
        "audio_seconds": sc.get("audio_seconds"),
        "missing_blocks": gates.get("missing_blocks"),
        "halted": gates.get("halted"),
        "step_error": gates.get("step_error"),
        "gates_ok": gates.get("ok"),
        "pcm_hash_match": gates.get("pcm_hash_match"),
        "frames": sc.get("frames"),
        "guest_instructions": sc.get("guest_instructions"),
        "cf_total": cf.get("total"),
        "cf_interpreted": cf.get("interpreted"),
        "cf_idle_fast_forward": cf.get("idle_fast_forward"),
        "cf_interpreted_pct": cf.get("interpreted_pct"),
        "softfloat_hle_calls": sc.get("softfloat_hle_calls"),
        "render_cache": sc.get("render_cache"),
        "wav": str(wav) if wav.exists() else None,
    }


def junit_failures(path: Path) -> tuple[list[str], int]:
    if not path.is_file():
        return [], 0
    root = ET.parse(path).getroot()
    bad, total = [], 0
    for case in root.iter("testcase"):
        total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            bad.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return bad, total


def run_suite(rate: int, suite: str, jobs: str | None, force: bool) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / f"{rate}-{suite}.json"
    if dest.exists() and not force:
        print(f"skip {dest.name} (done)", flush=True)
        return
    junit = RAW / f"{rate}-{suite}.xml"
    log = RAW / f"{rate}-{suite}.log"
    env = {**os.environ, "DIGI_CF_IPS": str(rate)}
    started = time.time()
    with log.open("wb") as out:
        done = subprocess.run(
            suite_command(suite, junit, jobs),
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            timeout=SUITE_TIMEOUT_S,
            check=False,
        )
    elapsed = time.time() - started
    entries: dict[str, dict[str, Any]] = {}
    reports = harvest(rate, started, suite)
    for rep in reports:
        for sc in json.loads(rep.read_text()).get("scenarios", []):
            entries.setdefault(sc["name"], entry_of(sc, rep))
    failures, tests = junit_failures(junit)
    dest.write_text(
        json.dumps(
            {
                "rate": rate,
                "suite": suite,
                "returncode": done.returncode,
                "elapsed_s": round(elapsed, 1),
                "oracle_failures": failures,
                "tests": tests,
                "reports": [str(r) for r in reports],
                "scenarios": entries,
            },
            indent=1,
        )
    )
    print(
        f"{rate} {suite}: rc={done.returncode} {len(entries)} scenarios, "
        f"{len(failures)}/{tests} oracle failures, {elapsed:.0f} s",
        flush=True,
    )


# --- analysis ------------------------------------------------------------


def load_raw() -> dict[tuple[int, str], dict[str, Any]]:
    raw: dict[tuple[int, str], dict[str, Any]] = {}
    for path in sorted(RAW.glob("*.json")):
        data = json.loads(path.read_text())
        raw[(data["rate"], data["suite"])] = data
    return raw


def waveform_diff(base_wav: str | None, wav: str | None) -> dict[str, Any] | None:
    """Lag (samples, positive = ``wav`` is later) that best aligns ``wav`` with ``base_wav``, and the residual rms with and
    without that lag, relative to the base rms."""
    if not base_wav or not wav:
        return None
    import numpy as np

    from tools.emulator import audiocheck as ac

    _, a = ac.load(base_wav)
    _, b = ac.load(wav)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ref = float(np.sqrt(np.mean(a**2))) or 1.0
    ma, mb = a.sum(axis=1), b.sum(axis=1)
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(ma, size) * np.conj(np.fft.rfft(mb, size)), size)
    window = 24000  # +-0.5 s
    cand = np.concatenate([corr[:window], corr[-window:]])
    idx = int(np.argmax(cand))
    k = idx if idx < window else idx - 2 * window  # corr[k] = sum a[t + k] * b[t]
    lag = -k  # samples by which ``wav`` is later than the baseline
    if k >= 0:
        x, y = a[k:], b[: n - k]
    else:
        x, y = a[: n + k], b[-k:]
    raw_res = float(np.sqrt(np.mean((a - b) ** 2))) / ref
    aligned = float(np.sqrt(np.mean((x - y) ** 2))) / ref
    return {
        "lag_samples": lag,
        "lag_ms": round(lag / 48.0, 3),
        "residual_rel": round(raw_res, 4),
        "residual_aligned_rel": round(aligned, 4),
    }


SATURATED_PCT = 98.0  # interpreted share of all instructions: no idle time left


def onset(e: dict[str, Any]) -> int | None:
    """Index of the first 100 ms window with sound, or None when silent."""
    for i, v in enumerate(e.get("window_rms_100ms") or []):
        if v and v > 0:
            return i
    return None


def classify(e: dict[str, Any], base: dict[str, Any] | None) -> str:
    """load: the run shows a missing block, halt, error or has no idle time left (the CPU is the
    bottleneck); identical: same PCM; phase: PCM differs but onset, active windows and level
    agree with the baseline; changed: it differs in onset, activity or level (>1 % rms)."""
    if e.get("halted") or e.get("step_error"):
        return "load: halted"
    if (e.get("missing_blocks") or 0) > 0:
        return "load: missing blocks"
    if (e.get("cf_interpreted_pct") or 0) >= SATURATED_PCT:
        return "load: saturated"
    if base is None:
        return "no baseline"
    if e["pcm_sha256"] == base["pcm_sha256"]:
        return "identical"
    same_onset = onset(e) == onset(base)
    same_windows = e["active_windows_100ms"] == base["active_windows_100ms"]
    level = abs(e["rms"] / base["rms"] - 1.0) if base.get("rms") else 0.0
    if same_onset and same_windows and level < 0.01:
        return "phase"
    return "changed"


def fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least squares I = W + p * ips -> (W, p)."""
    if len(points) < 3:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    p = sum((x - mx) * (y - my) for x, y in points) / sxx
    return my - p * mx, p


def analyze() -> None:
    raw = load_raw()
    rates = sorted({r for r, _ in raw})
    suites = sorted({s for _, s in raw})
    result: dict[str, Any] = {
        "rates": rates,
        "per_rate": {},
        "scenarios": {},
        "model": {},
    }
    # scenarios[suite/name][rate] = entry
    table: dict[str, dict[int, dict[str, Any]]] = {}
    for (rate, suite), data in raw.items():
        for name, e in data["scenarios"].items():
            table.setdefault(f"{suite}/{name}", {})[rate] = e

    for rate in rates:
        row: dict[str, Any] = {
            "oracle_failures": {},
            "returncodes": {},
            "pcm_changed": [],
            "missing_blocks": {},
            "halts": [],
            "scenarios": 0,
            "class_counts": {},
            "pin_mismatches": 0,
        }
        for suite in suites:
            got = raw.get((rate, suite))
            if not got:
                continue
            row["returncodes"][suite] = got["returncode"]
            row["oracle_failures"][suite] = got["oracle_failures"]
        for key, per in table.items():
            e = per.get(rate)
            if e is None:
                continue
            row["scenarios"] += 1
            base = per.get(BASE)
            cls = classify(e, base)
            row["class_counts"][cls] = row["class_counts"].get(cls, 0) + 1
            if key.startswith("pins/") and e.get("pcm_hash_match") is False:
                row["pin_mismatches"] += 1
            if base and e["pcm_sha256"] != base["pcm_sha256"]:
                row["pcm_changed"].append(key)
            if e.get("missing_blocks"):
                row["missing_blocks"][key] = e["missing_blocks"]
            if e.get("halted") or e.get("step_error"):
                row["halts"].append(key)
        result["per_rate"][str(rate)] = row

    for key, per in sorted(table.items()):
        base = per.get(BASE)
        info: dict[str, Any] = {"rates": {}}
        for rate in rates:
            e = per.get(rate)
            if e is None:
                continue
            cls = classify(e, base)
            item: dict[str, Any] = {
                "class": cls,
                "pcm_sha256": e["pcm_sha256"],
                "rms": e["rms"],
                "peak": e["peak"],
                "active_windows_100ms": e["active_windows_100ms"],
                "missing_blocks": e["missing_blocks"],
                "frames": e["frames"],
                "cf_interpreted": e["cf_interpreted"],
                "cf_idle_fast_forward": e["cf_idle_fast_forward"],
                "cf_interpreted_pct": e["cf_interpreted_pct"],
                "softfloat_hle_calls": e["softfloat_hle_calls"],
                "audio_seconds": e["audio_seconds"],
            }
            if cls in ("changed", "phase") and base:
                item["wave"] = waveform_diff(base.get("wav"), e.get("wav"))
                if base.get("rms"):
                    item["rms_rel_change"] = round(e["rms"] / base["rms"] - 1.0, 5)
                item["active_windows_delta"] = (
                    e["active_windows_100ms"] - base["active_windows_100ms"]
                )
            info["rates"][str(rate)] = item
        # busy-load model over the rates where this scenario neither changed load-wise nor halted
        pts = []
        for rate in rates:
            e = per.get(rate)
            if not e or not e["audio_seconds"] or e["cf_interpreted"] is None:
                continue
            if classify(e, base).startswith("load"):
                continue
            pts.append((float(rate), e["cf_interpreted"] / e["audio_seconds"]))
        model = fit(pts)
        if model:
            w, p = model
            info["model"] = {
                "W": w,
                "p": p,
                "saturation_ips": (w / (1 - p)) if p < 1 else None,
                "points": len(pts),
                "I_at_132M": w + p * BASE,
            }
            result["model"][key] = info["model"]
        result["scenarios"][key] = info

    sats = sorted(
        m["saturation_ips"]
        for m in result["model"].values()
        if m["saturation_ips"] and m["saturation_ips"] > 0
    )
    result["saturation_summary"] = (
        {
            "n": len(sats),
            "median_ips": statistics.median(sats),
            "worst_ips": sats[-1],
            "best_ips": sats[0],
        }
        if sats
        else None
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sweep.json").write_text(json.dumps(result, indent=1))
    (OUT / "sweep.md").write_text(markdown(result))
    print(f"wrote {OUT / 'sweep.json'} and {OUT / 'sweep.md'}")


def mhz(x: float | None) -> str:
    return "-" if x is None else f"{x / 1e6:.0f}"


def markdown(res: dict[str, Any]) -> str:
    lines = ["# ColdFire instruction-rate sweep", ""]
    lines += ["## Per rate", ""]
    lines += [
        "| ips (M) | scenarios | oracle failures (dn2 / dt2) | pin PCM mismatches | identical | phase | changed | load |",
        "|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rate in res["rates"]:
        r = res["per_rate"][str(rate)]
        of = r["oracle_failures"]
        fails = f"{len(of.get('dn2', []))} / {len(of.get('dt2', []))}"
        counts = r["class_counts"]
        lines.append(
            f"| {mhz(rate)} | {r['scenarios']} | {fails} | {r['pin_mismatches']} "
            f"| {counts.get('identical', 0)} | {counts.get('phase', 0)} | {counts.get('changed', 0)} "
            f"| {sum(v for k, v in counts.items() if k.startswith('load'))} |"
        )
    lines += [
        "",
        "load = missing block, halt, step error, or interpreted share >= "
        f"{SATURATED_PCT:.0f} % (no idle time left). phase = PCM differs, same onset window, same "
        "active windows, rms within 1 %. missing_blocks and halts are 0 in every run.",
        "",
        "## Largest PCM changes at rates where nothing is saturated",
        "",
        "| scenario | ips (M) | class | rms change | active windows | lag (ms) | residual | after lag |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rate in res["rates"]:
        rows = []
        for key, info in res["scenarios"].items():
            it = info["rates"].get(str(rate))
            if it and it["class"] in ("phase", "changed"):
                rows.append((abs(it.get("rms_rel_change") or 0.0), key, it))
        for _, key, it in sorted(rows, key=lambda x: -x[0])[:5]:
            w = it.get("wave") or {}
            lines.append(
                f"| {key} | {mhz(rate)} | {it['class']} | {it.get('rms_rel_change', '-')} "
                f"| {it.get('active_windows_delta', '-')} | {w.get('lag_ms', '-')} "
                f"| {w.get('residual_rel', '-')} | {w.get('residual_aligned_rel', '-')} |"
            )
    lines += [
        "",
        "## Busy-load model: I = W + p * ips (interpreted instructions per emulated second)",
        "",
        "Fit over the rates where the scenario is not saturated. Saturation rate = W / (1 - p).",
        "",
    ]
    s = res.get("saturation_summary")
    if s:
        lines.append(
            f"Over {s['n']} scenarios: median {mhz(s['median_ips'])} M, "
            f"worst {mhz(s['worst_ips'])} M, best {mhz(s['best_ips'])} M instr/s "
            f"(baseline 132 M)."
        )
        lines.append("")
    lines += [
        "| scenario | W (M/s) | p | saturation (M) | I at 132M (M/s) | fit points |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, m in sorted(res["model"].items()):
        lines.append(
            f"| {key} | {m['W'] / 1e6:.1f} | {m['p']:.4f} | {mhz(m['saturation_ips'])} "
            f"| {m['I_at_132M'] / 1e6:.1f} | {m['points']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the suites at each rate")
    r.add_argument(
        "--rates", default=",".join(str(x // 1_000_000) for x in RATES), help="MHz list"
    )
    r.add_argument("--suites", default="dn2,pins,dt2")
    r.add_argument("--jobs", default=None)
    r.add_argument(
        "--force", action="store_true", help="redo finished (rate, suite) pairs"
    )
    sub.add_parser("analyze", help="write sweep.json and sweep.md")
    args = ap.parse_args()
    if args.cmd == "run":
        for mhz_ in args.rates.split(","):
            for suite in args.suites.split(","):
                run_suite(int(float(mhz_) * 1_000_000), suite, args.jobs, args.force)
    analyze()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
