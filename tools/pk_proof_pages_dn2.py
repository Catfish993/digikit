#!/usr/bin/env python3
"""patchkit: own parameter pages for an added machine on Digitone II 1.12 (core/machines `pages` registry, core/params
on the Digitone II, test/dn2-pages), in the emulator, through the panel.

    uv run python tools/pk_proof_pages_dn2.py [--quick] [--only a,b,c,d,e] [--out DIR] [--jobs N]

test/dn2-pages is a machine of type 5 with two SYN pages of its own: page 1 knobs A..D = ALPH, BETA, GAMA, DELT (sound-array
slots 25, 26, 27, 30; ranges 0..0x7f00, 0..0x3f00, 0x100..0x7f00, 0..0x7f00; defaults 0x2000, 0x1000, 0x7000, 0x4000),
page 2 knobs A, B = EPSL, ZETA (slots 40, 41; defaults 0x800, 0x7f00). Independent parts, one daemon each:

  a  select   the machine list has PAGES (5th entry, before MIDI); YES commits type 5; the SYN key shows the module's two
              pages (frames differ from FM TONE's and from each other, the cycle is two pages long); the page rows read
              back from the running firmware hold the module's names, titles and codes (std::string descriptors built at
              run time); the stock FM TONE pages are unchanged by the patch (frame hashes equal to the stock firmware's)
  b  knobs    on each page encoders A..H, 30 events of +4 detents 2 ms apart with TRIG 1 held: exactly the mapped slots'
              words of the held voice move (A..D of page 1 on 25, 26, 27, 30; the empty knobs E..H nothing; page 2 A, B on
              40, 41); the ranges clamp (BETA top 0x3f00, GAMA bottom 0x100, GAMA top 0x7f00)
  c  defaults after the machine is selected and a note starts, the words of slots 25, 26, 27, 30, 40, 41 are the module's
              defaults, not FM TONE's
  d  persist  edit values, save the project, set the track to FM TONE, load it: type 5 is current again and the edited
              values are back; the stock firmware loads the same project without a fault (the track becomes FM TONE)
  e  stock    FM TONE (stock firmware vs patched image): SYN/FLTR/AMP page frames equal, same parameters

`--quick` runs a, b (page 1 only) and c. Output (PNG frames) goes under OUT (default out/runs/pages-dn2-proof), never
committed. Exit 0 only when every check passes. Sites and rows: finding 02, "DN2 parameter pages" and its "built" note.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import card_overlay  # noqa: E402
import pk_dn2_common as C  # noqa: E402
import pk_harness as H  # noqa: E402
import pk_proofkit as K  # noqa: E402

MODS = ["core/machines", "core/params", "test/dn2-pages"]
PAGES_DOWNS = 4  # FM TONE, FM DRUM, WAVETONE, SWARMER, PAGES (before MIDI)
PAGES_TYPE = 5
# (encoder, sound-array slot, minimum, maximum, default) per page
PAGE1 = [
    ("ENC A", 25, 0, 0x7F00, 0x2000),
    ("ENC B", 26, 0, 0x3F00, 0x1000),
    ("ENC C", 27, 0x100, 0x7F00, 0x7000),
    ("ENC D", 30, 0, 0x7F00, 0x4000),
]
PAGE2 = [("ENC A", 40, 0, 0x7F00, 0x0800), ("ENC B", 41, 0, 0x7F00, 0x7F00)]
EMPTY1 = ["ENC E", "ENC F", "ENC G", "ENC H"]
EMPTY2 = ["ENC C", "ENC D", "ENC E", "ENC F", "ENC G", "ENC H"]
ALL_SLOTS = [25, 26, 27, 28, 29, 30, 40, 41]


class PagesProof(C.Dn2Proof):
    def turn_page(
        self, s: H.Session, v: int, enc: str, slots: list[int], by: int = 4
    ) -> dict[int, int]:
        """30 events of BY detents 2 ms apart on ENC; the words that changed (slot -> new) in voice V."""
        before = {sl: self.word(s, v, sl) for sl in slots}
        s.turn_burst(enc, by, 30)
        s.run_ms(100)
        after = {sl: self.word(s, v, sl) for sl in slots}
        return {sl: after[sl] for sl in slots if after[sl] != before[sl]}

    def descriptor_rows(self, s: H.Session) -> list[dict]:
        """The `pages` registry rows read from the running firmware: (name, title, nine codes, machine, nine slots)."""
        assert s.emu is not None and s.image is not None
        plan = json.loads(Path(str(s.image)[: -len(".syx")] + ".plan.json").read_text())
        sym = {k.split(":", 1)[1]: v for k, v in plan["symbols"].items()}
        rows = []

        def cstr(p: int) -> str:
            out = b""
            while len(out) < 40:
                w = (s.emu.peek32(p & ~3) or 0).to_bytes(4, "big")
                for b in w[p & 3 :]:
                    if b == 0:
                        return out.decode("latin1")
                    out += bytes([b])
                p = (p & ~3) + 4
            return out.decode("latin1")

        for k in range(sym["pages.count"]):
            a = sym["pages.rows"] + k * sym["pages.row_size"]
            w = [s.emu.peek32(a + 4 * i) or 0 for i in range(21)]
            rows.append(
                {
                    "name": cstr(w[0]),
                    "title": cstr(w[1]),
                    "codes": w[2:11],
                    "machine": w[11],
                    "slots": w[12:21],
                    "refcount": (s.emu.peek32(w[0] - 4) or 0),
                }
            )
        return rows

    # -- a
    def part_a(self, quick: bool) -> None:
        print("a  select: the machine, its pages, the stock pages")
        with self.session(MODS, "a") as s:
            self.fresh(s)
            s.do_tap(["FUNC", "SRC"])
            s.run_ms(300)
            for _ in range(PAGES_DOWNS):
                s.do_tap("DOWN")
                s.run_ms(80)
            self.snap(s, "a-menu")
            s.do_tap("YES")
            s.run_ms(300)
            s.do_tap("YES")
            s.run_ms(300)
            self.check(
                "the reopened menu marks PAGES (row 4) as the track's machine",
                self.menu_row(s, "a-reopened") == 4,
            )
            v = self.hold(s, "TRIG 1")
            self.release(s, "TRIG 1")
            self.check("the audio mirror holds type 5", self.wire_type(s) == PAGES_TYPE)
            frames = []
            for k in range(3):
                s.do_tap("SYN")
                s.run_ms(400)
                frames.append(self.snap(s, f"a-syn{k}"))
            self.check(
                "the SYN key cycles through two pages (the third press is the first again)",
                frames[0] != frames[1] and frames[2] == frames[0],
                frames,
            )
            rows = self.descriptor_rows(s)
            self.check(
                "two page rows read back from the running firmware",
                len(rows) == 2,
                f"{len(rows)}",
            )
            want = [("PGA", "Pages Test"), ("PGB", "Pages Test")]
            self.check(
                "page names and titles are the module's (std::string chars, refcount -1)",
                [(r["name"], r["title"]) for r in rows] == want
                and all(r["refcount"] == 0xFFFFFFFF for r in rows),
                [(r["name"], r["title"], hex(r["refcount"])) for r in rows],
            )
            self.check(
                "page 1 codes are the module's parameter rows, then the track level (10)",
                rows[0]["codes"][4:8] == [0] * 4
                and rows[0]["codes"][8] == 10
                and len(set(rows[0]["codes"][:4])) == 4
                and all(c >= 321 for c in rows[0]["codes"][:4]),
                [hex(c) for c in rows[0]["codes"]],
            )
            self.check(
                "the rows belong to type 5",
                all(r["machine"] == PAGES_TYPE for r in rows),
            )
            s.health("end of a")
            del v

    # -- b
    def which_page(self, s: H.Session) -> int:
        """The page on screen, 1 or 2: encoder A moves slot 25 on page 1, slot 40 on page 2 (0 when neither)."""
        v = self.hold(s, "TRIG 1")
        changed = self.turn_page(s, v, "ENC A", ALL_SLOTS)
        self.release(s, "TRIG 1")
        return {25: 1, 40: 2}.get(next(iter(changed), 0), 0)

    def goto_page(self, s: H.Session, page: int) -> bool:
        for _ in range(4):
            s.do_tap("SYN")
            s.run_ms(400)
            if self.which_page(s) == page:
                return True
        return False

    def part_b(self, quick: bool) -> None:
        print("b  knobs: the encoders move the mapped words")
        with self.session(MODS, "b") as s:
            self.fresh(s)
            self.select(s, PAGES_DOWNS)
            for page, mapped, empty in (
                (1, PAGE1, EMPTY1),
                (2, PAGE2, EMPTY2),
            ):
                if quick and page == 2:
                    break
                self.check(f"the SYN key reaches page {page}", self.goto_page(s, page))
                self.snap(s, f"b-page{page}")
                for enc, slot, _mn, mx, df in mapped:
                    v = self.hold(s, "TRIG 1")
                    # a knob already at its top is turned down
                    changed = self.turn_page(
                        s, v, enc, ALL_SLOTS, -4 if df == mx else 4
                    )
                    self.release(s, "TRIG 1")
                    self.check(
                        f"page {page} {enc} moves slot {slot} only",
                        list(changed) == [slot],
                        {k: hex(x) for k, x in changed.items()},
                    )
                for enc in empty:
                    v = self.hold(s, "TRIG 1")
                    changed = self.turn_page(s, v, enc, ALL_SLOTS)
                    self.release(s, "TRIG 1")
                    self.check(f"page {page} {enc} (empty) moves nothing", not changed)
            if quick:
                s.health("end of b (quick)")
                return
            # ranges: BETA top, GAMA bottom and top (page 1)
            self.check("the SYN key reaches page 1 again", self.goto_page(s, 1))
            v = self.hold(s, "TRIG 1")
            for _ in range(6):
                s.turn_burst("ENC B", 4, 30)
            top_b = self.word(s, v, 26)
            for _ in range(8):
                s.turn_burst("ENC C", -4, 30)
            low_c = self.word(s, v, 27)
            for _ in range(8):
                s.turn_burst("ENC C", 4, 30)
            top_c = self.word(s, v, 27)
            self.release(s, "TRIG 1")
            self.check("BETA stops at its maximum 0x3f00", top_b == 0x3F00, hex(top_b))
            self.check("GAMA stops at its minimum 0x100", low_c == 0x100, hex(low_c))
            self.check("GAMA reaches its maximum 0x7f00", top_c == 0x7F00, hex(top_c))
            s.health("end of b")

    # -- c
    def part_c(self, quick: bool) -> None:
        print("c  defaults after the selection are the machine's own")
        with self.session(MODS, "c") as s:
            self.fresh(s)
            self.select(s, PAGES_DOWNS)
            v = self.hold(s, "TRIG 1")
            self.release(s, "TRIG 1")
            got = {sl: self.word(s, v, sl) for sl in (25, 26, 27, 30, 40, 41)}
            want = {
                25: 0x2000,
                26: 0x1000,
                27: 0x7000,
                30: 0x4000,
                40: 0x0800,
                41: 0x7F00,
            }
            self.check(
                "the six mapped words hold the module's defaults",
                got == want,
                {k: hex(x) for k, x in got.items()},
            )
            s.health("end of c")

    # -- d
    def part_d(self, quick: bool) -> None:
        print("d  persistence")
        overlay = self.out / "card.overlay"
        with self.session(MODS, "d") as s:
            self.fresh(s)
            self.select(s, PAGES_DOWNS)
            self.check("the SYN key reaches page 1", self.goto_page(s, 1))
            v = self.hold(s, "TRIG 1")
            s.turn_burst("ENC A", 4, 30)
            s.turn_burst("ENC C", -4, 30)
            s.run_ms(400)  # the words follow the knob through a smoother
            edited = {sl: self.word(s, v, sl) for sl in (25, 27)}
            self.release(s, "TRIG 1")
            self.check(
                "the knobs moved ALPH and GAMA",
                edited[25] != 0x2000 and edited[27] != 0x7000,
                {k: hex(x) for k, x in edited.items()},
            )
            self.save_project(s)
            r = s.emu.call("card.overlay", path=str(overlay)) if s.emu else {}
            self.check(
                "card writes exported", r.get("sectors", 0) > 0, r.get("sectors")
            )
            types = card_overlay.sound_types(overlay.read_bytes(), type_at=0xF4)
            self.check(
                "the saved project stores type 5 in sound records",
                types.get(5, 0) > 0,
                types,
            )
            s.do_tap(["FUNC", "SRC"])
            s.run_ms(300)
            for _ in range(6):
                s.do_tap("UP")
                s.run_ms(80)
            s.do_tap("YES")
            s.run_ms(300)
            s.do_tap("YES")
            s.run_ms(300)
            self.check("track set to FM TONE", self.menu_row(s, "d-fmtone") == 0)
            self.load_project(s)
            self.check(
                "the loaded project has PAGES on track 1 again",
                self.menu_row(s, "d-loaded") == 4,
            )
            v = self.hold(s, "TRIG 1")
            self.release(s, "TRIG 1")
            back = {sl: self.word(s, v, sl) for sl in (25, 27)}
            self.check(
                "the edited values are back",
                back == edited,
                {k: hex(x) for k, x in back.items()},
            )
            # the stock firmware loads the same project
            s.load(s.syx, settle=False)
            assert s.emu is not None
            s.emu.drain_events()
            s.run_ms(2000)
            s.emu.pcm_start()
            s.pcm_mark = 0
            s.health("stock firmware")
            self.load_project(s)
            self.check(
                "STOCK loads the project: track 1 is FM TONE",
                self.menu_row(s, "d-stock") == 0,
            )
            s.health("end of d")

    # -- e
    def part_e(self, quick: bool) -> None:
        print("e  stock machines: pages unchanged by the patch")

        def pages(s: H.Session, tag: str) -> list[str]:
            self.fresh(s)
            out = []
            for k in ("SYN", "SYN", "SYN", "SYN", "FLTR", "AMP"):
                s.do_tap(k)
                s.run_ms(300)
                out.append(self.snap(s, f"{tag}-{len(out)}"))
            return out

        with self.session([], "e-stock") as s:
            ref = pages(s, "e-stock")
        with self.session(MODS, "e-patched") as s:
            got = pages(s, "e-patched")
        self.check(
            "FM TONE SYN (4 pages), FLTR, AMP frames equal stock's", got == ref, got
        )


PARTS = [
    K.Part("a", "descriptor and pages", lambda p: p.part_a(p.quick), True, 60),
    K.Part("b", "page knobs", lambda p: p.part_b(p.quick), True, 60),
    K.Part("c", "page 2 and more", lambda p: p.part_c(p.quick), True, 60),
    K.Part("d", "d", lambda p: p.part_d(p.quick), False, 80),
    K.Part("e", "persistence and stock", lambda p: p.part_e(p.quick), False, 120),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    K.add_args(ap, PARTS)
    a = ap.parse_args()
    a.out = a.out or ROOT / "out" / "runs" / "pages-dn2-proof"
    return K.run_tool(__file__, PagesProof, PARTS, a)


if __name__ == "__main__":
    sys.exit(main())
