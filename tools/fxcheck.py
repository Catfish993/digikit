#!/usr/bin/env python3
"""Bit-reduction and delay features of DN2 FX scenario runs, as a compact table.

    uv run python tools/fxcheck.py RUN_DIR [RUN_DIR ...] [--ref fx-dry-tap] [--no-plots]

A RUN_DIR holds report.json and the WAVs (audio-<scenario>.wav, or audio.wav
for a single scenario) of a ``coupled.py scenario --wav`` run. The dry
reference is the scenario named by --ref (default fx-dry-tap) in any of the
given run directories. Parameter values come from the report's ``peek``
(s16 big-endian, UI value = high byte). Writes fxcheck.json and, unless
--no-plots, plots-<scenario>/ (audiocheck plots) into each run directory.

Per scenario: basic checks and L-R; on the 0.1-0.3 s after the note onset
(the same window for every scenario) the distinct sample values, the
fundamental level, THD and the noise floor relative to it, and the RMS of the
difference to the dry reference; for delay runs the echo-only residual
(scenario minus dry): echo onsets, spacing and level ratios.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np

import audiocheck as ac

F0 = 261.6  # Trig 1 audition pitch, Hz
WIN = (0.1, 0.3)  # analysis window after the note onset, s


def ui(hexval: str | None) -> int | None:
    """UI value (high byte) of a peeked big-endian s16."""
    if not hexval or len(hexval) < 4:
        return None
    return int(hexval[:2], 16)


def onset(x: ac.F, rate: int) -> float:
    o = ac.onsets(x, rate)
    return o[0] if o else 0.0


def window(x: ac.F, rate: int, t0: float) -> ac.F:
    return x[int((t0 + WIN[0]) * rate) : int((t0 + WIN[1]) * rate)]


def tone_fit(w: ac.F, rate: int) -> dict[str, float]:
    """Least-squares fit of harmonics 1..10 of f0, each with a cubic amplitude
    envelope (the note decays). Returns f0, the fundamental level (dBFS, peak
    sine), THD (dB re fundamental) and the residual (dB re fundamental): what
    is not a slowly modulated harmonic, i.e. noise, quantisation, aliasing."""
    m = ac._mono(w)
    t = np.arange(len(m)) / rate
    u = np.linspace(-1, 1, len(m))

    def fit(f0: float, kmax: int) -> tuple[ac.F, ac.F, ac.F]:
        cols = []
        for k in range(1, kmax + 1):
            for j in range(4):
                cols.append(np.cos(2 * np.pi * k * f0 * t) * u**j)
                cols.append(np.sin(2 * np.pi * k * f0 * t) * u**j)
        a = np.stack(cols, axis=1)
        c, *_ = np.linalg.lstsq(a, m, rcond=None)
        return a, c, m - a @ c

    best = min(
        np.arange(F0 - 1.5, F0 + 1.5, 0.05),
        key=lambda f: float(np.sum(fit(float(f), 1)[2] ** 2)),
    )
    a, c, res = fit(float(best), 10)

    def power(k: int) -> float:
        cols = slice((k - 1) * 8, k * 8)
        return float(np.mean((a[:, cols] @ c[cols]) ** 2))

    p1 = power(1)
    harm = sum(power(k) for k in range(2, 11))
    db = lambda v: 10 * math.log10(max(v, 1e-30))  # noqa: E731
    return {
        "f0_hz": float(best),
        "fund_db": db(2 * p1),
        "thd_db": db(harm) - db(p1),
        "resid_db": db(float(np.mean(res**2))) - db(p1),
        "resid_rms": float(np.sqrt(np.mean(res**2))),
    }


def align(x: ac.F, ref: ac.F, rate: int, t0: float, r0: float) -> tuple[ac.F, ac.F]:
    """Mono copies of x and ref that start 20 ms before their onsets, shifted by
    the lag (+-3 ms) that best matches the first 300 ms, cut to equal length."""
    pre = int(0.02 * rate)
    a = ac._mono(x)[max(0, int(t0 * rate) - pre) :]
    b = ac._mono(ref)[max(0, int(r0 * rate) - pre) :]
    n = int(0.3 * rate)
    lags = range(-int(0.003 * rate), int(0.003 * rate) + 1)
    lag = max(
        lags,
        key=lambda s: float(np.dot(a[pre + s : pre + s + n], b[pre : pre + n])),
    )
    a = a[max(lag, 0) :]
    b = b[max(-lag, 0) :]
    k = min(len(a), len(b))
    return a[:k], b[:k]


def bit_features(
    x: ac.F, rate: int, t0: float, ref: ac.F | None, r0: float
) -> dict[str, object]:
    out: dict[str, object] = {}
    if ref is not None:
        a, b = align(x, ref, rate, t0, r0)
        s = slice(int((0.02 + WIN[0]) * rate), int((0.02 + WIN[1]) * rate))
        den = float(np.sqrt(np.mean(b[s] ** 2)))
        num = float(np.sqrt(np.mean((a[s] - b[s]) ** 2)))
        out["diff_db"] = 20 * math.log10(max(num / den, 1e-12)) if den else None
        out["diff_rms"] = num
        # Uniform quantiser error rms = step / sqrt(12); full scale is +-1.
        out["implied_bits"] = math.log2(2 / (num * math.sqrt(12))) if num else None
    w = window(x, rate, t0)
    out.update(ac.levels(w))
    out.update(tone_fit(w, rate))
    return out


def delay_features(x: ac.F, ref: ac.F, rate: int, t0: float, r0: float) -> dict:
    """Echo-only residual = x - ref after alignment. Onsets of its 5 ms envelope
    are echo starts (seconds after the note onset); the level of an echo is the
    peak envelope up to the next one."""
    a, b = align(x, ref, rate, t0, r0)
    res = (a - b)[:, None]
    t, e = ac.envelope(res, rate, 5.0)
    ons = ac.onsets(res, rate, 5.0, 6.0, 45.0, 0.1)
    bounds = [*ons[1:], float(t[-1]) + 1]
    levels_db = [
        float(e[(t >= p) & (t < q)].max()) for p, q in zip(ons, bounds, strict=False)
    ]
    return {
        "residual_peak_db": float(e.max()),
        "echo_onsets_s": [round(o - 0.02, 4) for o in ons],
        "spacing_s": [round(q - p, 4) for p, q in zip(ons, ons[1:], strict=False)],
        "echo_level_db": [round(v, 2) for v in levels_db],
        "echo_ratio_db": [
            round(q - p, 2) for p, q in zip(levels_db, levels_db[1:], strict=False)
        ],
    }


def diff_vs_dry(
    x: ac.F, ref: ac.F, rate: int, t0: float, r0: float, hold: float
) -> dict[str, object]:
    """Generic difference to the dry reference, split at note-off (hold seconds
    after the onset): peak 5 ms RMS level (dBFS) of x - ref while the note is
    held and after it, the dry level in the same spans, and the number of
    onsets in the tail difference. Below -90 dB counts as no difference."""
    a, b = align(x, ref, rate, t0, r0)
    pre = int(0.02 * rate)
    cut = pre + int(hold * rate)
    d = (a - b)[:, None]
    out: dict[str, object] = {"hold_s": hold}
    for key, sl in (("note", slice(pre, cut)), ("tail", slice(cut, None))):
        if len(d[sl]) < int(0.01 * rate):
            continue
        _, e = ac.envelope(d[sl], rate, 5.0)
        _, er = ac.envelope(b[sl][:, None], rate, 5.0)
        out[f"{key}_diff_db"] = float(e.max())
        out[f"{key}_dry_db"] = float(er.max())
    tail = d[cut:]
    out["tail_onsets"] = (
        len(ac.onsets(tail, rate, 5.0, 6.0, 45.0, 0.1)) if len(tail) > 100 else 0
    )
    tdb = out.get("tail_diff_db")
    out["tail_echo"] = bool(isinstance(tdb, float) and tdb > -90)
    return out


def scenario_events() -> dict[str, list[dict]]:
    """Events of every scenario in scenarios/dn2-fx.json, by name."""
    f = pathlib.Path(__file__).resolve().parent.parent / "scenarios" / "dn2-fx.json"
    return {s["name"]: s["events"] for s in json.loads(f.read_text())["scenarios"]}


def hold_seconds(events: list[dict]) -> float | None:
    """Duration of the Trig 1 (button 25) press in a scenario's events."""
    down = None
    for e in events:
        if e.get("button") == 25:
            if e.get("down"):
                down = e["at_instr"]
            elif down is not None:
                return (e["at_instr"] - down) / 132e6
    return None


