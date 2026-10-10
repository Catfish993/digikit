"""Find real-time spikes in a DIGI_SPIKE_LOG directory.

The DSP worker writes dsp.jsonl (one record per 0.1 audio s) and the coupled
run loop writes cf.jsonl (one record per ~20 ms of wall time), both on one
process clock. For each DSP window this reports how fast audio was produced
(audio s per wall s), where the wall time went and the audio queue, and it
names the largest cost of every window that fell behind real time:

- dsp: the DSP thread was busy rendering;
- compile: fast-tier region builds and compiles on the DSP thread;
- cf: the DSP waited for frames while the ColdFire thread was emulating;
- ui-gap: the DSP waited while no step_chunk call ran (in the app: the
  webview pump and IPC).

    DIGI_SPIKE_LOG=DIR mise run emu        # or any coupled-run
    uv run python tools/spikes.py DIR [--top 20] [--json OUT]
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
    underruns: int | None
    cause: str


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
    wall: float, wait: float, compile_ns: float, cf_work: float, gap: float
) -> str:
    """The largest cost of a window (all arguments in ns)."""
    if wait > 0.25 * wall:
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
            j += 1
        compile_ns = rec["compile_ns"] + rec["build_ns"]
        audio_s = rec["periods"] / PERIODS_PER_S
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
                queue_ms=min(queues) if queues else None,
                underruns=underruns,
                cause=cause_of(wall, rec["wait_ns"], compile_ns, work, gap),
            )
        )
    return out


def summarise(windows: list[Window]) -> dict:
    behind = [w for w in windows if w.speed < 1.0]
    queues = [w.queue_ms for w in windows if w.queue_ms is not None]
    underruns = [w.underruns for w in windows if w.underruns is not None]
    audio = sum(w.audio_s for w in windows)
    wall = sum(w.wall_ms for w in windows) / 1e3
    return {
        "windows": len(windows),
        "audio_s": audio,
        "wall_s": wall,
        "speed": audio / wall if wall else 0.0,
        "behind": len(behind),
        "causes": dict(Counter(w.cause for w in behind).most_common()),
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
        f"{s['windows']} windows, {s['behind']} behind real time"
    )
    if s["causes"]:
        causes = ", ".join(f"{k} {v}" for k, v in s["causes"].items())
        print(f"  causes when behind: {causes}")
    print(
        f"  periods over {PERIOD_MS:.3f} ms: {s['periods_over_budget']} "
        f"(worst {s['worst_period_ms']:.2f} ms); "
        f"worst gap between steps {s['worst_gap_ms']:.2f} ms"
    )
    if s["queue_min_ms"] is None:
        print("  no audio queue (no playback)")
    else:
        print(f"  queue min {s['queue_min_ms']:.0f} ms; underruns +{s['underruns']}")
    print("  slowest windows:")
    print(
        "      t_s  speed  wall_ms  dsp_busy  dsp_wait  compile  cf_work  ui_gap"
        "  max_period  queue_ms  cause"
    )
    for w in sorted(windows, key=lambda w: w.speed)[:top]:
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
