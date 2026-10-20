#!/usr/bin/env python3
"""patchkit Phase 4.2 proof: a registered machine type renders through the per-voice hook (DT2 1.17).

    uv run python -m tools.patchkit.pk_proof_voice [--patched OUT.syx] [--ready DIR] [--out DIR] [--only basic,level,reverb,filter]
        [--quick] [--jobs N]

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

Parts (independent, in parallel processes with `--jobs`, tools/patchkit/pk_proofkit.py): basic = type0 (stock, patched-type0),
sine60, sine72, tune (tune+1); level (lev-half, lev-full, stock, stock-lev-half); reverb (rev-dry, rev-send); filter
(flt-open, flt-closed). `--quick` runs type0, sine60, level and filter; the full tier runs every case.
The patched image is built from core/dsp-voice and test/sine-voice unless
`--patched` names one. Output stays under out/ (derived from firmware).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.patchkit import hook_ab, pluck_ref  # noqa: E402
from tools.patchkit import pk_proofkit as K  # noqa: E402

PATCH_MODS = ["core/dsp-voice", "test/sine-voice"]

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


# digikit/pluck is a real machine now (core/machines gives it its type, core/params its knobs): its proof is
# tools/patchkit/pk_proof_pluck.py. `analyse_pluck` stays here for it.
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


class VoiceProof(K.Proof):
    tool = "voice"
    device = "dt2"
    fw = "1.17"
    patched: pathlib.Path | None = (
        None  # --patched: an image to load instead of building PATCH_MODS
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.report: dict = {}
        self.pcm: dict[str, np.ndarray] = {}

    def render(self, name: str) -> dict:
        """One case from `restart` on its own daemon (the poke is part of the daemon's environment)."""
        if name in self.report:
            return self.report[name]
        use_patch, poke, events = CASES[name]
        wav = self.out / f"{name}.wav"
        env = {"DSP_FRAME_POKE": poke} if poke else None
        busy = None
        mods = PATCH_MODS if use_patch else []
        image = self.patched if use_patch else None
        with self.session(
            mods,
            f"case-{'p' if use_patch else 's'}-{poke or 'none'}",
            env=env,
            image=image,
        ) as s:
            emu = s.emu
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
                s.timings["emu_ms"] = s.timings.get("emu_ms", 0.0) + RUN_END / 132_000
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
                s.timings["emu_ms"] = s.timings.get("emu_ms", 0.0) + (
                    60_000_000 / 132_000 + 1800 / 1.5
                )
            emu.call("pcm.tap", action="capture_stop")
            st = emu.status()
        info = {
            "dsp_halted": (st.get("audio") or {}).get("halted"),
            "frames": st["dsp_frames"],
            "busy_per_block": busy,
        }
        self.pcm[name] = hook_ab.read_wav(wav)
        info.update(analyse(self.pcm[name]))
        if name in PLUCK_CASES and not info["silent"]:
            info.update(analyse_pluck(self.pcm[name], PLUCK_CASES[name], info["start"]))
        self.report[name] = info
        print(name, json.dumps(info), flush=True)
        return info

    def case_ok(self, name: str) -> bool:
        """The per-case verdict of the original tool: no DSP halt, plus the case's own condition."""
        r = self.render(name)
        ok = r.get("dsp_halted") is None
        detail = ""
        if name == "patched-type0":
            self.render("stock")
            n = min(len(self.pcm["stock"]), len(self.pcm[name]))
            same = bool(np.array_equal(self.pcm["stock"][:n], self.pcm[name][:n]))
            r["equal_to_stock"] = same
            ok &= same and not r["silent"]
            detail = f"equal to stock: {same}"
        want = {
            "sine60": semitone_hz(60),
            "sine72": semitone_hz(72),
            "tune+1": semitone_hz(61),
        }.get(name)
        if want is not None:
            cents = 1200 * math.log2(r["hz"] / want) if not r["silent"] else None
            r["cents_error"] = cents
            ok &= cents is not None and abs(cents) < 5
            detail = f"{r.get('hz', 0):.2f} Hz, {cents if cents is None else round(cents, 2)} cents"
        return self.check(f"case {name}", bool(ok), detail)

    def finish(self) -> None:
        (self.out / "report.json").write_text(
            json.dumps(self.report, indent=1, default=float)
        )

    def part_cases(self, names: tuple[str, ...]) -> None:
        for name in names:
            self.case_ok(name)
        self.finish()

    def part_level(self) -> None:
        for name in ("lev-half", "lev-full", "stock", "stock-lev-half"):
            self.render(name)
        for name in ("lev-half", "lev-full", "stock", "stock-lev-half"):
            self.case_ok(name)
        r = self.report
        sine_db = 20 * math.log10(r["lev-half"]["rms"] / r["lev-full"]["rms"])
        stock_db = 20 * math.log10(r["stock-lev-half"]["rms"] / r["stock"]["rms"])
        self.check(
            "level stage: the sine and the stock sample drop by the same dB",
            abs(sine_db - stock_db) < 0.3,
            f"sine {sine_db:.2f} dB, stock {stock_db:.2f} dB",
        )
        self.finish()

    def part_reverb(self) -> None:
        for name in ("rev-dry", "rev-send"):
            self.case_ok(name)
        tails = {k: tail_energy(self.pcm[k]) for k in ("rev-dry", "rev-send")}
        self.check(
            "reverb send adds a tail",
            tails["rev-send"] > 30 * max(tails["rev-dry"], 1e-12),
            str(tails),
        )
        self.finish()

    def part_filter(self) -> None:
        for name in ("flt-open", "flt-closed"):
            self.case_ok(name)
        drop = 20 * math.log10(
            self.report["flt-closed"]["rms"] / self.report["flt-open"]["rms"]
        )
        self.check("the filter closes on the sine", drop < -6, f"{drop:.1f} dB")
        self.finish()


P = VoiceProof
PARTS = [
    K.Part(
        "type0",
        "stock, and the patched image with no type poked equals it",
        lambda p: p.part_cases(("stock", "patched-type0")),
        True,
        60,
        group="basic",
    ),
    K.Part(
        "sine60",
        "the sine voice at note 60",
        lambda p: p.part_cases(("sine60",)),
        True,
        40,
        group="basic",
    ),
    K.Part(
        "sine72",
        "the sine voice at note 72",
        lambda p: p.part_cases(("sine72",)),
        False,
        40,
        group="basic",
    ),
    K.Part(
        "tune",
        "the sine voice tuned +1 semitone",
        lambda p: p.part_cases(("tune+1",)),
        False,
        40,
        group="basic",
    ),
    K.Part("level", "the level stage sees the voice", P.part_level, True, 90),
    K.Part("reverb", "the reverb send", P.part_reverb, False, 60),
    K.Part("filter", "the filter", P.part_filter, True, 60),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--patched",
        type=pathlib.Path,
        help="an image to load instead of the patch of PATCH_MODS",
    )
    ap.add_argument(
        "--ready", type=pathlib.Path, help="ready inputs (DIGI_PK_READY_DT2_117)"
    )
    K.add_args(ap, PARTS)
    a = ap.parse_args()
    a.out = a.out or ROOT / "out" / "runs" / "voice-proof"
    if a.ready:
        os.environ["DIGI_PK_READY_DT2_117"] = str(a.ready.resolve())
    VoiceProof.patched = a.patched.resolve() if a.patched else None
    return K.run_tool(__file__, VoiceProof, PARTS, a)


if __name__ == "__main__":
    sys.exit(main())