def load_run(d: pathlib.Path) -> dict[str, tuple[dict, pathlib.Path]]:
    report = json.loads((d / "report.json").read_text())
    out = {}
    for sc in report["scenarios"]:
        wav = d / f"audio-{sc['name']}.wav"
        out[sc["name"]] = (sc, wav if wav.exists() else d / "audio.wav")
    return out


def check(
    runs: list[pathlib.Path], ref_name: str, plots: bool
) -> dict[str, dict[str, object]]:
    loaded = {}
    for d in runs:
        for name, (sc, wav) in load_run(d).items():
            rate, x = ac.load(wav)
            loaded[name] = (d, sc, rate, x)
    ref = loaded.get(ref_name)
    res: dict[str, dict[str, object]] = {}
    evs = scenario_events()
    for name, (d, sc, rate, x) in loaded.items():
        t0 = onset(x, rate)
        pk = sc.get("peek") or {}
        row: dict[str, object] = {
            "run_dir": str(d),
            "peek_ui": {k: ui(v) for k, v in pk.items()},
            "analysis": ac.analyse(x, rate),
            "onset_s": t0,
        }
        rx = ref[3] if ref and name != ref_name else None
        r0 = onset(rx, rate) if rx is not None else 0.0
        row["bits"] = bit_features(x, rate, t0, rx, r0)
        if rx is not None and (pk.get("dsend") or "0000") != "0000":
            row["delay"] = delay_features(x, rx, rate, t0, r0)
        hold = hold_seconds(evs.get(name, []))
        if rx is not None and hold:
            row["vs_dry"] = diff_vs_dry(x, rx, rate, t0, r0, hold)
        res[name] = row
        if plots:
            ac.plots(x, rate, _mkdir(d / f"plots-{name}"))
    for d in runs:
        mine = {k: v for k, v in res.items() if v["run_dir"] == str(d)}
        (d / "fxcheck.json").write_text(json.dumps(mine, indent=2) + "\n")
    return res


