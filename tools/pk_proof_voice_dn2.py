#!/usr/bin/env python3
"""patchkit core/dsp-voice on Digitone II 1.12: test/dn2-sine renders through the FM engine's later stages.

    uv run python tools/pk_proof_voice_dn2.py [--out DIR] [--only a,b,c]

Emulator only (finding 06, "Per-voice hook on DN2"). SINE is the machine type 5 that test/dn2-sine adds (selected in the
machine menu; the track's frame words are forced with DSP_FRAME_POKE where a case needs another note):

  a  stock   with core/dsp-voice and test/dn2-sine patched in, FM TONE selected: the PCM is bit-identical to the stock
             firmware's (the hook with no registered type changes nothing)
  b  pitch   SINE: note 60 gives 261.63 Hz, note 72 (pitch word 0x4800) 523.25 Hz
  c  stages  the level knob (LEVEL encoder), the filter frequency, the overdrive and the reverb send change the sine:
             the engine's per-track processing, FX and mix see the registered voice

Output goes under OUT (default out/runs/voice-proof-dn2), never committed. Exit 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hook_ab  # noqa: E402
import pk_harness as H  # noqa: E402
from pk_proof_drive import aligned_equal, analyse  # noqa: E402

MODS = ["core/machines", "core/dsp-voice", "test/dn2-sine"]
SINE_DOWNS = 4  # FM TONE, FM DRUM, WAVETONE, SWARMER, SINE (before MIDI)
READY = ROOT / "out" / "ready-fastweb-dn2"


def peak_hz(x: np.ndarray, fs: float = 48000.0) -> float:
    """Frequency of the strongest partial, refined by parabolic interpolation on a zero-padded Hann spectrum."""
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n), 8 * n))
    k = int(np.argmax(spec))
    if 0 < k < len(spec) - 1:
        a, b, c = np.log(spec[k - 1 : k + 2] + 1e-30)
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return float(k * fs / (8 * n))


def semitone_hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


class Proof:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(
            f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}",
            flush=True,
        )
        if not ok:
            self.failed.append(name)
        return ok

    def render(
        self,
        name: str,
        mods: list[str],
        *,
        downs: int = 0,
        poke: str = "",
        page: str = "",
        turns: tuple[tuple[str, int], ...] = (),
        hold_ms: int = 1000,
        run_ms: int = 1300,
    ) -> np.ndarray:
        """One fresh daemon: select the machine, set the knobs, hold TRIG 1, capture the left channel."""
        if "DIGI_PK_READY_DN2_112" not in os.environ and READY.is_dir():
            os.environ["DIGI_PK_READY_DN2_112"] = str(READY)
        if poke:
            os.environ["DSP_FRAME_POKE"] = poke
        else:
            os.environ.pop("DSP_FRAME_POKE", None)
        s = H.Session(
            H.Cell("dn2", "1.12", list(mods)),
            H.Goldens(),
            log=None,
            run_name=f"voice-dn2-{name}",
        )
        s.open()
        with s:
            s.reset()
            s.run_ms(1500)
            if downs:
                s.do_tap(["FUNC", "SRC"])
                s.run_ms(300)
                for _ in range(downs):
                    s.do_tap("DOWN")
                    s.run_ms(80)
                s.do_tap("YES")
                s.run_ms(300)
                s.do_tap("YES")
                s.run_ms(300)
            if page:
                s.do_tap(page)
                s.run_ms(300)
            for enc, by in turns:
                for _ in range(abs(by) // 10 + 1):
                    s.do_turn(enc, int(math.copysign(min(abs(by), 10), by)))
                    s.run_ms(60)
                    by = int(math.copysign(max(abs(by) - 10, 0), by))
            wav = self.out / f"{name}.wav"
            s.emu.call("pcm.tap", action="capture", path=str(wav))
            s.emu.tap(25, hold=int(hold_ms * H.MS))
            s.run_ms(run_ms)
            s.emu.call("pcm.tap", action="capture_stop")
            s.health(f"render {name}")
        os.environ.pop("DSP_FRAME_POKE", None)
        return hook_ab.read_wav(wav)[0::2].astype(np.float64)

    def part_a(self) -> None:
        print("a  stock (hook patched in, FM TONE selected)")
        ref = self.render("stock", [])
        got = self.render("patched-fmtone", MODS)
        full = np.stack([ref, ref])  # aligned_equal takes interleaved stereo
        inter = np.empty(2 * len(ref))
        inter[0::2] = ref
        inter[1::2] = ref
        other = np.empty(2 * len(got))
        other[0::2] = got
        other[1::2] = got
        del full
        ok, shift = aligned_equal(inter, other)
        self.check(
            "FM TONE with the hook patched in equals stock",
            ok and not analyse(inter).get("silent", True),
            f"{analyse(inter).get('active_samples')} active samples, shift {shift}",
        )

    def part_b(self) -> None:
        print("b  pitch")
        for note, poke in ((60, ""), (72, "0x02=0x4800")):
            x = self.render(f"sine{note}", MODS, downs=SINE_DOWNS, poke=poke)
            act = np.flatnonzero(np.abs(x) > 1e-5)
            seg = x[act[0] + 2000 : act[0] + 26000] if len(act) > 30000 else x[:0]
            f = peak_hz(seg) if len(seg) else 0.0
            want = semitone_hz(note)
            cents = 1200 * math.log2(f / want) if f else 99.0
            self.check(
                f"SINE note {note}: {want:.2f} Hz",
                abs(cents) < 2.0,
                f"{f:.3f} Hz, {cents:+.2f} cents",
            )

    def part_c(self) -> None:
        print(
            "c  the engine's later stages see the voice (stock FM TONE with the same knobs: level -28.8 dB)"
        )
        base = self.render("c-base", MODS, downs=SINE_DOWNS, run_ms=2600)
        rms = lambda x, a=8000, b=50000: float(np.sqrt(np.mean(x[a:b] ** 2)))  # noqa: E731
        lev = self.render(
            "c-level", MODS, downs=SINE_DOWNS, turns=(("LEVEL", -300),), run_ms=2600
        )
        db = 20 * math.log10(max(rms(lev), 1e-9) / rms(base))
        self.check("the LEVEL knob lowers the sine", db < -10, f"{db:+.1f} dB")
        env = [rms(base, k, k + 4800) for k in range(0, 40000, 4800)]
        tail = rms(base, 60000, 66000) / max(rms(base, 8000, 14000), 1e-9)
        self.check(
            "the amplitude envelope releases the voice after the note (the engine's gate)",
            tail < 0.01,
            f"tail / body {tail:.4f}, rms per 100 ms {[round(e, 4) for e in env[:8]]}",
        )
        hi = "0x02=0x6000"  # note 96, 2093 Hz
        open_ = self.render("c-flt-open", MODS, downs=SINE_DOWNS, poke=hi)
        closed = self.render(
            "c-flt-closed",
            MODS,
            downs=SINE_DOWNS,
            poke=hi,
            page="FLTR",
            turns=(("ENC E", -300),),
        )
        db = 20 * math.log10(max(rms(closed), 1e-9) / rms(open_))
        self.check("the filter frequency closes on the sine", db < -6, f"{db:+.1f} dB")
        od = self.render(
            "c-overdrive", MODS, downs=SINE_DOWNS, page="FX", turns=(("ENC B", 300),)
        )
        spec = np.abs(np.fft.rfft(od[8000:40000] * np.hanning(32000)))
        spec0 = np.abs(np.fft.rfft(base[8000:40000] * np.hanning(32000)))
        k1 = int(261.63 / 48000 * 32000)
        h3 = float(spec[3 * k1 - 4 : 3 * k1 + 5].max() / spec[k1 - 4 : k1 + 5].max())
        h3b = float(spec0[3 * k1 - 4 : 3 * k1 + 5].max() / spec0[k1 - 4 : k1 + 5].max())
        self.check(
            "the overdrive adds harmonics to the sine",
            h3 > 100 * max(h3b, 1e-6),
            f"3rd harmonic {h3b:.2e} -> {h3:.2e} of the fundamental",
        )
        rev = self.render(
            "c-reverb",
            MODS,
            downs=SINE_DOWNS,
            page="FX",
            turns=(("ENC F", 300),),
            run_ms=2600,
        )
        a = rms(base, 70000, 100000)
        b = rms(rev, 70000, 100000)
        self.check(
            "the reverb send adds a tail after the note",
            b > 5 * max(a, 1e-9),
            f"tail rms {a:.5f} -> {b:.5f}",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out", type=Path, default=ROOT / "out" / "runs" / "voice-proof-dn2"
    )
    ap.add_argument("--only", default="a,b,c")
    a = ap.parse_args()
    proof = Proof(a.out)
    only = set(a.only.split(","))
    if "a" in only:
        proof.part_a()
    if "b" in only:
        proof.part_b()
    if "c" in only:
        proof.part_c()
    print("PASS" if not proof.failed else f"FAIL: {', '.join(proof.failed)}")
    return 1 if proof.failed else 0


if __name__ == "__main__":
    sys.exit(main())
