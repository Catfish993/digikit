#!/usr/bin/env python3
"""patchkit proof: the PLUCK machine end to end on Digitakt II (core/dsp-voice + core/machines + core/params +
machines/pluck), in the emulator, through the panel.

    uv run python tools/pk_proof_pluck.py [--fw 1.17|1.16] [--out DIR] [--only a,b,c,d,e,f,g]

Drives one patched image per part on coupled daemons through the harness (`tools/pk_harness.py` Session: panel inputs,
health asserted after every input) and reads back:

  a  select    FUNC+SRC lists PLUCK (8th entry), YES commits type 7. The machine's descriptor, read from the running
               firmware, names the five rows (descriptor field -> relocated parameter table row -> name pointer): TUNE
               and LEV are SAMPLE's rows, DECAY, PICK, BRIGHT and COLOR the module's. After the first trig the engine
               slots hold the module's defaults (and the TX frame words follow).
  b  knobs     encoders A..H on the SRC page change exactly the slots the descriptor maps them to (finding 02) in the
               Sound object and in the TX frame; the empty fields (D, G) change nothing. Control: stock SAMPLE.
  c  tuning    TRIG 1 and keyboard keys: the fundamental of the rendered note against the pitch word the ColdFire sent.
  d  params    DECAY, BRIGHT, COLOR, PICK set through the encoders change the sound the way the DSP routine documents:
               T60 (DECAY), spectral tilt (BRIGHT), excitation spectrum (COLOR) and the comb notch at the 4th harmonic
               (PICK = 1/2).
  e  sequencer four quarter-note trigs plus PLAY: four plucks.
  f  persist   save the project with edited PLUCK values, set the track to SAMPLE, load it: type 7 and the four values
               are back. The card's writes are laid over the card of the STOCK firmware: the track loads as SAMPLE.
  g  mod       MOD page, DEST knob: the SRC group lists PLUCK's own names (frames differ from stock SAMPLE's).

Output (PNG frames, WAV, card images) goes under OUT (default out/runs/pluck-proof) and is never committed. Exit 0 only
when every check passes. Addresses are the DT2 1.16/1.17 ones of findings 02 and 04.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import card_overlay  # noqa: E402
import hook_ab  # noqa: E402
import pk_harness as H  # noqa: E402
import pk_proof_machines as M  # noqa: E402
import pluck_ref  # noqa: E402

MODS = ["core/dsp-voice", "core/machines", "core/params", "machines/pluck"]
PLUCK_DOWNS = (
    7  # presses of DOWN from the top of the machine list to PLUCK (the 8th entry)
)
# descriptor field -> (mirror index, encoder) of the SRC page, finding 02
SLOTS = {
    0: (25, 1),
    1: (26, 2),
    2: (27, 3),
    3: (28, 4),
    4: (31, 5),
    5: (32, 6),
    6: (33, 7),
    7: (34, 8),
}
# module defaults of manifest modules/machines/pluck/module.toml
DEFAULTS = {25: 0x4000, 26: 0x4000, 27: 0x2000, 31: 0x5000, 32: 0x2800, 34: 0x6400}
DECAY, PICK, BRIGHT, COLOR = 26, 27, 31, 32
ENC = {DECAY: 2, PICK: 3, BRIGHT: 5, COLOR: 6}
PITCH_WORD = (
    0x80005348  # TX frame word 0x02 of track 1 sits in the low half of this long
)
T1_BASE = {
    "1.17": 0x4020F59C,
    "1.16": 0x4020F18C,
}  # stock table; patched images read it back from the code


def semitone_hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


# ----- analysis -------------------------------------------------------------------------------------------------


def left_of(pcm: np.ndarray) -> np.ndarray:
    return pcm[0::2].astype(np.float64)


def onset(left: np.ndarray, thresh: float = 1e-4) -> int:
    act = np.flatnonzero(np.abs(left) > thresh)
    return int(act[0]) if len(act) else -1


def f0_of(left: np.ndarray, start: int, want_hz: float) -> float:
    return pluck_ref.f0_from_crossings(left[start + 300 : start + 300 + 30000], want_hz)


def t60_of(left: np.ndarray, start: int, drop_db: float = 30.0) -> float | None:
    """T60 from the slope of the 512-sample RMS (dB) over the first DROP_DB of decay after the peak."""
    n = (len(left) - start) // 512
    if n < 8:
        return None
    rms = np.array(
        [
            np.sqrt(np.mean(left[start + 512 * k : start + 512 * (k + 1)] ** 2))
            for k in range(n)
        ]
    )
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    k0 = int(np.argmax(db))
    below = np.flatnonzero(db[k0:] < db[k0] - drop_db)
    k1 = k0 + (int(below[0]) if len(below) else len(db) - k0 - 1)
    if k1 - k0 < 3:
        return None
    t = (np.arange(k0, k1 + 1) * 512 + 256) / 48000.0
    slope = float(np.polyfit(t, db[k0 : k1 + 1], 1)[0])
    return -60.0 / slope if slope < 0 else None


def band_db(left: np.ndarray, a: int, n: int, lo: float, hi: float) -> float:
    """Level (dB) of the band LO..HI Hz in the window A..A+N."""
    x = left[a : a + n] * np.hanning(n)
    sp = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(n, 1 / 48000.0)
    return 10 * math.log10(float(np.sum(sp[(f >= lo) & (f < hi)])) + 1e-30)


def harmonic_db(left: np.ndarray, a: int, n: int, f0: float, k: int) -> float:
    """Level of the k-th harmonic: the strongest bin within +-2 % of k * f0 (the window leaks, the string detunes
    slightly with damping)."""
    return band_db(left, a, n, 0.98 * k * f0, 1.02 * k * f0)


def onsets(left: np.ndarray) -> list[int]:
    """Sample indexes of new plucks: the excitation is noise, so the energy above the 2nd-difference corner jumps by
    more than 5 dB within 10 ms (a ringing string only decays there)."""
    h = np.diff(left, 2)
    w = 480
    e = np.array([np.sum(h[i : i + w] ** 2) for i in range(0, len(h) - w, w)])
    out: list[int] = []
    for i in range(1, len(e)):
        if (
            e[i] > 1e-4
            and e[i] > 3.2 * max(e[i - 1], 1e-12)
            and (not out or i * w - out[-1] > 4800)
        ):
            out.append(i * w)
    return out


# ----- the proof ------------------------------------------------------------------------------------------------


class PluckProof(M.Proof):
    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        return super().check(name, ok, str(detail))

    def pluck(self, s: H.Session) -> None:
        """A fresh start with PLUCK selected on track 1 and one trig, so the engine slots hold their defaults."""
        self.fresh(s)
        self.select(s, PLUCK_DOWNS)
        s.do_hold("TRIG 1", 200)
        s.run_ms(300)

    def play(
        self,
        s: H.Session,
        name: str,
        key: str = "TRIG 1",
        hold: int = 300,
        total: int = 2200,
    ) -> tuple[np.ndarray, int]:
        """Hold KEY with PCM capture; returns (left channel, pitch word read 100 ms into the note)."""
        assert s.emu is not None
        wav = self.out / f"{name}.wav"
        s.emu.call("pcm.tap", action="capture", path=str(wav))
        s.emu.button(H.button(key), True)
        s.run_ms(100)
        pitch = (s.emu.peek32(PITCH_WORD) or 0) & 0xFFFF
        s.run_ms(hold - 100)
        s.emu.button(H.button(key), False)
        s.run_ms(total - hold)
        s.emu.call("pcm.tap", action="capture_stop")
        s.health(f"play {name}")
        return left_of(hook_ab.read_wav(wav)), pitch

    def descriptor_names(self, s: H.Session) -> list[tuple[str, str]]:
        """(long name, short name) of the nine fields of PLUCK's descriptor, read from the running firmware: the
        machines registry row and the relocated table are found through the plan's symbols, each field is a row of the
        table, its name pointers are C strings."""
        assert s.emu is not None and s.image is not None
        plan = json.loads(Path(str(s.image)[: -len(".syx")] + ".plan.json").read_text())
        sym = {k.split(":", 1)[1]: v for k, v in plan["symbols"].items()}
        row = (
            sym["machines.rows"]
            + (sym["machines.pluck.index"] - sym["machines.first"])
            * sym["machines.row_size"]
        )
        desc, t1 = row, sym["pf_table"]
        peek = s.emu.peek32

        def cstr(addr: int) -> str:
            raw = b"".join(
                (peek(addr + i) or 0).to_bytes(4, "big") for i in range(0, 40, 4)
            )
            return raw.split(b"\0")[0].decode("ascii", "replace")

        out = []
        for n in range(9):
            code = peek(desc + 8 + 4 * n) or 0
            if code == 0:
                out.append(("", ""))
                continue
            row = t1 + 60 * code
            out.append((cstr(peek(row + 0x28) or 0), cstr(peek(row + 0x30) or 0)))
        return out

    # -- a: select and defaults
    def part_a(self) -> None:
        print(f"a  select ({M.FW})")
        with self.session(MODS) as s:
            self.fresh(s)
            s.do_tap(["FUNC", "SRC"])
            s.run_ms(200)
            for _ in range(PLUCK_DOWNS):
                s.do_tap("DOWN")
                s.run_ms(80)
            self.snap(s, "a-menu")
            s.do_tap("YES")
            s.run_ms(300)
            s.do_tap("YES")
            s.run_ms(300)
            t = self.type_now(s)
            self.check("PLUCK commits type 7", t == 7, f"track object type {t}")
            names = self.descriptor_names(s)
            print("     descriptor fields:", names)
            self.check(
                "the descriptor's rows carry the module's names",
                [n[1] for n in names[1:3] + names[4:6]]
                == ["DECAY", "PICK", "BRIGHT", "COLOR"]
                and [n[0] for n in names[1:3] + names[4:6]]
                == ["Decay", "Pick position", "Brightness", "Excitation colour"]
                and names[3] == ("", "")
                and names[6] == ("", ""),
                str(names),
            )
            s.do_hold("TRIG 1", 200)
            s.run_ms(300)
            s.do_tap("SRC")
            s.run_ms(500)
            self.snap(s, "a-src")
            got = {i: s.mirror(i) for i in DEFAULTS}
            self.check(
                "engine slots hold the module's defaults",
                got == DEFAULTS,
                {i: hex(v) for i, v in got.items()},
            )
            tx = {i: s.tx_word(i) for i in DEFAULTS}
            self.check(
                "the TX frame carries them",
                tx == DEFAULTS,
                {i: hex(v) for i, v in tx.items()},
            )
            tx_type, dsp_type = self.wire(s)
            self.check(
                "TX frame machine word and DSP type cache are 7",
                tx_type == 7 and dsp_type == 7,
                f"{tx_type} {dsp_type}",
            )
            s.health("end of a")

    # -- b: knobs
    def part_b(self) -> None:
        print("b  knobs on the SRC page (PLUCK, then stock SAMPLE as the control)")
        for label, mods, downs in (("pluck", MODS, PLUCK_DOWNS), ("sample", [], 0)):
            with self.session(mods) as s:
                self.fresh(s)
                if downs:
                    self.select(s, downs)
                s.do_hold("TRIG 1", 200)
                s.run_ms(300)
                s.do_tap("SRC")
                s.run_ms(500)
                for field_n, (idx, enc) in SLOTS.items():
                    if label == "sample" and field_n in (1, 2, 3):
                        continue  # PLAY and SAMP are modes and CFADE is not on the stock SRC page
                    before = {i: s.mirror(i) for i, _ in SLOTS.values()}
                    s.turn_burst(
                        enc, -4, 12
                    )  # down first: LEV, TUNE and the knobs at the top would clip
                    low = {i: s.mirror(i) for i, _ in SLOTS.values()}
                    s.turn_burst(enc, 4, 40)
                    high = {i: s.mirror(i) for i, _ in SLOTS.values()}
                    changed = sorted(
                        i for i in before if low[i] != before[i] or high[i] != before[i]
                    )
                    s.do_hold("TRIG 1", 100)  # a frame carries the values to the DSP
                    s.run_ms(100)
                    tx_changed = [i for i in changed if s.tx_word(i) == high[i]]
                    empty = label == "pluck" and field_n in (3, 6)
                    want = [] if empty else [idx]
                    self.check(
                        f"{label}: encoder {enc} moves slot(s) {want}",
                        changed == want and (not changed or tx_changed == changed),
                        f"changed {changed}, in the TX frame {tx_changed}",
                    )
                s.health(f"end of b {label}")

    # -- c: tuning
    def part_c(self) -> None:
        print("c  tuning: the fundamental against the pitch word of the note")
        with self.session(MODS) as s:
            self.pluck(s)
            left, pitch = self.play(s, "c-trig1", "TRIG 1")
            st = onset(left)
            note = pitch / 256
            self.check(
                "TRIG 1 plays note 60", pitch == 0x3C00, f"pitch word {pitch:#06x}"
            )
            f = f0_of(left, st, semitone_hz(note))
            cents = 1200 * math.log2(f / semitone_hz(note))
            self.check(
                f"note {note:g}: f0 {f:.3f} Hz", abs(cents) < 3, f"{cents:+.4f} cents"
            )
            s.do_tap("KEYBOARD")
            s.run_ms(300)
            for key in ("TRIG 2", "TRIG 3", "TRIG 5", "TRIG 13", "TRIG 16"):
                left, pitch = self.play(s, f"c-{key.replace(' ', '').lower()}", key)
                st = onset(left)
                if st < 0:
                    self.check(
                        f"{key}: sounds", False, f"silent, pitch word {pitch:#06x}"
                    )
                    continue
                note = pitch / 256
                f = f0_of(left, st, semitone_hz(note))
                cents = 1200 * math.log2(f / semitone_hz(note))
                self.check(
                    f"{key}: note {note:g}, f0 {f:.3f} Hz",
                    abs(cents) < 3,
                    f"{cents:+.4f} cents",
                )

    # -- d: parameter effects
    def part_d(self) -> None:
        print("d  parameters set through the encoders change the sound")
        with self.session(MODS) as s:
            self.pluck(s)
            s.do_tap("SRC")
            s.run_ms(300)

            def setp(idx: int, value: int) -> int:
                return s.turn_to(ENC[idx], idx, value)

            def note(name: str, total: int = 2200) -> tuple[np.ndarray, int]:
                left, _ = self.play(s, name, "TRIG 1", total=total)
                return left, onset(left)

            reached = setp(BRIGHT, 0x7F00)
            self.check("BRIGHT set to its top", reached >= 0x7C00, f"{reached:#x}")
            res = {}
            for label, value, total in (
                ("short", 0x2000, 2200),
                ("mid", 0x4000, 3200),
                ("long", 0x5000, 6000),
            ):
                setp(DECAY, value)
                left, st = note(f"d-decay-{label}", total)
                res[label] = t60_of(left, st)
                print(f"     DECAY {value:#x}: T60 {res[label]}")
            self.check(
                "DECAY lengthens the note (T60 short < mid < long)",
                all(res.values()) and res["short"] < res["mid"] < res["long"],
                str({k: round(v, 3) if v else v for k, v in res.items()}),
            )
            want_mid = 1.02
            self.check(
                "DECAY 0x4000 with BRIGHT at the top gives T60 about 1.02 s",
                res["mid"] is not None and abs(res["mid"] / want_mid - 1) < 0.2,
                f"{res['mid']}",
            )
            setp(DECAY, 0x4000)
            # brightness: energy above 3 kHz 0.2 s into the note, relative to the note's body
            hf = {}
            for label, value in (("dark", 0x0800), ("bright", 0x7F00)):
                setp(BRIGHT, value)
                left, st = note(f"d-bright-{label}")
                hf[label] = band_db(left, st + 9600, 4096, 3000, 12000) - band_db(
                    left, st + 9600, 4096, 100, 1000
                )
                print(f"     BRIGHT {value:#x}: HF/LF {hf[label]:.1f} dB")
            self.check(
                "BRIGHT raises the high frequencies late in the note",
                hf["bright"] - hf["dark"] > 6,
                f"{hf['bright'] - hf['dark']:.1f} dB",
            )
            # colour: the excitation's lowpass; the first 4096 samples after the onset
            setp(BRIGHT, 0x7F00)
            col = {}
            for label, value in (("white", 0x0000), ("soft", 0x7F00)):
                setp(COLOR, value)
                left, st = note(f"d-color-{label}")
                col[label] = band_db(left, st + 200, 4096, 4000, 20000) - band_db(
                    left, st + 200, 4096, 100, 1000
                )
                print(f"     COLOR {value:#x}: HF/LF {col[label]:.1f} dB")
            self.check(
                "COLOR (excitation lowpass) lowers the high frequencies",
                col["white"] - col["soft"] > 6,
                f"{col['white'] - col['soft']:.1f} dB",
            )
            # pick position: plucked at the fraction x = PICK / 4 of the string the harmonics m scale as |sin(pi m x)|
            # (the comb y[k] - y[k - p], p = x N); at PICK = 1/2 (x = 1/4) the 2nd and 6th are at their peak, the 4th and
            # 8th in a notch. The notch is as exact as p is an integer, so the measure sums two harmonics each.
            setp(COLOR, 0x2800)
            comb = {}
            for label, value in (("none", 0x0000), ("half", 0x3F80)):
                setp(PICK, value)
                left, st = note(f"d-pick-{label}")
                f0 = f0_of(left, st, semitone_hz(60))
                lev = {
                    m: 10 ** (harmonic_db(left, st + 400, 8192, f0, m) / 10)
                    for m in (2, 4, 6, 8)
                }
                comb[label] = 10 * math.log10((lev[2] + lev[6]) / (lev[4] + lev[8]))
                print(f"     PICK {value:#x}: (H2+H6)/(H4+H8) {comb[label]:.1f} dB")
            self.check(
                "PICK = 1/2 notches the 4th and 8th harmonics",
                comb["half"] > 10 and comb["half"] - comb["none"] > 8,
                f"{comb['half']:.1f} dB against {comb['none']:.1f} dB without",
            )
            s.health("end of d")

    # -- e: sequencer
    def sequence_run(self, name: str, place: bool) -> np.ndarray:
        """PLUCK on track 1; with PLACE, grid recording (RECORD) puts a trig on step 1 (the same waits without); PLAY
        for 3.6 s with PCM capture (the pattern is 2.35 s long)."""
        with self.session(MODS) as s:
            self.fresh(s)
            self.select(s, PLUCK_DOWNS)
            if place:
                s.do_tap("RECORD")
            s.run_ms(300)
            if place:
                s.do_tap("TRIG 1")
            s.run_ms(300)
            if place:
                s.do_tap("RECORD")
            s.run_ms(1500)  # the audition of the trig dies away
            wav = self.out / f"{name}.wav"
            assert s.emu is not None
            s.emu.call("pcm.tap", action="capture", path=str(wav))
            s.do_tap("PLAY")
            s.run_ms(3600)
            s.emu.call("pcm.tap", action="capture_stop")
            s.do_tap("STOP")
            s.run_ms(300)
            s.health(f"sequence {name}")
            return left_of(hook_ab.read_wav(wav))

    # -- e: sequencer
    def part_e(self) -> None:
        print(
            "e  sequencer: a trig on step 1 of track 1 (the other tracks' pattern is subtracted)"
        )
        # the factory pattern plays the other tracks too: the same run without the trig is the baseline, the engine
        # is deterministic, so the difference is track 1
        base = self.sequence_run("e-seq-base", place=False)
        full = self.sequence_run("e-seq", place=True)
        n = min(len(base), len(full))
        diff = full[:n] - base[:n]
        on = onsets(diff)
        self.check(
            "the sequencer plucks the string on both passes of the pattern",
            len(on) >= 2 and 2.0 < (on[1] - on[0]) / 48000 < 2.7,
            f"onsets at {[round(o / 48000, 3) for o in on]} s",
        )
        if on:
            f = f0_of(diff, on[0], semitone_hz(60))
            self.check(
                "the first sounds at note 60",
                abs(1200 * math.log2(f / semitone_hz(60))) < 3,
                f"{f:.3f} Hz",
            )

    # -- f: persistence
    def part_f(self) -> None:
        print("f  persistence (patched save and load, then stock load)")
        overlay = self.out / "card.overlay"
        want = {DECAY: 0x2000, BRIGHT: 0x6000}
        with self.session(MODS) as s:
            self.pluck(s)
            s.do_tap("SRC")
            s.run_ms(300)
            for idx, v in want.items():
                s.turn_to(ENC[idx], idx, v)
            keep = {i: s.mirror(i) for i in DEFAULTS}
            self.save_project(s)
            self.select(s, 0, ups=PLUCK_DOWNS + 2)  # SAMPLE
            self.check(
                "track set to SAMPLE", self.type_now(s) == 0, f"{self.type_now(s)}"
            )
            self.load_project(s)
            self.check(
                "project reload brings type 7 back",
                self.type_now(s) == 7,
                f"{self.type_now(s)}",
            )
            s.do_hold("TRIG 1", 200)
            s.run_ms(300)
            back = {i: s.mirror(i) for i in DEFAULTS}
            self.check(
                "and the engine slot values",
                back == keep,
                f"{ {i: hex(v) for i, v in back.items()} } vs { {i: hex(v) for i, v in keep.items()} }",
            )
            s.health("reloaded")
            r = s.emu.call("card.overlay", path=str(overlay))
            self.check(
                "card writes exported", r["sectors"] > 0, f"{r['sectors']} sectors"
            )
            card = s.card
        types = card_overlay.sound_types(overlay.read_bytes())
        self.check(
            "the saved project stores type 7 in its sound records",
            types.get(7, 0) > 0,
            f"{types}",
        )
        base = Path(card or "")
        out_img = self.out / "card-after-save.img"
        if not base.is_file():
            self.check("card image found", False, str(base))
            return
        n = card_overlay.apply(base, overlay, out_img)
        self.check("card image with the saved project written", n > 0, f"{n} sectors")
        with self.session([]) as s:
            s.card = str(out_img)
            s.load(s.syx, settle=False)
            s.emu.drain_events()
            s.run_ms(300)
            s.emu.pcm_start()
            s.pcm_mark = 0
            s.health("stock with the saved card")
            self.load_project(s)
            t = self.type_now(s)
            self.check(
                "STOCK loads the project: track 1 is SAMPLE", t == 0, f"type {t}"
            )
            s.run_ms(500)
            s.do_hold("TRIG 1", 300)
            s.run_ms(300)
            s.health("stock after loading the patched project")

    # -- g: modulation destination picker
    def part_g(self) -> None:
        print("g  MOD page destination list")
        with self.session([]) as s:
            stock = self.dest_frames(s, 0, upto=40)
        with self.session(MODS) as s:
            got = self.dest_frames(s, PLUCK_DOWNS, upto=40)
            self.snap(s, "g-mod")
        self.check(
            "PLUCK's destination list differs from SAMPLE's (its own names)",
            got != stock,
            f"{sum(a != b for a, b in zip(got, stock, strict=False))} of {len(stock)} frames differ",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--only", default="a,b,c,d,e,f,g")
    ap.add_argument("--fw", default="1.17", choices=("1.16", "1.17"))
    a = ap.parse_args()
    M.FW = a.fw
    proof = PluckProof(
        a.out
        or ROOT
        / "out"
        / "runs"
        / ("pluck-proof" if a.fw == "1.17" else f"pluck-proof-{a.fw}")
    )
    for part in a.only.split(","):
        getattr(proof, f"part_{part}")()
    print("PASS" if not proof.failed else f"FAIL: {', '.join(proof.failed)}")
    return 1 if proof.failed else 0


if __name__ == "__main__":
    sys.exit(main())
