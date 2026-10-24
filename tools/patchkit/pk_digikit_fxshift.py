#!/usr/bin/env python3
"""The `fxshift` kind of the digikit scenarios: the frequency shifter `digikit/freqshift` on a Digitakt II, page, knobs and sound (docs/findings/54-module-cookbook.md, 61-fx-pages.md).

Run by `tools/patchkit/pk_digikit_scen.py` (scenario `fx-freqshift-dt2`). The image is the runtime with `core/dynfx` and `core/fxpages`; the sound is the run-time machine `test/sine` on track 1,
a sine of one frequency f0. Two boots, each in its own daemon:

  dry     the sine alone: the reference PCM and f0.
  shift   the sine and the effect (tab FSH, title FREQ SHIFTER, the knob SHIFT -64 .. +63 bipolar, default 0, MIX default 0):
          page A  loaded with its defaults the effect changes nothing: the PCM is the dry PCM bit for bit; the page row, the names, the value indices and the defaults are the manifest's;
          B       MIX turned to the top, SHIFT 0: the sound is the dry sound's magnitude (an allpass pair: 0.5 % in amplitude);
          C       SHIFT turned up: p0 is the float the knob stands for and the sine moves by exactly the shift of the routine's mapping (inc 48000 / 2^32 Hz); the image is 40 dB down;
          D       SHIFT turned down: the same downwards;
          E       the SHIFT and MIX values go into a kit (Save Kit), SHIFT is turned back up, Load Kit gives the saved value back (kit array, DSP row word, and the sound moves down again).

Screenshots (the default page, the page while SHIFT is turned each way, MIX turned) go to the scenario's work folder; `DIGIKIT_SHOTS=DIR` copies them to DIR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from tools.patchkit import freqshift_ref as R
from tools.patchkit import pk_device
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit.pk_digikit_fx import W_NFAULT, W_P0, chain_rows, scaled
from tools.patchkit.pk_digikit_fxpage import (
    ENC_A,
    ENC_LEVEL,
    ONE,
    SINE,
    STOCK_PAGES,
    W_MIX,
    Kit,
    Shots,
    bits,
    knob_float,
    open_send_fx,
    page_down,
    profile,
    unbits,
)
from tools.patchkit.pk_digikit_fxpersist import KIT_AREAS, Run

FX = "digikit/freqshift"
LO, HI = -64.0, 63.0  # the SHIFT knob
FS = 48000.0
RAW_MAX = 0x7F00


def channels(wav: Path) -> np.ndarray:
    """The left channel of a float WAV (the capture is stereo)."""
    data = wav.read_bytes()
    pos, nch = 12, 2
    while pos + 8 <= len(data):
        tag, size = (
            data[pos : pos + 4],
            int.from_bytes(data[pos + 4 : pos + 8], "little"),
        )
        if tag == b"fmt ":
            nch = int.from_bytes(data[pos + 10 : pos + 12], "little")
        elif tag == b"data":
            x = np.frombuffer(data[pos + 8 : pos + 8 + size // 4 * 4], dtype="<f4")
            return x.reshape(-1, nch)[:, 0].astype(np.float64)
        pos += 8 + size + (size & 1)
    raise ValueError(f"{wav}: no data chunk")


def amplitude(y: np.ndarray, f: float) -> float:
    """The amplitude of the component of y at exactly f Hz (a Hann-windowed single-bin projection)."""
    n = np.arange(len(y))
    w = np.hanning(len(y))
    return float(2 * abs(np.sum(w * y * np.exp(-2j * np.pi * f * n / FS))) / np.sum(w))


def find_f0(y: np.ndarray) -> float:
    """The frequency of the strongest component, refined by the projection (the window is 0.7 s: 1.4 Hz bins)."""
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)), 8 * len(y)))
    f = float(np.argmax(spec[100:])) + 100
    f0 = f * FS / (8 * len(y))
    best = f0
    for step in (0.2, 0.02, 0.002):
        grid = best + step * np.arange(-10, 11)
        best = float(grid[int(np.argmax([amplitude(y, g) for g in grid]))])
    return best


def run_fxshift(c: Any) -> None:
    from tools.patchkit.pk_digikit_scen import RT, cstr

    t = c.table()
    hint = t.get("hint", 7)
    trig = t.get("trig_frame", M.TRIG_FRAME)
    cfg = profile(c.device)
    img = c.image_for(RT + ",core/dynfx,core/fxpages", "fxshift")
    sine = c.pack(SINE, "m0.dkm")
    fxf = c.pack(FX, "m1.dkm")
    card_sine, card_fx = c.card([sine]), c.card([sine, fxf])
    pcm: dict[str, np.ndarray] = {}
    raw_pcm: dict[str, np.ndarray] = {}
    exp: dict[str, float] = {}  # the p0 words of the runs (floats)

    def to_main(rig: Any) -> None:
        rig.s.do_tap("NO", 20)
        rig.s.run_ms(300)

    def sound(rig: Any, name: str, again: bool = True) -> None:
        wav = c.work / f"{name}.wav"
        raw_pcm[name] = rig.note(wav, rig.frames() + 300 if again else trig)
        pcm[name] = channels(wav)

    def dry() -> None:
        with c.rig(img, card_sine, "dry") as rig:
            info = rig.boot_info()
            c.check(
                "dry: the sine loaded", info["results"] == [0], f"{info['results']}"
            )
            rig.pick(rig.dyn_type(SINE, "sine"), hint)
            to_main(rig)
            sound(rig, "dry", again=False)
            c.done(rig)

    def shift() -> None:
        with c.rig(img, card_fx, "shift") as rig:
            info = rig.boot_info()
            c.check(
                "shift: the sine and the effect loaded",
                info["results"] == [0, 0] and info["safe"] == 0,
                f"{info['results']}",
            )
            rig.pick(rig.dyn_type(SINE, "sine"), hint)
            shots = Shots(c.work, "fsh")
            plan = rig.plan
            slots = pk_device.limits(c.device).fx_slots
            open_send_fx(rig)
            for _ in range(3):
                page_down(rig)
            rig.s.run_ms(600)
            shots.take(rig, "page-default")
            # the page the runtime wrote
            count = rig.w(M.sym(plan, "core/fxpages", "fxp_count"))
            lst = M.sym(plan, "core/fxpages", "fxp_list_buf")
            listed = [rig.w(lst + 4 * i) for i in range(count)]
            row0 = M.sym(plan, "core/fxpages", "fxp_rows")
            first = cfg["page_first"]
            c.check(
                "shift: the FX view has four pages: the three stock pages and the effect's",
                count == 4 and listed == [*STOCK_PAGES[c.device], first],
                f"count {count} {listed}",
            )
            p_first = M.sym(plan, "core/fxpages", "params.fp0.index")
            codes = [rig.w(row0 + 8 + 4 * i) for i in range(9)]
            c.check(
                "shift: the page row has SHIFT at position 0, nothing at 1 to 7 and MIX on the level slot",
                codes == [p_first, 0, 0, 0, 0, 0, 0, 0, p_first + 7],
                f"{codes}",
            )
            c.check(
                "shift: the page's tab is FSH and its title FREQ SHIFTER",
                cstr(rig, rig.w(row0)) == "FSH"
                and cstr(rig, rig.w(row0 + 4)) == "FREQ SHIFTER",
                f"{cstr(rig, rig.w(row0))!r} {cstr(rig, rig.w(row0 + 4))!r}",
            )
            pf = M.sym(plan, "core/params", "pf_table")

            def prow(code: int) -> list[int]:
                return [rig.w(pf + 60 * code + 4 * i) for i in range(15)]

            g, m = prow(p_first), prow(p_first + 7)
            idx = cfg["value_indices"]
            c.check(
                "shift: the SHIFT row is the effect row's value index 0, range 0 .. 0x7f00, default 0x4000 (SHIFT 0, the middle of the bipolar knob); MIX is index 7, default 0",
                (g[1], g[2], g[3], g[4]) == (idx[0], 0, 0x7F00, 0x4000)
                and (m[1], m[2], m[3], m[4]) == (idx[7], 0, 0x7F00, 0),
                f"{g[1:5]} {m[1:5]}",
            )
            c.check(
                "shift: the names are Freq Shift / SHIFT and Mix / MIX",
                (cstr(rig, g[10]), cstr(rig, g[12]), cstr(rig, m[10]), cstr(rig, m[12]))
                == ("Freq Shift", "SHIFT", "Mix", "MIX"),
                f"{cstr(rig, g[10])} {cstr(rig, g[12])} {cstr(rig, m[10])} {cstr(rig, m[12])}",
            )
            kit = Kit(rig, cfg)
            c.check(
                "shift: the kit's value array holds the defaults: SHIFT raw 0x4000, MIX raw 0",
                (kit.raw(0, 0), kit.raw(0, 7)) == (0x4000, 0),
                f"{kit.raw(0, 0):#x} {kit.raw(0, 7):#x}",
            )
            row = chain_rows(rig, slots)[0]
            c.check(
                "shift: the chain's row has p0 = 0.0 and the mix word 0.0 (loading the effect does not change the sound)",
                row[W_P0] == 0 and row[W_MIX] == 0,
                f"{row[W_P0]:#x} {row[W_MIX]:#x}",
            )

            def words() -> tuple[int, int]:
                r = chain_rows(rig, slots)[0]
                return r[W_P0], r[W_MIX]

            r = Run(c, rig, cfg)

            def to_page() -> None:
                r.fx_page(
                    3
                )  # the FX view keeps its last page: top first, then DEL, REV, CHO, the effect's

            # A: the defaults
            to_main(rig)
            sound(rig, "a")
            # B: MIX to the top
            to_page()
            kit.set_raw(ENC_LEVEL, 0, 7, 0x7000)
            rig.s.turn_burst(ENC_LEVEL, 8, 60)  # on to the top stop
            rig.s.run_ms(60)
            c.check(
                "shift: MIX turned to the top gives raw 0x7f00 and a mix word of exactly 1.0",
                kit.raw(0, 7) == RAW_MAX and words()[1] == ONE,
                f"{kit.raw(0, 7):#x} {words()[1]:#x}",
            )
            shots.take(rig, "page-mix-top")
            to_main(rig)
            sound(rig, "b")

            def turn_to(raw: int, name: str) -> float:
                to_page()
                got = kit.set_raw(ENC_A, 0, 0, raw)
                rig.s.run_ms(100)
                shots.take(rig, f"page-shift-{name}")
                want = knob_float(LO, HI, got)
                p0, mix = words()
                c.check(
                    f"shift: turning SHIFT to raw {got:#x} puts p0 = {want:.5f} (min + span * raw / 0x7f00 in float32) in the chain's row, the mix stays at 1.0",
                    p0 == bits(want) and mix == ONE,
                    f"p0 {unbits(p0):.6f} want {want:.6f}, mix {mix:#x}",
                )
                exp[name] = want
                return want

            # C: up, D: down
            turn_to(0x6800, "up")
            to_main(rig)
            sound(rig, "c")
            turn_to(0x1800, "down")
            to_main(rig)
            sound(rig, "d")
            # E: save with SHIFT down (A:002), turn it up, load: the saved value is back and the sound moves down again
            c.check(
                "shift: no fault of the effect so far (the row's fault count is 0)",
                chain_rows(rig, slots)[0][W_NFAULT] == 0,
                f"{chain_rows(rig, slots)[0][W_NFAULT]}",
            )
            saved = (kit.raw(0, 0), kit.raw(0, 7))
            to_main(rig)
            r.save_kit(2, "fsh")
            to_page()
            kit.set_raw(ENC_A, 0, 0, 0x6800)
            to_main(rig)
            up_raw = kit.raw(0, 0)
            out = r.load_kit(2, "fsh")
            c.check(
                "shift: Load Kit gives SHIFT and MIX back (the kit array, the DSP row's p0 and mix word, the row's words after the load)",
                (kit.raw(0, 0), kit.raw(0, 7)) == saved
                and up_raw != saved[0]
                and words() == (bits(exp["down"]), ONE),
                f"saved {saved} before {up_raw:#x} after {kit.raw(0, 0):#x} {kit.raw(0, 7):#x} row {words()} want {bits(exp['down']):#x} seen {out['seen'][:4]}",
            )
            exp["e"] = exp["down"]
            to_page()
            rig.s.run_ms(300)
            shots.take(rig, "page-loaded")
            to_main(rig)
            sound(rig, "e")
            c.check(
                "shift: no fault of the effect at the end (the row's fault count is 0)",
                chain_rows(rig, slots)[0][W_NFAULT] == 0,
                f"{chain_rows(rig, slots)[0][W_NFAULT]}",
            )
            c.done(rig)

    c.parallel(dry, shift)
    # the sound: the sine of the dry run, then the effect's runs
    win = slice(int(0.3 * FS), int(1.0 * FS))
    y0 = pcm["dry"][win]
    f0 = find_f0(y0)
    a0 = amplitude(y0, f0)
    c.check(
        "dry: the sine is a clean tone (f0 found, amplitude above 0.01)",
        a0 > 0.01 and 20 < f0 < 8000,
        f"f0 {f0:.3f} Hz, amplitude {a0:.4f}",
    )
    rd = scaled(raw_pcm["dry"], raw_pcm["a"], 1.0)
    c.check(
        "shift A: with the defaults (MIX 0) the PCM is the dry PCM bit for bit from the onset on: loading the effect does not change the sound",
        bool(rd["identical"]) and abs(rd["shift"]) <= 128,
        f"onset shift {rd['shift']}, max err {rd.get('max_abs_err')}",
    )
    yb = pcm["b"][win]
    ab = amplitude(yb, f0)
    rms = float(np.sqrt(np.mean(yb**2)) / np.sqrt(np.mean(y0**2)))
    c.check(
        "shift B: MIX 127 and SHIFT 0 keep the sine's frequency and level (an allpass pair: amplitude within 0.5 %, RMS within 0.5 %)",
        abs(ab / a0 - 1) < 0.005 and abs(rms - 1) < 0.005,
        f"f0 {f0:.3f}: amplitude ratio {ab / a0:.5f}, rms ratio {rms:.5f}",
    )
    for name, key in (("c", "up"), ("d", "down"), ("e", "e")):
        v = exp[key]
        inc = R.target_inc(v)
        sh = inc * FS / 2**32
        y = pcm[name][win]
        wanted, image, still = (
            amplitude(y, f0 + sh),
            amplitude(y, abs(f0 - sh)),
            amplitude(y, f0),
        )
        db = 20 * np.log10(wanted / max(image, 1e-12))
        c.check(
            f"shift {name.upper()}: SHIFT {v:+.3f} moves the sine from {f0:.2f} Hz by {sh:+.3f} Hz (the routine's mapping: inc {inc}); level within 0.5 %, the image at {abs(f0 - sh):.2f} Hz is {db:.1f} dB down (at least 40), nothing is left at f0",
            abs(wanted / a0 - 1) < 0.005 and db > 40.0 and still / wanted < 0.01,
            f"wanted {wanted:.4f} ({wanted / a0:.4f} of dry), image {image:.5f}, at f0 {still:.5f}, rejection {db:.1f} dB",
        )
    c.common_checks(KIT_AREAS[c.device])
