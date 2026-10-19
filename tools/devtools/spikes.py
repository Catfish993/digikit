"""Find real-time spikes in a DIGI_SPIKE_LOG directory.

The DSP worker writes dsp.jsonl (one record per 0.1 audio s) and the coupled
run loop writes cf.jsonl (one record per ~20 ms of wall time), both on one
process clock. For each DSP window this reports how fast audio was produced
(audio s per wall s), where the wall time went and the audio queue, and it
names the largest cost of every window that fell behind real time:

- dsp: the DSP thread was busy rendering;
- compile: fast-tier region builds and compiles on the DSP thread;
- cf: the DSP waited for frames while the ColdFire thread was emulating;
- ui-gap: the DSP waited while no step_chunk call ran (the webview pump);
- paced: the DSP waited because the free-running emulator held the audio
  queue at its target (not a problem, and not counted as behind).

The DSP load line (busy time per audio s, and per window) is the headroom:
above 1.0 the DSP alone cannot keep up.

    DIGI_SPIKE_LOG=DIR mise run workbench  # or any coupled-run
    uv run python -m tools.devtools.spikes DIR [--top 20] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

PERIODS_PER_S = 1500
SOURCE_RATE = 48_000
PERIOD_MS = 1e3 / PERIODS_PER_S
# A window whose lowest queue is at least this share of the pace target was
# held back by pacing.
PACED_FRACTION = 0.85


@dataclass
class Window:
    t_s: float
    audio_s: float
    wall_ms: float
    speed: float
    dsp_busy_ms: float
    dsp_wait_ms: float
    compile_ms: float
    busy_max_ms: float
    over: int
    cf_work_ms: float
    ui_gap_ms: float
    gap_max_ms: float
    queue_ms: float | None
    pace_ms: float | None
    underruns: int | None
    cause: str

    @property
    def dsp_load(self) -> float:
        """DSP busy time per audio time in this window."""
        return self.dsp_busy_ms / (self.audio_s * 1e3) if self.audio_s else 0.0

    @property
    def behind(self) -> bool:
        return self.speed < 1.0 and self.cause != "paced"


def read_jsonl(path: Path) -> list[dict]:
    """Records of one log; a torn last line (killed process) is dropped."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return out


def queue_ms(queue: dict | None) -> float | None:
    """Queued audio in ms: the device ring plus the feeder's pending input."""
    if not queue:
        return None
    rate = queue.get("device_rate") or SOURCE_RATE
    device = queue["queued_device_frames"] / rate
    pending = queue["feeder_pending_source_frames"] / SOURCE_RATE
    return 1e3 * (device + pending)


def cause_of(
    wall: float,
    wait: float,
    compile_ns: float,
    cf_work: float,
    gap: float,
    queue: float | None,
    pace: float | None,
) -> str:
    """The largest cost of a window (times in ns, queue and pace in ms)."""
    if wait > 0.25 * wall:
        if queue is not None and pace and queue >= PACED_FRACTION * pace:
            return "paced"
        return "ui-gap" if gap >= cf_work else "cf"
    if compile_ns >= 0.1 * wall:
        return "compile"
    return "dsp"