def _mkdir(p: pathlib.Path) -> pathlib.Path:
    p.mkdir(exist_ok=True)
    return p


def table(res: dict[str, dict[str, object]]) -> str:
    cols = (
        "scenario  br dsend peak   rms    dc     L-R(dB) levels  "
        "fund    thd  resid  diff  bits"
    )
    lines = [cols]
    for name, r in res.items():
        a: dict = r["analysis"]  # type: ignore[assignment]
        b: dict = a["basic"]
        p: dict = r["peek_ui"]  # type: ignore[assignment]
        k: dict = r["bits"]  # type: ignore[assignment]
        lr = a["lr_diff"]["rel_db"]
        diff = k.get("diff_db")
        lines.append(
            f"{name:<12}{p.get('br')!s:>3}{p.get('dsend')!s:>6} "
            f"{b['peak']:.3f} {b['rms']:.4f} {max(map(abs, b['dc'])):.0e} "
            f"{'identical' if lr is None else f'{lr:7.1f}':>9} "
            f"{k['n_levels']:>6.0f} "
            f"{k['fund_db']:6.1f} {k['thd_db']:6.1f} {k['resid_db']:6.1f} "
            f"{'-' if diff is None else f'{diff:5.1f}'} "
            f"{'-' if k.get('implied_bits') is None else f'{k["implied_bits"]:4.1f}'}"
            f"{' NONFINITE' if b['nonfinite'] else ''}{' GROWTH' if b['growth'] else ''}"
        )
    for name, r in res.items():
        d = r.get("delay")
        if d:
            p = r["peek_ui"]  # type: ignore[assignment]
            lines.append(
                f"{name}: time {p.get('dtime')} fb {p.get('dfb')} mix {p.get('dmix')} "
                f"echoes at {d['echo_onsets_s']} s, spacing {d['spacing_s']}, "  # type: ignore[index]
                f"levels {d['echo_level_db']} dB, ratios {d['echo_ratio_db']} dB"  # type: ignore[index]
            )
    for name, r in res.items():
        v = r.get("vs_dry")
        if v:
            lines.append(
                f"{name}: vs dry note {v.get('note_diff_db', float('nan')):.1f} dB "  # type: ignore[attr-defined]
                f"(dry {v.get('note_dry_db', float('nan')):.1f}), tail "  # type: ignore[attr-defined]
                f"{v.get('tail_diff_db', float('nan')):.1f} dB "  # type: ignore[attr-defined]
                f"(dry {v.get('tail_dry_db', float('nan')):.1f}), tail onsets "  # type: ignore[attr-defined]
                f"{v['tail_onsets']}, echo {'YES' if v['tail_echo'] else 'no'}"  # type: ignore[index]
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+", type=pathlib.Path)
    ap.add_argument("--ref", default="fx-dry-tap")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)
    print(table(check(a.runs, a.ref, not a.no_plots)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
