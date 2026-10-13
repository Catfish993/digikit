#!/usr/bin/env python3
"""patchkit Phase 3 proof: core/machines on Digitakt II 1.17 (machines/xslice and a second clone).

    uv run python tools/pk_proof_machines.py [--out DIR] [--only a,b,c] [--keep]

Drives patched images on coupled daemons through the harness (`tools/pk_harness.py` Session: panel inputs, health
asserted after every input) and reads back:

  a  select      FUNC+SRC lists the machine, YES commits: the track object's type byte (0x4265338e, the word at
                 0x4265338c), the TX frame's machine word (0x80005348 + 0x94) and the DSP's cache (DM 0x255970) all
                 carry the type number; the SRC page opens. PCM of an audition of XSLICE equals stock MANUAL SLICE
                 (the DSP selector word of the clone is copied, finding 02 "core.machines").
  b  persist     save the project with an XSLICE track, set the track to SAMPLE, load the project: type 7 is back.
                 The card's writes are exported (`card.overlay`), laid over the card image (`tools/card_overlay.py`) and
                 the same project is loaded on the STOCK firmware: the track loads as SAMPLE, no fault.
  c  two         XSLICE (7) and a second clone (8): both select, both read back their type, both sound like MANUAL
                 SLICE; the same image is built whichever order the modules are named.

Output (PNG frames, WAV, card images) goes under OUT (default out/runs/machines-proof) and is never committed.
Exit 0 only when every check passes. Addresses are the DT2 1.16/1.17 ones of finding 02 and 04.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
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
from pk_proof_drive import aligned_equal, analyse  # noqa: E402

TRACK_OBJ_WORD = 0x4265338C  # the type byte is the third byte of this word (0x4265338e)
TX_TYPE = (
    0x800053DC  # TX frame 0x80005348 + 0x94: big-endian word, track 1 in the high half
)
DSP_CACHE = 0x255970  # DSP DM: a 16-bit type per track at +2t
SLICE_DOWNS = (
    4  # presses of DOWN from the top of the machine list to SLICE, XSLICE, XClone
)
XSLICE_DOWNS = 7
XCLONE_DOWNS = 8
FW = "1.17"  # --fw
MODS_ONE = ["core/machines", "machines/xslice"]
MODS_TWO = ["core/machines", "machines/xslice", "test/machine-clone"]


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

    # -- one daemon
    def session(self, mods: list[str]) -> H.Session:
        cell = H.Cell("dt2", FW, list(mods))
        s = H.Session(
            cell,
            H.Goldens(),
            log=None,
            run_name="machines-" + "-".join(m.rsplit("/", 1)[-1] for m in mods),
        )
        s.open()
        return s

    def fresh(self, s: H.Session) -> None:
        """Back to the ready state, with time for the sample slot to finish loading."""
        s.reset()
        s.run_ms(1500)

    def snap(self, s: H.Session, name: str) -> None:
        _, raw = s.emu.frame()
        from digi_client import oled_png

        (self.out / f"{name}.png").write_bytes(oled_png(raw))

    def select(self, s: H.Session, downs: int, ups: int = 0) -> None:
        """FUNC+SRC, DOWN x downs (the cursor starts on the track's machine; UP x ups first), YES (commits, the
        menu stays), YES (closes)."""
        s.do_tap(["FUNC", "SRC"])
        s.run_ms(200)
        for key, n in (("UP", ups), ("DOWN", downs)):
            for _ in range(n):
                s.do_tap(key)
                s.run_ms(80)
        s.do_tap("YES")
        s.run_ms(300)
        s.do_tap("YES")
        s.run_ms(300)

    def type_now(self, s: H.Session) -> int:
        word = s.emu.peek32(TRACK_OBJ_WORD) or 0
        return (word >> 8) & 0xFF

    def audition(self, s: H.Session, name: str) -> np.ndarray:
        wav = self.out / f"{name}.wav"
        s.emu.call("pcm.tap", action="capture", path=str(wav))
        s.do_hold("TRIG 1", 500)
        s.run_ms(500)
        s.emu.call("pcm.tap", action="capture_stop")
        s.health(f"audition {name}")
        return hook_ab.read_wav(wav)

    def wire(self, s: H.Session) -> tuple[int, int]:
        """(TX frame machine word of track 1, DSP cache word of track 1): meaningful after an audition."""
        tx = ((s.emu.peek32(TX_TYPE) or 0) >> 16) & 0xFFFF
        dsp = int.from_bytes(
            bytes.fromhex(s.emu.dsp_peek(DSP_CACHE, 2)["hex"]), "little"
        )
        return tx, dsp

    def same_sound(
        self, name: str, ref: np.ndarray, got: np.ndarray, label: str
    ) -> None:
        a = analyse(ref)
        ok, shift = aligned_equal(ref, got)
        self.check(
            f"{name}: PCM equals {label}",
            ok and a.get("silent", True) is not True,
            f"{a.get('active_samples')} active samples, shift {shift}",
        )

    def sequence(self, s: H.Session, downs: int, name: str) -> np.ndarray:
        """The machine on track 1, TRIG 1 (audition and step 1), then the sequencer plays the step for 2 s."""
        self.fresh(s)
        self.select(s, downs)
        s.do_tap("TRIG 1")
        s.run_ms(
            1600
        )  # the audition (a 1 s sample) is over before the sequencer retriggers the track
        wav = self.out / f"{name}.wav"
        s.emu.call("pcm.tap", action="capture", path=str(wav))
        s.do_tap("PLAY")
        s.run_ms(2000)
        s.emu.call(
            "pcm.tap", action="capture_stop"
        )  # before STOP: the release after STOP is timing, not sound
        s.do_tap("STOP")
        s.run_ms(300)
        s.health(f"sequence {name}")
        return hook_ab.read_wav(wav)

    def dest_frames(self, s: H.Session, downs: int, upto: int = 36) -> list[str]:
        """MOD page 1, the DEST knob (encoder D) turned one detent at a time: the frame hash after each turn from
        the 22nd on, where the destination list (grouped by owner: META, SRC, ...) comes up."""
        self.fresh(s)
        self.select(s, downs)
        s.do_tap("MOD")
        s.run_ms(300)
        out = []
        for i in range(upto):
            s.do_turn("ENC D", 1)
            s.run_ms(150)
            if i >= 24:
                # the title bar (the top 3 of 8 pages of the OLED: 3 * 128 bytes) animates and differs by timing, and
                # the focus moves into the SRC group during the 23rd turn: compare the settled frames
                out.append(hashlib.sha256(s.frame()[1][384:]).hexdigest()[:12])
        return out

    # -- the parts
    def part_a(self) -> dict[str, np.ndarray]:
        print(f"a  select (stock, then machines/xslice on {FW})")
        pcm: dict[str, np.ndarray] = {}
        with self.session([]) as s:
            self.fresh(s)
            self.select(s, SLICE_DOWNS)
            self.check(
                "stock: SLICE is type 6", self.type_now(s) == 6, f"{self.type_now(s)}"
            )
            pcm["stock_slice"] = self.audition(s, "stock-slice")
            self.check(
                "stock: SLICE sounds",
                not analyse(pcm["stock_slice"]).get("silent", True),
            )
        with self.session(MODS_ONE) as s:
            self.fresh(s)
            self.select(s, SLICE_DOWNS)
            self.check("patched: SLICE is still type 6", self.type_now(s) == 6)
            pcm["patched_slice"] = self.audition(s, "patched-slice")
            self.same_sound(
                "patched SLICE", pcm["stock_slice"], pcm["patched_slice"], "stock SLICE"
            )
            self.fresh(s)
            s.do_tap(["FUNC", "SRC"])
            s.run_ms(200)
            for _ in range(XSLICE_DOWNS):
                s.do_tap("DOWN")
                s.run_ms(80)
            self.snap(s, "xslice-menu")
            s.do_tap("YES")
            s.run_ms(300)
            s.do_tap("YES")
            s.run_ms(300)
            t = self.type_now(s)
            self.check("XSLICE commits type 7", t == 7, f"track object type {t}")
            s.do_tap("SRC")
            s.run_ms(300)
            self.snap(s, "xslice-src")
            s.health("SRC page")
            pcm["xslice"] = self.audition(s, "xslice")
            tx, dsp = self.wire(s)
            self.check("TX frame machine word is 7", tx == 7, f"{tx:#06x}")
            self.check("DSP type cache (DM 0x255970) is 7", dsp == 7, f"{dsp}")
            self.same_sound("XSLICE", pcm["stock_slice"], pcm["xslice"], "stock SLICE")
            s.health("end of a")
        return pcm

    def part_b(self) -> None:
        print("b  persistence (patched save and load, then stock load)")
        overlay = self.out / "card.overlay"
        with self.session(MODS_ONE) as s:
            self.fresh(s)
            self.select(s, XSLICE_DOWNS)
            self.check("XSLICE selected before the save", self.type_now(s) == 7)
            self.save_project(s)
            self.select(s, 0, ups=XSLICE_DOWNS + 2)  # to the top of the list: SAMPLE
            self.check(
                "track set to SAMPLE", self.type_now(s) == 0, f"{self.type_now(s)}"
            )
            self.load_project(s)
            t = self.type_now(s)
            self.check("project reload brings type 7 back", t == 7, f"{t}")
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
            self.snap(s, "stock-loaded")
            s.run_ms(500)
            s.do_hold("TRIG 1", 300)
            s.run_ms(300)
            s.health("stock after loading the patched project")
            self.check("stock keeps running (no fault, halt or stall)", True)

    def part_b_control(self) -> None:
        """Control: the same image with the persistence immediate put back (one byte): the type must NOT survive."""
        print("b2 control (persistence immediate left at the stock value)")
        import tomllib

        import port_resolve as pr
        from dt2 import build as dbuild

        src = ROOT / "out" / "patch" / f"proof-xslice-{FW}.syx"
        r = subprocess.run(
            [sys.executable, str(HERE / "patchkit.py"), "patch", "--firmware", str(ROOT / f"Digitakt_II_OS{FW}.syx"),
             "--modules", ",".join(MODS_ONE), "-o", str(src)],
            capture_output=True, text=True, cwd=ROOT,
        )  # fmt: skip
        self.check("patch for the control", r.returncode == 0)
        port = tomllib.loads(
            (ROOT / "devices" / "ports" / f"dt2-{FW}.toml").read_text()
        )
        site = next(a for a in port["anchor"] if a["name"] == "cf.machines.persist")[
            "addr"
        ]
        main = bytearray(pr.sections_of(src)[3])
        at = site + 1 - 0x40000400
        self.check(
            "the persistence immediate is the patched one", main[at] == 9, f"{main[at]}"
        )
        main[at] = 8
        img = self.out / "proof-nopersist.syx"
        img.write_bytes(
            dbuild.rebuild(
                str(src),
                replacements={3: bytes(main)},
                pack="aplib",
                max_container=dbuild.FLASH_CONTAINER_MAX,
            )
        )
        cell = H.Cell("dt2", FW, list(MODS_ONE))
        s = H.Session(cell, H.Goldens(), log=None, run_name="machines-nopersist")
        with s:
            s.open(image=img)
            self.fresh(s)
            self.select(s, XSLICE_DOWNS)
            self.check("control: XSLICE selected", self.type_now(s) == 7)
            self.save_project(s)
            self.select(s, 0, ups=XSLICE_DOWNS + 2)
            self.load_project(s)
            t = self.type_now(s)
            self.check(
                "control: without the immediate the type falls back to SAMPLE",
                t == 0,
                f"{t}",
            )

    def save_project(self, s: H.Session) -> None:
        s.do_tap(["FUNC", "SETTINGS"])
        s.run_ms(300)
        s.do_tap("YES")
        s.run_ms(3000)

    def load_project(self, s: H.Session) -> None:
        s.do_tap("SETTINGS")
        s.run_ms(200)
        s.do_tap("YES")
        s.run_ms(200)
        s.do_tap("YES")
        s.run_ms(500)
        s.do_tap(
            "YES"
        )  # the list entry: "Any changes will be lost. Proceed with loading?"
        s.run_ms(500)
        s.do_tap("YES")
        s.run_ms(6000)

    def part_c(self, pcm: dict[str, np.ndarray]) -> None:
        print("c  two machine modules")
        sets = {}
        for name, mods in (
            ("sorted", MODS_TWO),
            ("reversed", list(reversed(MODS_TWO))),
        ):
            img = ROOT / "out" / "patch" / f"proof-two-{name}-{FW}.syx"
            r = subprocess.run(
                [sys.executable, str(HERE / "patchkit.py"), "patch", "--firmware", str(ROOT / f"Digitakt_II_OS{FW}.syx"),
                 "--modules", ",".join(mods), "-o", str(img)],
                capture_output=True, text=True, cwd=ROOT,
            )  # fmt: skip
            self.check(
                f"patch ({name} order)",
                r.returncode == 0,
                (r.stderr or r.stdout).strip().splitlines()[-1],
            )
            sets[name] = hashlib.sha256(img.read_bytes()).hexdigest()
        self.check(
            "same bytes in any selection order",
            sets["sorted"] == sets["reversed"],
            sets["sorted"][:12],
        )
        with self.session(MODS_TWO) as s:
            for label, downs, want in (
                ("XSLICE", XSLICE_DOWNS, 7),
                ("XClone", XCLONE_DOWNS, 8),
            ):
                self.fresh(s)
                self.select(s, downs)
                t = self.type_now(s)
                self.check(f"{label} commits type {want}", t == want, f"{t}")
                self.snap(s, f"two-{label.lower()}")
                p = self.audition(s, f"two-{label.lower()}")
                tx, dsp = self.wire(s)
                self.check(
                    f"{label}: TX {tx} and DSP cache {dsp} carry {want}",
                    tx == want and dsp == want,
                )
                self.same_sound(label, pcm["stock_slice"], p, "stock SLICE")
            s.health("end of c")


def part_d(proof: Proof) -> None:
    print("d  modulation destination picker (MOD page, DEST knob)")
    with proof.session([]) as s:
        stock = proof.dest_frames(s, SLICE_DOWNS)
    with proof.session(MODS_TWO) as s:
        for label, downs in (("XSLICE", XSLICE_DOWNS), ("XClone", XCLONE_DOWNS)):
            got = proof.dest_frames(s, downs)
            proof.check(
                f"{label}: the destination lists (with the SRC group) equal MANUAL SLICE's",
                got == stock,
                f"{len(got)} frames",
            )


def part_e(proof: Proof) -> None:
    print("e  sequencer: the step plays")
    with proof.session([]) as s:
        stock = proof.sequence(s, SLICE_DOWNS, "seq-stock-slice")
    proof.check(
        "stock: SLICE plays from the sequencer", not analyse(stock).get("silent", True)
    )
    with proof.session(MODS_TWO) as s:
        for label, downs in (("XSLICE", XSLICE_DOWNS), ("XClone", XCLONE_DOWNS)):
            got = proof.sequence(s, downs, f"seq-{label.lower()}")
            proof.same_sound(f"{label} (sequencer)", stock, got, "stock SLICE")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out", type=Path, default=ROOT / "out" / "runs" / "machines-proof"
    )
    ap.add_argument("--only", default="a,b,b2,c,d,e")
    ap.add_argument("--fw", default="1.17", choices=("1.16", "1.17"))
    a = ap.parse_args()
    global FW
    FW = a.fw
    proof = Proof(
        a.out
        or ROOT
        / "out"
        / "runs"
        / ("machines-proof" if FW == "1.17" else f"machines-proof-{FW}")
    )
    only = set(a.only.split(","))
    pcm = proof.part_a() if "a" in only else {}
    if "b" in only:
        proof.part_b()
    if "b2" in only:
        proof.part_b_control()
    if "d" in only:
        part_d(proof)
    if "e" in only:
        part_e(proof)
    if "c" in only:
        if not pcm:
            print("c needs the PCM of a: run with --only a,c")
            return 2
        proof.part_c(pcm)
    print("PASS" if not proof.failed else f"FAIL: {', '.join(proof.failed)}")
    return 1 if proof.failed else 0


if __name__ == "__main__":
    sys.exit(main())