def join(dsp: list[dict], cf: list[dict]) -> list[Window]:
    """One Window per DSP record, with the cf records that ended inside it."""
    out = []
    j = 0
    for rec in dsp:
        t0, t = rec["t0_ns"], rec["t_ns"]
        wall = max(t - t0, 1)
        while j < len(cf) and cf[j]["t_ns"] <= t0:
            j += 1
        gap = work = gap_max = 0
        queues: list[float] = []
        pace = None
        underruns = None
        while j < len(cf) and cf[j]["t_ns"] <= t:
            c = cf[j]
            gap += c["gap_ns"]
            gap_max = max(gap_max, c["gap_max_ns"])
            work += max(c["step_ns"] - c["reply_wait_ns"], 0)
            q = queue_ms(c.get("queue"))
            if q is not None:
                queues.append(q)
                underruns = c["queue"]["underrun_events"]
                if "pace_seconds" in c["queue"]:
                    pace = 1e3 * c["queue"]["pace_seconds"]
            j += 1
        compile_ns = rec["compile_ns"] + rec["build_ns"]
        audio_s = rec["periods"] / PERIODS_PER_S
        queue = min(queues) if queues else None
        out.append(
            Window(
                t_s=t / 1e9,
                audio_s=audio_s,
                wall_ms=wall / 1e6,
                speed=audio_s / (wall / 1e9),
                dsp_busy_ms=rec["busy_ns"] / 1e6,
                dsp_wait_ms=rec["wait_ns"] / 1e6,
                compile_ms=compile_ns / 1e6,
                busy_max_ms=rec["busy_max_ns"] / 1e6,
                over=rec["over"],
                cf_work_ms=work / 1e6,
                ui_gap_ms=gap / 1e6,
                gap_max_ms=gap_max / 1e6,
                queue_ms=queue,
                pace_ms=pace,
                underruns=underruns,
                cause=cause_of(
                    wall, rec["wait_ns"], compile_ns, work, gap, queue, pace
                ),
            )
        )
    return out


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summarise(windows: list[Window]) -> dict:
    behind = [w for w in windows if w.behind]
    queues = [w.queue_ms for w in windows if w.queue_ms is not None]
    underruns = [w.underruns for w in windows if w.underruns is not None]
    audio = sum(w.audio_s for w in windows)
    wall = sum(w.wall_ms for w in windows) / 1e3
    loads = [w.dsp_load for w in windows]
    return {
        "windows": len(windows),
        "audio_s": audio,
        "wall_s": wall,
        "speed": audio / wall if wall else 0.0,
        "behind": len(behind),
        "paced": sum(w.cause == "paced" for w in windows),
        "causes": dict(Counter(w.cause for w in behind).most_common()),
        "dsp_load": sum(w.dsp_busy_ms for w in windows) / (audio * 1e3)
        if audio
        else 0.0,
        "dsp_load_p50": percentile(loads, 0.50),
        "dsp_load_p99": percentile(loads, 0.99),
        "dsp_load_max": max(loads, default=0.0),
        "dsp_over_windows": sum(load > 1.0 for load in loads),
        "periods_over_budget": sum(w.over for w in windows),
        "worst_period_ms": max((w.busy_max_ms for w in windows), default=0.0),
        "worst_gap_ms": max((w.gap_max_ms for w in windows), default=0.0),
        "queue_min_ms": min(queues) if queues else None,
        "underruns": underruns[-1] - underruns[0] if underruns else None,
    }


def print_report(path: Path, s: dict, windows: list[Window], top: int) -> None:
    print(f"spikes: {path}")
    print(
        f"  audio {s['audio_s']:.2f} s in {s['wall_s']:.2f} s wall ({s['speed']:.2f}x); "
        f"{s['windows']} windows, {s['behind']} behind real time, {s['paced']} paced"
    )
    if s["causes"]:
        causes = ", ".join(f"{k} {v}" for k, v in s["causes"].items())
        print(f"  causes when behind: {causes}")
    print(
        f"  DSP load {s['dsp_load']:.3f} per audio s (windows p50 {s['dsp_load_p50']:.3f}, "
        f"p99 {s['dsp_load_p99']:.3f}, max {s['dsp_load_max']:.3f}; "
        f"{s['dsp_over_windows']} over 1.0)"
    )
    print(
        f"  periods over {PERIOD_MS:.3f} ms: {s['periods_over_budget']} "
        f"(worst {s['worst_period_ms']:.2f} ms); "
        f"worst gap between steps {s['worst_gap_ms']:.2f} ms"
    )
    if s["queue_min_ms"] is None:
        print("  no audio queue (no playback)")
    else:
        print(f"  queue min {s['queue_min_ms']:.0f} ms; underruns +{s['underruns']}")
    print("  slowest windows (paced ones left out):")
    print(
        "      t_s  speed  wall_ms  dsp_busy  dsp_wait  compile  cf_work  ui_gap"
        "  max_period  queue_ms  cause"
    )
    unpaced = [w for w in windows if w.cause != "paced"]
    for w in sorted(unpaced, key=lambda w: w.speed)[:top]:
        q = "-" if w.queue_ms is None else f"{w.queue_ms:.0f}"
        print(
            f"  {w.t_s:7.2f}  {w.speed:5.2f}  {w.wall_ms:7.1f}  {w.dsp_busy_ms:8.1f}"
            f"  {w.dsp_wait_ms:8.1f}  {w.compile_ms:7.1f}  {w.cf_work_ms:7.1f}"
            f"  {w.ui_gap_ms:6.1f}  {w.busy_max_ms:10.2f}  {q:>8}  {w.cause}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find real-time spikes in a DIGI_SPIKE_LOG directory."
    )
    ap.add_argument("dir", type=Path, help="the DIGI_SPIKE_LOG directory")
    ap.add_argument("--top", type=int, default=20, help="windows to list (default 20)")
    ap.add_argument(
        "--json", type=Path, help="write the summary and every window as JSON"
    )
    args = ap.parse_args(argv)
    dsp = read_jsonl(args.dir / "dsp.jsonl")
    if len(dsp) < 2:
        ap.error(f"fewer than two records in {args.dir / 'dsp.jsonl'}")
    # The first window includes the worker's start-up.
    windows = join(dsp[1:], read_jsonl(args.dir / "cf.jsonl"))
    summary = summarise(windows)
    print_report(args.dir, summary, windows, args.top)
    if args.json:
        data = {"summary": summary, "windows": [asdict(w) for w in windows]}
        args.json.write_text(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
