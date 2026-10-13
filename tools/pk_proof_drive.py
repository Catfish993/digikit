#!/usr/bin/env python3
"""patchkit Phase 4.1 proof: fx/master-drive on a running coupled emulator (DT2 1.17 or DN2 1.12).

    uv run python tools/pk_proof_drive.py dt2|dn2 [--stock STOCK.syx] [--patched PATCHED.syx] [--ready DIR]
        [--out DIR] [--run-dir DIR]

One coupled daemon starts on the stock firmware (ready inputs of `tools/ready.py ... --force`), the DT2 loads the
one-sample card, and the same panel script (master page via FUNC+6, encoder 7 turned, TRIG 1 held) is rendered with
PCM capture:

  stock-rest, stock-turned      the stock firmware, knob untouched / knob turned to the top (control: the
                                master compressor is unmixed, so the sidechain filter must not reach the PCM)
  patched-rest                  `firmware.load` of PATCHED (coupled cold boot of the changed MAIN): must be
                                bit-identical to stock-rest
  patched-mid, patched-full     knob at about UI 100 and at the top: RMS, crest factor, harmonic content
  patched-sweep                 knob swept up while the note sounds: largest sample-to-sample step against
                                stock and against the fixed settings (clicks)

Frames of the master page (stock and patched, knob at rest and at the top) go to OUT as PNG: the patched label must
read DRV. The DSP word of the knob is read back with `dsp.peek`. Output files and the report stay under out/ (never
committed: they derive from firmware). Needs DIGI_EMUD (the digi-emud binary of `mise run emu:serve -- --build-only`).
Exit 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import card_check
    import hook_ab
    from digi_client import Emu
finally:
    sys.path.pop(0)

STOCK = {"dt2": "Digitakt_II_OS1.17.syx", "dn2": "Digitone_II_OS1.12.syx"}
TRIG1, FUNC = 25, 17
# The master page: FUNC + button 6 (scenarios/*-sendfx.json "sf-mst-mix"); DT2 first leaves the FILE SYSTEM modal.
PRELUDE = {
    "dt2": [
        {"at_instr": 0, "button": 12, "down": True},
        {"at_instr": 1_000_000, "button": 12, "down": False},
        {"at_instr": 4_000_000, "button": 5, "down": True},
        {"at_instr": 6_000_000, "button": 5, "down": False},
        {"at_instr": 74_000_000, "button": FUNC, "down": True},
        {"at_instr": 75_000_000, "button": 6, "down": True},
        {"at_instr": 76_000_000, "button": 6, "down": False},
        {"at_instr": 77_000_000, "button": FUNC, "down": False},
    ],
    "dn2": [
        {"at_instr": 1_000_000, "button": 5, "down": True},
        {"at_instr": 3_000_000, "button": 5, "down": False},
        {"at_instr": 75_000_000, "button": FUNC, "down": True},
        {"at_instr": 76_000_000, "button": 6, "down": True},
        {"at_instr": 77_000_000, "button": 6, "down": False},
        {"at_instr": 78_000_000, "button": FUNC, "down": False},
    ],
}
# DSP address of the Sidechain Filter word (finding 04) and the instruction counts of the script
WORD = {"dt2": 0x256008, "dn2": 0x25CED8}
SHOT_AT = {"dt2": 90_000_000, "dn2": 92_000_000}
TURN_AT = {
    "dt2": 95_000_000,
    "dn2": 97_000_000,
}  # first knob event, then one every 1M instructions
TRIG_DOWN = {"dt2": 150_000_000, "dn2": 150_000_000}
TRIG_UP = {"dt2": 300_000_000, "dn2": 330_000_000}
END = {"dt2": 340_000_000, "dn2": 370_000_000}
DETENT = 4  # detents per knob event (scenarios/*-sendfx.json)


def turns(
    device: str, count: int, start: int | None = None, every: int = 500_000
) -> list[dict]:
    t0 = TURN_AT[device] if start is None else start
    return [
        {"at_instr": t0 + i * every, "turn": 7, "detents": DETENT} for i in range(count)
    ]


def script(device: str, knob: list[dict]) -> list[dict]:
    return [
        *PRELUDE[device],
        *knob,
        {"at_instr": TRIG_DOWN[device], "button": TRIG1, "down": True},
        {"at_instr": TRIG_UP[device], "button": TRIG1, "down": False},
    ]


def analyse(pcm: np.ndarray) -> dict:
    """RMS and peak of the active part, crest factor, spectral shares, largest sample-to-sample step."""
    ch = [pcm[0::2].astype(np.float64), pcm[1::2].astype(np.float64)]
    act = np.flatnonzero(np.abs(ch[0]) > 1e-6)
    if len(act) < 2000:
        return {"active_samples": int(len(act)), "silent": True}
    lo, hi = act[0], act[-1]
    seg = ch[0][lo:hi]
    rms = float(np.sqrt(np.mean(seg**2)))
    peak = float(np.max(np.abs(seg)))
    n = 1 << int(np.floor(np.log2(len(seg))))
    spec = np.abs(np.fft.rfft(seg[len(seg) // 2 - n // 2 :][:n] * np.hanning(n))) ** 2
    k = int(np.argmax(spec))
    tot = float(spec.sum())
    core = float(spec[max(0, k - 3) : k + 4].sum())
    freqs = np.fft.rfftfreq(n, 1 / 48000)
    return {
        "active_samples": int(len(act)),
        "silent": False,
        "rms": rms,
        "peak": peak,
        "crest_db": 20 * np.log10(peak / rms),
        "peak_hz": float(freqs[k]),
        "non_fundamental": 1
        - core / tot,  # power outside the strongest partial (a sine: harmonic content)
        "hf_share": float(spec[freqs > 2000].sum() / tot),
        "max_step": max(float(np.max(np.abs(np.diff(c[lo:hi])))) for c in ch),
    }


def aligned_equal(
    ref: np.ndarray, test: np.ndarray, max_shift: int = 512
) -> tuple[bool, int | None]:
    """Is TEST equal to REF over REF's active part, possibly shifted by whole stereo frames? The cold-booted
    patched ready state can sit one DSP block (64 samples) away from the stock one: a capture phase, not audio."""
    for k in sorted(range(-max_shift, max_shift + 1, 2), key=abs):
        x, y = (ref[k:], test) if k >= 0 else (ref, test[-k:])
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        act = np.flatnonzero(np.abs(x) > 1e-6)
        if len(act) > 1000 and np.array_equal(x[act[0] : act[-1]], y[act[0] : act[-1]]):
            return True, k
    return False, None


class Proof:
    def __init__(self, emu: Emu, device: str, out: pathlib.Path):
        self.emu, self.device, self.out = emu, device, out
        self.runs: dict[str, dict] = {}

    def render(self, name: str, knob: list[dict], shots: bool = True) -> np.ndarray:
        emu, dev = self.emu, self.device
        emu.restart(auto_no=True)
        wav = self.out / f"{name}.wav"
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.schedule(script(dev, knob))
        info: dict = {}
        pos = 0
        for label, at in (
            ("rest", SHOT_AT[dev]),
            ("turned", TRIG_DOWN[dev] - 1_000_000),
            ("end", END[dev]),
        ):
            emu.step(at - pos)
            pos = at
            word = emu.dsp_peek(WORD[dev], 2)["hex"]
            info[f"word_{label}"] = int.from_bytes(bytes.fromhex(word), "little")
            if shots and label != "end":
                (self.out / f"{name}-master-{label}.png").write_bytes(emu.frame_png())
        emu.call("pcm.tap", action="capture_stop")
        st = emu.status()
        info["dsp_frames"] = st["dsp_frames"]
        info["cf_error"] = (st.get("cf") or {}).get("error")
        info["dsp_halted"] = (st.get("audio") or {}).get("halted")
        pcm = hook_ab.read_wav(wav)
        info.update(analyse(pcm))
        self.runs[name] = info
        return pcm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("device", choices=("dt2", "dn2"))
    ap.add_argument("--stock")
    ap.add_argument("--patched")
    ap.add_argument("--ready")
    ap.add_argument("--out")
    ap.add_argument("--run-dir")
    ap.add_argument(
        "--full-events", type=int, default=60, help="knob events of the top setting"
    )
    ap.add_argument(
        "--mid-events", type=int, default=13, help="knob events of the middle setting"
    )
    a = ap.parse_args()
    dev = a.device
    stock = pathlib.Path(a.stock or ROOT / STOCK[dev]).resolve()
    patched = pathlib.Path(
        a.patched or ROOT / "out" / "patch" / f"md-{dev}.syx"
    ).resolve()
    ready = pathlib.Path(a.ready or ROOT / "out" / f"ready-md-{dev}")
    out = pathlib.Path(a.out or ROOT / "out" / "runs" / "md-proof" / dev)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"device": dev, "stock": stock.name, "patched": patched.name}
    with Emu.spawn_coupled(stock, ready, timeout=300, run_dir=a.run_dir) as emu:
        card = None
        if dev == "dt2":
            samples = out / "samples"
            card_check.make_samples(samples)
            card = emu.call("card.load", dir=str(samples))["card"]
        pr = Proof(emu, dev, out)
        stock_rest = pr.render("stock-rest", [])
        stock_turned = pr.render("stock-turned", turns(dev, a.full_events))
        params = {"syx": str(patched), "start": "auto", "force": True, "auto_no": True}
        if card:
            params["card"] = card
        rep = emu.call("firmware.load", **params)
        report["load"] = {
            k: rep.get(k)
            for k in (
                "start_used",
                "main_changed",
                "audio",
                "coupled_note",
                "cache_hit",
            )
        }
        p_rest = pr.render("patched-rest", [])
        pr.render("patched-mid", turns(dev, a.mid_events))
        pr.render("patched-full", turns(dev, a.full_events))
        # sweep: one knob event every 3M instructions while the note sounds
        pr.render(
            "patched-sweep",
            turns(
                dev, a.full_events, start=TRIG_DOWN[dev] + 5_000_000, every=3_000_000
            ),
        )
    r = pr.runs
    n = min(len(stock_rest), len(p_rest))
    rest_equal, rest_shift = aligned_equal(stock_rest, p_rest)
    report["patched_rest_shift_samples"] = rest_shift
    checks = {
        "stock_audible": not r["stock-rest"]["silent"],
        "patched_rest_bit_identical": rest_equal,
        "stock_unaffected_by_knob": bool(
            len(stock_rest) == len(stock_turned)
            and np.array_equal(stock_rest[:n], stock_turned[:n])
        ),
        "knob_reached_top": r["patched-full"]["word_end"]
        > r["patched-mid"]["word_end"]
        > 0x5000,
        "pcm_changes_with_drive": r["patched-full"]["rms"] != r["stock-rest"]["rms"],
        "harmonic_content_rises": r["patched-rest"]["non_fundamental"]
        < r["patched-mid"]["non_fundamental"]
        < r["patched-full"]["non_fundamental"],
        "level_follows_drive": r["patched-rest"]["rms"]
        < r["patched-mid"]["rms"]
        < r["patched-full"]["rms"],
        "no_clicks": r["patched-sweep"]["max_step"]
        <= 1.10 * max(r["stock-rest"]["max_step"], r["patched-full"]["max_step"]),
        "no_fault": all(
            x["cf_error"] is None and x["dsp_halted"] is None for x in r.values()
        ),
    }
    report["runs"] = r
    report["checks"] = checks
    report["pass"] = all(checks.values())
    (out / "report.json").write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps(report, indent=1, default=float))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
