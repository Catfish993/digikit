#!/usr/bin/env python3
"""patchkit Phase 4.2 proof: a registered machine type renders through the per-voice hook (DT2 1.17).

    uv run python tools/pk_proof_voice.py [--patched OUT.syx] [--ready DIR] [--out DIR] [--case NAME ...]

Each case starts a coupled daemon on the stock firmware and the one-sample card, optionally loads the patched image
(`firmware.load`, hot DSP), forces frame words with DSP_FRAME_POKE (core.machines does not exist yet, so the type is
forced on the wire: word 0x94 is the machine type of track 0) and renders TRIG 1 held for 1 s with PCM capture:

  stock             the stock firmware (reference)
  patched-type0     patched image, no poke: must be bit-identical to stock (a stock machine is untouched)
  sine60            type 8 forced, note 60: a sine at 261.63 Hz
  sine72            type 8, pitch word 0x4800 (note 72): 523.25 Hz
  tune+1            type 8, tune word 0x4100 (+1 semitone): 277.18 Hz
  lev-half          type 8, the machine level word 0xec halved (0x6400 -> 0x3200): -6 dB if the engine's later
                    stages see the voice (amp / level stage)
  stock-lev-half    the same on the stock sample (reference for the level stage)

The tool prints one JSON report and exits 1 unless every case passes. Output stays under out/ (derived from firmware).
Needs DIGI_EMUD (the digi-emud binary built with SHARC_GEN_DIR=out/native/dt2-audio/gen).
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import tempfile

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import card_check
    import hook_ab
    import pluck_ref
    from digi_client import Emu
finally:
    sys.path.pop(0)

TRIG1 = 25
NOTE_HZ = {60: 261.6256, 72: 523.2511}


def semitone_hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


# ColdFire-instruction timed panel scripts (DT2): the boot modal is closed with NO (button 12), then a page button
NO, FX_PAGE, FLT_PAGE, ENC_REV, ENC_FLT = 12, 5, 3, 6, 5
NOTE_DOWN, NOTE_UP, RUN_END = 150_000_000, 200_000_000, 560_000_000


def panel(page: int, encoder: int, events: int, detents: int) -> list[dict]:
    ev = [
        {"at_instr": 0, "button": NO, "down": True},
        {"at_instr": 1_000_000, "button": NO, "down": False},
        {"at_instr": 4_000_000, "button": page, "down": True},
        {"at_instr": 6_000_000, "button": page, "down": False},
    ]
    ev += [
        {"at_instr": 14_000_000 + i * 1_000_000, "turn": encoder, "detents": detents}
        for i in range(events)
    ]
    return ev


# machines/pluck is a real machine now (core/machines gives it its type, core/params its knobs): its proof is
# tools/pk_proof_pluck.py. `analyse_pluck` stays here for it.
PLUCK_CASES: dict[str, int] = {}

# name -> (patched?, DSP_FRAME_POKE, panel events)
CASES: dict[str, tuple[bool, str, list[dict]]] = {
    "stock": (False, "", []),
    "patched-type0": (True, "", []),
    "sine60": (True, "0x94=8", []),
    "sine72": (True, "0x94=8,0x02=0x4800", []),
    "tune+1": (True, "0x94=8,0xda=0x4100", []),
    "lev-half": (True, "0x94=8,0xec=0x3200", []),
    "lev-full": (True, "0x94=8", []),
    "stock-lev-half": (False, "0xec=0x3200", []),
    "rev-dry": (True, "0x94=8", panel(FX_PAGE, ENC_REV, 0, 0)),
    "rev-send": (True, "0x94=8", panel(FX_PAGE, ENC_REV, 52, 4)),
    "flt-open": (True, "0x94=8,0x02=0x6000", panel(FLT_PAGE, ENC_FLT, 0, 0)),
    "flt-closed": (True, "0x94=8,0x02=0x6000", panel(FLT_PAGE, ENC_FLT, 46, -4)),
}


def peak_hz(x: np.ndarray, fs: float = 48000.0) -> float:
    """Frequency of the strongest partial, refined by parabolic interpolation on a zero-padded Hann spectrum."""
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n), 8 * n))
    k = int(np.argmax(spec))
    if 0 < k < len(spec) - 1:
        a, b, c = np.log(spec[k - 1 : k + 2] + 1e-30)
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return float(k * fs / (8 * n))


def tail_energy(pcm: np.ndarray) -> float:
    """Mean square of the left channel from 0.1 s after the sine voice ends (2 s after the note starts) to the end."""
    left = pcm[0::2].astype(np.float64)
    start = int(left_index_of(NOTE_DOWN) + 96000 + 0.1 * 48000)
    return float(np.mean(left[start:] ** 2)) if start < len(left) else 0.0


def left_index_of(instr: int) -> float:
    """Audio sample of a ColdFire instruction count (about 132 M instructions per audio second)."""
    return instr / 132_000_000 * 48000


def analyse_pluck(pcm: np.ndarray, note: int, start: int) -> dict:
    """Fundamental by bandpass and zero-crossing fit; decay as the slope of the 2048-sample RMS in dB."""
    left = pcm[0::2].astype(np.float64)
    want = semitone_hz(note)
    period = 48000.0 / want
    seg = left[start + int(4 * period) : start + 36000]
    got = pluck_ref.f0_from_crossings(seg, want)
    blocks = [
        float(np.sqrt(np.mean(left[start + k : start + k + 2048] ** 2)))
        for k in range(2048, 40000, 2048)
    ]
    t = (np.arange(len(blocks)) * 2048 + 2048 + 1024) / 48000.0
    slope = float(np.polyfit(t, 20 * np.log10(np.maximum(blocks, 1e-12)), 1)[0])
    return {
        "f0": got,
        "cents": 1200 * math.log2(got / want),
        "decay_db_per_s": slope,
        "t60_s": -60.0 / slope if slope < 0 else None,
    }


def analyse(pcm: np.ndarray) -> dict:
    left = pcm[0::2].astype(np.float64)
    act = np.flatnonzero(np.abs(left) > 1e-6)
    if len(act) < 4000:
        return {"active_samples": int(len(act)), "silent": True, "rms": 0.0}
    lo, hi = int(act[0]), int(act[-1])
    seg = left[lo : lo + min(hi - lo, 36000)][6000:]  # past the attack, 0.75 s at most
    return {
        "active_samples": int(len(act)),
        "silent": False,
        "start": lo,
        "rms": float(np.sqrt(np.mean(seg**2))),
        "peak": float(np.max(np.abs(seg))),
        "hz": peak_hz(seg),
    }


def render(
    syx: pathlib.Path,
    ready: pathlib.Path,
    patched: pathlib.Path | None,
    poke: str,
    events: list[dict],
    wav: pathlib.Path,
    work: pathlib.Path,
) -> dict:
    env = {"DSP_FRAME_POKE": poke} if poke else {}
    busy = None
    with Emu.spawn_coupled(syx, ready, timeout=300, env=env) as emu:
        samples = work / "samples"
        card_check.make_samples(samples)
        emu.call("card.load", dir=str(samples))
        if patched is not None:
            emu.call("firmware.load", syx=str(patched), auto_no=True)
        if events:
            emu.restart(auto_no=True)
            emu.call("pcm.tap", action="capture", path=str(wav))
            emu.schedule(
                [
                    *events,
                    {"at_instr": NOTE_DOWN, "button": TRIG1, "down": True},
                    {"at_instr": NOTE_UP, "button": TRIG1, "down": False},
                ]
            )
            emu.step(RUN_END)
        else:
            emu.restart(auto_no=True)
            emu.step(20_000_000)
            emu.call("pcm.tap", action="capture", path=str(wav))
            emu.call("input.button", code=TRIG1, down=True)
            emu.step(40_000_000)
            t0 = (emu.status().get("audio") or {}).get("dsp_tiers") or {}
            frames = emu.status()["dsp_frames"]
            emu.call("run_until", frames=frames + 1500)
            t1 = (emu.status().get("audio") or {}).get("dsp_tiers") or {}
            if t0 and t1:
                busy = (
                    (t1["instructions"] - t1["idle_skip"])
                    - (t0["instructions"] - t0["idle_skip"])
                ) / 1500
            emu.call("input.button", code=TRIG1, down=False)
            frames = emu.status()["dsp_frames"]
            emu.call("run_until", frames=frames + 300)
        emu.call("pcm.tap", action="capture_stop")
        st = emu.status()
    return {
        "dsp_halted": (st.get("audio") or {}).get("halted"),
        "frames": st["dsp_frames"],
        "busy_per_block": busy,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stock", default=str(ROOT / "Digitakt_II_OS1.17.syx"))
    ap.add_argument("--patched", default=str(ROOT / "out" / "patch" / "vp.syx"))
    ap.add_argument("--ready", default=str(ROOT / "out" / "ready-fw-dt2"))
    ap.add_argument("--out", default=str(ROOT / "out" / "runs" / "voice-proof"))
    ap.add_argument("--case", action="append", choices=list(CASES))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="pk-voice-"))
    stock, patched, ready = (
        pathlib.Path(a.stock).resolve(),
        pathlib.Path(a.patched).resolve(),
        pathlib.Path(a.ready).resolve(),
    )
    names = a.case or list(CASES)
    report: dict = {"patched": patched.name}
    pcm: dict[str, np.ndarray] = {}
    for name in names:
        use_patch, poke, events = CASES[name]
        wav = out / f"{name}.wav"
        info = render(
            stock, ready, patched if use_patch else None, poke, events, wav, work
        )
        pcm[name] = hook_ab.read_wav(wav)
        info.update(analyse(pcm[name]))
        if name in PLUCK_CASES and not info["silent"]:
            info.update(analyse_pluck(pcm[name], PLUCK_CASES[name], info["start"]))
        report[name] = info
        print(name, json.dumps(info), flush=True)
    ok = True
    checks: dict[str, bool] = {}
    for name in names:
        r = report[name]
        ok_run = r.get("dsp_halted") is None
        if name == "patched-type0" and "stock" in pcm:
            n = min(len(pcm["stock"]), len(pcm[name]))
            same = bool(np.array_equal(pcm["stock"][:n], pcm[name][:n]))
            r["equal_to_stock"] = same
            ok_run &= same and not r["silent"]
        want = {
            "sine60": semitone_hz(60),
            "sine72": semitone_hz(72),
            "tune+1": semitone_hz(61),
        }.get(name)
        if want is not None:
            cents = 1200 * math.log2(r["hz"] / want) if not r["silent"] else None
            r["cents_error"] = cents
            ok_run &= cents is not None and abs(cents) < 5
        if name in PLUCK_CASES:
            ok_run &= not r["silent"] and abs(r["cents"]) < 2.0
            if name == "pluck60":
                ok_run &= r["t60_s"] is not None and abs(r["t60_s"] / 1.02 - 1) < 0.15
        checks[name] = bool(ok_run)
        ok &= ok_run
    if all(k in report for k in ("lev-half", "lev-full", "stock", "stock-lev-half")):
        sine_db = 20 * math.log10(report["lev-half"]["rms"] / report["lev-full"]["rms"])
        stock_db = 20 * math.log10(
            report["stock-lev-half"]["rms"] / report["stock"]["rms"]
        )
        report["level_stage_db"] = {"sine": sine_db, "stock": stock_db}
        good = abs(sine_db - stock_db) < 0.3
        checks["level-stage"] = good
        ok &= good
    if "rev-dry" in report and "rev-send" in report:
        tails = {k: tail_energy(pcm[k]) for k in ("rev-dry", "rev-send")}
        report["reverb_tail"] = tails
        good = tails["rev-send"] > 30 * max(tails["rev-dry"], 1e-12)
        checks["reverb-send"] = good
        ok &= good
    if "flt-open" in report and "flt-closed" in report:
        drop = 20 * math.log10(report["flt-closed"]["rms"] / report["flt-open"]["rms"])
        report["filter_drop_db"] = drop
        good = drop < -6
        checks["filter"] = good
        ok &= good
    report["checks"] = checks
    report["pass"] = bool(ok)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps({"checks": checks, "pass": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
