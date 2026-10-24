#!/usr/bin/env python3
"""The `fxpersist` kind of the digikit scenarios: the values of run-time effects are saved with the kit (docs/findings/61-fx-pages.md "Persistence").

Run by `tools/patchkit/pk_digikit_scen.py` (scenarios `fx-persist-dt2`, quick, and `fx-persist-restart-dt2`, full, which adds the restarts). The image is the runtime with `core/dynfx` and `core/fxpages` (the Digitakt II); the sound is the run-time machine
`test/sine` on track 1; the effects are `test/dyn-fx-gain` (GAIN 0.5, MIX 127) and `test/dyn-fx-gain-b` (another module id, so another effect ID; GAIN 0.25, MIX 64).
One daemon. `fx-persist-restart-dt2` has three boots (the card and the kit slots stay across `restart`), `fx-persist-dt2` only the second, in which the stock kit and the swap
are made with the tables of the kit hooks (see `restarts` below):

  stock  (restarts only) the sine alone: Save Kit (A:001) with a stock FX value turned; the storage record the saver wrote has all spare halfwords 0 (no effect: ID 0 and zero
         values), and the stock value comes back with Load Kit.
  fx     the sine and both effects (rows 0 and 1): the defaults; GAIN and MIX turned; another pattern's kit object holds the defaults and the first pattern's kit keeps the
         turned values; Save Kit (A:002): the spare halfwords hold the 24 values and the 3 IDs (the bytes the firmware reads back from the kit file); the values changed;
         Load Kit gives the saved values back (kit array, DSP row words, PCM, pages); no word of a row is 0 at any time during the load; Load Kit of the stock kit gives the
         defaults (not zeros, not the old values) and the stock value; Save Project and Load Project give the values back.
  swap   the effects in the other rows (selection order; without restarts: the IDs and defaults of the two rows swapped in the hooks' tables): Load Kit of A:002 finds another effect's ID in each row and gives each its own defaults.

Screenshots go to the scenario's work folder; `DIGIKIT_SHOTS=DIR` copies them to DIR.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit.pk_digikit_fx import W_P0, chain_rows, scaled
from tools.patchkit.pk_digikit_fxpage import (
    ENC_A,
    ENC_LEVEL,
    FX,
    SINE,
    W_MIX,
    Kit,
    Shots,
    fxp_state,
    knob_float,
    open_send_fx,
    page_down,
    profile,
    put16,
    u16,
)
from tools.patchkit.pk_harness import button

FX_B = "test/dyn-fx-gain-b"
LO, HI = 0.0, 1.27  # the GAIN knob of both effects
STOCK_IDX = 32  # the kit array index of Delay Time (finding 61: the stock effect values are 25 .. 69)
ID_A = 0x4625  # patchkit `register::effect_id("test/dyn-fx-gain", "gain")`
RAW_MAX = 0x7F00
FXP_MARK = 0x46A7  # the value core/fxpages puts at [fx] mark_index of a kit array that holds its rows
# the firmware's kit library: where Save Kit writes (sectors); the Digitone II keeps it near the start of the card
KIT_AREAS = {"dt2": ((0x16000, 0x17000),), "dn2": ((0x2000, 0x2100),)}
# the defaults of A and B in the kit (raw), None where the page has no knob (the word is not the effect's)
DEF_AB = (
    [0x3200] + [None] * 6 + [0x7F00] + [0x1900] + [None] * 6 + [0x4000] + [None] * 8
)  # the third row has no effect
DEF_BA = (
    [0x1900] + [None] * 6 + [0x4000] + [0x3200] + [None] * 6 + [0x7F00] + [None] * 8
)


def f32(x: float) -> np.float32:
    return np.float32(x)


class Run:
    """The rig, the live kit, the screenshots and the hook state of core/fxpages."""

    def __init__(self, c: Any, rig: Any, cfg: dict[str, Any]):
        self.c, self.rig, self.cfg = c, rig, cfg
        self.kit = Kit(rig, cfg)
        self.shots = Shots(c.work, "p")
        self.nrows = len(cfg["value_indices"]) // 8
        self.spare_hw = cfg["store_halfwords"]

    # -- the firmware's own UI
    def tap(self, keys: Any, ms: int = 300) -> None:
        self.rig.s.do_tap(keys, 20)
        self.rig.s.run_ms(ms)

    def to_main(self) -> None:
        self.tap("NO", 300)

    def cell(self, name: str) -> int:
        return self.rig.w(M.sym(self.rig.plan, "core/fxpages", name))

    def halfwords(self, addr: int, n: int) -> list[int]:
        return [u16(self.rig, addr + 2 * i) for i in range(n)]

    def ids(self) -> list[int]:
        return self.halfwords(
            M.sym(self.rig.plan, "core/fxpages", "fxp_ids"), self.nrows
        )

    def values(self) -> list[int]:
        """The 8 values of each effect row in the live kit."""
        return [self.kit.raw(k, j) for k in range(self.nrows) for j in range(8)]

    def stock(self) -> int:
        kit = self.rig.w(self.cfg["kit_ptr"])
        return u16(self.rig, kit + self.cfg["array_off"] + 2 * STOCK_IDX)

    def poke(self, index: int, raw: int) -> None:
        """Write the halfword INDEX of the live kit's value array (the change the firmware's own pages make; the next poll of the runtime sends it)."""
        addr = self.rig.w(self.cfg["kit_ptr"]) + self.cfg["array_off"] + 2 * index
        put16(self.rig, addr, raw)

    def press_watch(
        self, key: str, total_ms: int, step: int, each: Any = None
    ) -> list[tuple[int, int, int, int]]:
        """Press KEY (released after 60 ms) and read the chain's rows every STEP ms for TOTAL_MS ms: (ms, row, p0, mix) at every change. EACH runs after every step."""
        last = {k: self.row_words(k) for k in range(self.nrows)}
        seen: list[tuple[int, int, int, int]] = []
        emu = self.rig.emu
        emu.button(button(key), True)
        for ms in range(0, total_ms, step):
            self.rig.s.run_ms(step)
            if each is not None:
                each()
            for k in range(self.nrows):
                now = self.row_words(k)
                if now != last[k]:
                    seen.append((ms, k, now[0], now[1]))
                    last[k] = now
            if ms == 60:
                emu.button(button(key), False)
        return seen

    def save_project(self) -> None:
        """FUNC + SETTINGS, slot 001 (as the project round trip does it)."""
        self.tap(["FUNC", "SETTINGS"], 300)
        self.shots.take(self.rig, "project-save-menu")
        self.tap("YES", 500)  # slot 001
        self.tap("YES", 500)  # the name of a new project
        self.rig.s.run_ms(3500)

    def load_project(self) -> list[tuple[int, int, int, int]]:
        """SETTINGS, the first project (four YES, as the project round trip does it); the rows of the chain are watched for 7 s from the last."""
        self.tap("SETTINGS", 300)
        for _ in range(3):
            self.tap("YES", 500)
        return self.press_watch("YES", 7000, 10)

    def poke16(self, addr: int, raw: int) -> None:
        """Write the halfword at ADDR."""
        put16(self.rig, addr, raw)

    def swap_rows(self, a: int, b: int) -> None:
        """Swap the effect IDs and the defaults of two rows in the tables of the kit hooks (the runtime writes them in the order the effects load)."""
        ids = M.sym(self.rig.plan, "core/fxpages", "fxp_ids")
        defs = M.sym(self.rig.plan, "core/fxpages", "fxp_defs")
        ia, ib = self.halfwords(ids + 2 * a, 1)[0], self.halfwords(ids + 2 * b, 1)[0]
        da, db = self.halfwords(defs + 16 * a, 8), self.halfwords(defs + 16 * b, 8)
        self.poke16(ids + 2 * a, ib)
        self.poke16(ids + 2 * b, ia)
        for j in range(8):
            self.poke16(defs + 16 * a + 2 * j, db[j])
            self.poke16(defs + 16 * b + 2 * j, da[j])

    def mark(self) -> int:
        """The mark of core/fxpages in the live kit's array."""
        kit = self.rig.w(self.cfg["kit_ptr"])
        return u16(self.rig, kit + self.cfg["array_off"] + 2 * self.cfg["mark_index"])

    def row_words(self, k: int) -> tuple[int, int]:
        r = chain_rows(self.rig, self.nrows)[k]
        return r[W_P0], r[W_MIX]

    def spare(self, base: int) -> list[int]:
        """The spare halfwords of the storage record at BASE."""
        return self.halfwords(base + self.cfg["store_off"], self.spare_hw)

    def fx_page(self, n: int) -> None:
        """The FX view on page n (0 DEL, 1 REV, 2 CHO, 3 the first effect page ...): the view keeps its last page, so go to the top first."""
        open_send_fx(self.rig)
        for _ in range(5):
            self.tap("UP", 80)
        for _ in range(n):
            page_down(self.rig)

    def page(self, k: int, tag: str) -> str:
        """The FX page of effect row k, settled, as a screenshot hash."""
        self.fx_page(3 + k)
        self.rig.s.run_ms(600)
        return self.shots.take(self.rig, tag)

    def turn(self, k: int, gain: int, mix: int) -> tuple[int, int]:
        """Turn GAIN and MIX of effect row k's page to the raw values (within the helper's tolerance); returns what the kit holds."""
        self.fx_page(3 + k)
        g = self.kit.set_raw(ENC_A, k, 0, gain)
        m = self.kit.set_raw(ENC_LEVEL, k, 7, mix)
        return g, m

    # -- Save Kit and Load Kit (PRESET > KIT, slot A:00n); the menus keep their cursors, so each walk goes to the top first
    def kit_menu(self, entry: int) -> None:
        """PRESET, the KIT column, UP to its first entry (LOAD), then DOWN to ENTRY (0 LOAD, 1 SAVE)."""
        self.tap("PRESET", 500)
        self.tap("RIGHT", 200)
        for _ in range(2):
            self.tap("UP", 100)
        for _ in range(entry):
            self.tap("DOWN", 100)

    def slot(self, n: int) -> None:
        for _ in range(4):  # the slot list has the cursor on a slot; UP stops at A:001
            self.tap("UP", 80)
        for _ in range(n - 1):
            self.tap("DOWN", 80)

    def save_kit(self, n: int, tag: str) -> None:
        self.kit_menu(1)
        self.tap("YES", 600)  # the slot list
        self.slot(n)
        self.shots.take(self.rig, f"{tag}-save-slot")
        self.tap("YES", 600)  # the name
        self.tap("YES", 1500)  # SAVED!
        self.shots.take(self.rig, f"{tag}-saved")
        self.rig.s.run_ms(500)

    def load_kit(self, n: int, tag: str) -> dict[str, Any]:
        """Load Kit from slot n. The last key is held 60 ms and the rows of the chain are read every 2 ms for 0.6 s (the load takes about 0.16 s): `seen` is the list of (ms, row, p0, mix) at every
        change, `file` the spare halfwords of the storage record the loader read (taken the moment the load hook ran)."""
        self.kit_menu(0)
        self.tap("YES", 700)  # the list
        self.slot(n)
        self.shots.take(self.rig, f"{tag}-load-slot")
        file: list[int] = []
        n0 = self.cell("fxp_nload")

        def capture() -> None:
            if not file and self.cell("fxp_nload") != n0:
                file.extend(self.spare(self.cell("fxp_load_src")))

        seen = self.press_watch("YES", 600, 2, capture)
        self.shots.take(self.rig, f"{tag}-loaded")
        return {"seen": seen, "file": file}


def expected_record(ids: list[int], vals: list[int], size: int) -> list[int]:
    """What the hooks write: the values of the rows that have an effect (0 for an empty row), then the IDs, then 0s up to SIZE halfwords."""
    out: list[int] = []
    for k, i in enumerate(ids):
        out += vals[8 * k : 8 * k + 8] if i else [0] * 8
    out += ids
    return out + [0] * (size - len(out))


def factor(vals: list[int]) -> float:
    """The PCM gain of the effects in series: the product over the rows of 1 + mix * (gain - 1)."""
    f = 1.0
    for k in range(2):
        g = knob_float(LO, HI, vals[8 * k])
        m = float(f32(vals[8 * k + 7]) / f32(RAW_MAX))
        f *= 1.0 + m * (g - 1.0)
    return f


def hx(xs: list[int]) -> str:
    return "[" + " ".join(f"{x:x}" for x in xs) + "]"


def is_default(got: list[int], want: list[int | None]) -> bool:
    return all(w is None or abs(g - w) <= 1 for g, w in zip(got, want, strict=True))


def run_fxpersist(c: Any) -> None:
    t = c.table()
    hint = t.get("hint", 7)
    cfg = profile(c.device)
    img = c.image_for("core/runtime,core/dynfx,core/fxpages", "fxpersist")
    sine = c.pack(SINE, "m0.dkm")
    fxa = c.pack(FX, "m1.dkm")
    fxb = c.pack(FX_B, "m2.dkm")
    full = bool(t.get("restarts"))
    # `restarts = true` (scenario fx-persist-restart-dt2, full tier) does what a user does: a boot without the effects saves a kit, a restart brings the effects, another restart
    # swaps their rows. The quick scenario skips both restarts (about 140 s each): it saves the stock kit with the hooks' effect IDs zeroed (the bytes the saver writes with no
    # effect loaded) and swaps the IDs and defaults in the hooks' tables (what the restart with the other order does to them)
    card = c.card([sine] if full else [sine, fxa, fxb])
    pcm: dict[str, np.ndarray] = {}

    with c.rig(img, card, "persist") as rig:
        r = Run(c, rig, cfg)

        def restart() -> None:
            rig.restart()
            rig.emu.pcm_start()
            rig.s.pcm_mark = 0

        def select_sine() -> None:
            rig.pick(rig.dyn_type(SINE, "sine"), hint)

        def note(name: str) -> None:
            r.to_main()
            pcm[name] = rig.note(c.work / f"{name}.wav", rig.frames() + 300)

        def ratio(
            tag: str, ref: str, name: str, gain: float, what: str, ulps: float = 12.0
        ) -> None:
            x = scaled(pcm[ref], pcm[name], gain)
            db = 20 * np.log10(gain)
            c.check(
                f"{tag}: the PCM is the {ref} PCM times {gain:.6f} ({what}), within {ulps} ulps from the onset on",
                x["rms_db"] is not None
                and x["max_err_ulps"] <= ulps
                and abs(x["rms_db"] - db) < 0.001
                and abs(x["shift"]) <= 128,
                f"rms {x['rms_db']} want {db:.4f}, max err {x['max_err_ulps']:.2f} ulps, onset shift {x['shift']}",
            )

        def same(tag: str, ref: str, name: str, what: str) -> None:
            x = scaled(pcm[ref], pcm[name], 1.0)
            c.check(
                f"{tag}: the PCM is the {ref} PCM bit for bit from the onset on ({what})",
                bool(x["identical"]) and abs(x["shift"]) <= 128,
                f"onset shift {x['shift']}, max err {x.get('max_abs_err')}",
            )

        def no_zero(tag: str, seen: list[tuple[int, int, int, int]]) -> None:
            zeros = [s for s in seen if s[2] == 0 or s[3] == 0]
            c.check(
                f"{tag}: no word p0 or mix of a row was 0 at any time while the load ran (the rows read every 2 ms for 0.6 s after a kit load, every 10 ms for 7 s after a project load)",
                not zeros,
                f"changes {[(ms, k, hex(a), hex(b)) for ms, k, a, b in seen]}",
            )

        # ---- stock: the sine alone, no effect ------------------------------------------------------------------------------------------
        stock_k1 = 0x4800
        if full:
            info = rig.boot_info()
            c.check(
                "stock: the sine loaded", info["results"] == [0], f"{info['results']}"
            )
            select_sine()
            raw0 = r.stock()
            r.poke(STOCK_IDX, 0x4800)  # a stock FX value (Delay Time) set in the kit
            stock_k1 = r.stock()
            c.check(
                "stock: the stock value (kit index 32) was set",
                stock_k1 != raw0,
                f"{raw0:#x} -> {stock_k1:#x}",
            )
            n0 = r.cell("fxp_nsave")
            r.save_kit(1, "k1")
            c.check(
                "stock: Save Kit went through the save hook",
                r.cell("fxp_nsave") > n0,
                f"{n0} -> {r.cell('fxp_nsave')}",
            )
            rec = r.spare(r.cell("fxp_save_dst"))
            c.check(
                "stock: with no effect loaded the spare halfwords the saver wrote are all 0 (no values, ID 0 in every row)",
                rec == [0] * len(rec),
                hx(rec),
            )
            r.poke(STOCK_IDX, 0x2000)
            moved = r.stock()
            c.check(
                "stock: the stock value was changed after the save",
                moved != stock_k1,
                f"{moved:#x}",
            )
            nl = r.cell("fxp_nload")
            out = r.load_kit(1, "k1")
            c.check(
                "stock: Load Kit went through the load hook",
                r.cell("fxp_nload") > nl,
                f"{nl} -> {r.cell('fxp_nload')}",
            )
            c.check(
                "stock: Load Kit gives the stock value back (the stock loader and saver are as before)",
                abs(r.stock() - stock_k1) <= 0x40 and r.stock() != moved,
                f"saved {stock_k1:#x}, after load {r.stock():#x}, changed {moved:#x}",
            )
            c.check(
                "stock: the kit file's spare halfwords as the loader read them are all 0",
                out["file"] == [0] * r.spare_hw,
                hx(out["file"]),
            )

        # ---- fx: the sine and both effects, rows 0 and 1 -------------------------------------------------------------------------------
        if full:
            sel = rig.set_selection([sine, fxa, fxb])
            c.check(
                "fx: SELECT of the sine and both effects is accepted",
                sel == 0,
                f"{sel}",
            )
            restart()
        info = rig.boot_info()
        c.check(
            "fx: the sine and both effects are loaded",
            info["results"] == [0, 0, 0] and info["safe"] == 0,
            f"{info['results']}",
        )
        ids = r.ids()
        c.check(
            "fx: the rows' effect IDs are the packer's (test/dyn-fx-gain 0x4625), different for the two effects, 0 for the free row",
            ids[0] == ID_A and ids[1] not in (0, ids[0]) and ids[2] == 0,
            hx(ids),
        )
        st = fxp_state(rig)
        got = r.values()
        # the kit objects were made before the modules were loaded: the runtime gave the live one the defaults and the mark when it registered the effects
        c.check(
            "fx: the boot's kit had been initialised before the effects were here; the runtime seeded its two rows and put the mark in",
            r.cell("fxp_ninit") >= 1
            and st["seeds"] == 2
            and r.mark() == FXP_MARK
            and is_default(got, DEF_AB),
            f"init {r.cell('fxp_ninit')} load {r.cell('fxp_nload')} seeds {st['seeds']} mark {r.mark():#x} {hx(got)}",
        )
        select_sine()
        got = r.values()
        c.check(
            "fx: the live kit holds each effect's defaults (A: GAIN 0.5, MIX 127; B: GAIN 0.25, MIX 64)",
            is_default(got, DEF_AB),
            hx(got),
        )
        dflt = [g if d is None else d for g, d in zip(got, DEF_AB, strict=True)]
        if not full:
            # the stock kit (what a kit saved with no effect holds): saved with the IDs of the hooks zeroed
            ids_at = M.sym(rig.plan, "core/fxpages", "fxp_ids")
            r.poke(STOCK_IDX, stock_k1)
            for k in range(r.nrows):
                r.poke16(ids_at + 2 * k, 0)
            r.save_kit(1, "k1")
            for k in range(r.nrows):
                r.poke16(ids_at + 2 * k, ids[k])
            c.check(
                "fx: the stock kit was saved with no effect ID and no value",
                r.spare(r.cell("fxp_save_dst")) == [0] * r.spare_hw,
                hx(r.spare(r.cell("fxp_save_dst"))),
            )
            r.to_main()
        note("default")

        # turn the values: A and B, GAIN and MIX
        a_g, a_m = r.turn(0, 0x6400, 0x4000)
        b_g, b_m = r.turn(1, 0x2800, 0x2000)
        r.poke(STOCK_IDX, 0x3000)  # a stock FX value in the same kit
        stock_s = r.stock()
        r.to_main()
        S = r.values()
        c.check(
            "fx: the turned values are in the kit (A: GAIN, MIX; B: GAIN, MIX)",
            (S[0], S[7], S[8], S[15]) == (a_g, a_m, b_g, b_m),
            hx(S),
        )
        note("turned")
        ratio(
            "fx turned",
            "default",
            "turned",
            factor(S) / factor(dflt),
            "both effects with the turned GAIN and MIX",
        )
        words_s = [r.row_words(k) for k in range(2)]
        shot4, shot5 = r.page(0, "fx-page4-turned"), r.page(1, "fx-page5-turned")
        r.to_main()

        # another pattern has a kit object of its own, made before the effects were loaded: it plays with the defaults; back on the first pattern the turned values are there
        r.tap("PTN", 800)
        r.tap("TRIG 2", 300)
        waited = 300
        while (
            r.mark() != FXP_MARK and waited < 8000
        ):  # the runtime seeds a kit it has not seen in a pass of its UI loop
            rig.s.run_ms(100)
            waited += 100
        got = r.values()
        c.check(
            "fx: pattern A02's kit (another kit object) holds the defaults, not what the object held before (seeded, marked)",
            is_default(got, DEF_AB) and r.mark() == FXP_MARK and got[:16] != S[:16],
            f"{hx(got)} mark {r.mark():#x} after {waited} ms, live kit {rig.w(cfg['kit_ptr']):#x}, the runtime's {fxp_state(rig)['kit']:#x}, seeds {fxp_state(rig)['seeds']}",
        )
        rig.s.run_ms(1200)
        r.tap("PTN", 800)
        r.tap("TRIG 1", 1500)
        got = r.values()
        c.check(
            "fx: back on pattern A01 the kit has the turned values",
            got[:16] == S[:16],
            f"{hx(got)} want {hx(S)}",
        )

        # Save Kit A:002
        ns = r.cell("fxp_nsave")
        r.save_kit(2, "k2")
        c.check(
            "fx: Save Kit went through the save hook",
            r.cell("fxp_nsave") > ns,
            f"{ns} -> {r.cell('fxp_nsave')}",
        )
        arr = r.cell("fxp_last_save")
        rec = r.spare(r.cell("fxp_save_dst"))
        c.check(
            "fx: the spare halfwords the saver wrote hold the values of the rows with an effect, then the 3 effect IDs, the rest 0 (as computed from the array it read)",
            rec
            == expected_record(
                ids, [u16(rig, arr + 2 * i) for i in cfg["value_indices"]], r.spare_hw
            ),
            f"{hx(rec)} array {arr:#x}",
        )

        # change everything and load the kit back
        # the knobs and MIX of both rows and the stock value change in the kit array (the poll sends them to the DSP)
        vi = cfg["value_indices"]
        for k, g in ((0, 0x1800), (1, 0x5400)):
            r.poke(vi[8 * k], g)
            r.poke(vi[8 * k + 7], 0x7F00)
        r.poke(STOCK_IDX, 0x1000)
        rig.s.run_ms(300)
        r.to_main()
        changed = r.values()
        c.check(
            "fx: the values were changed after the save",
            changed[0] != S[0]
            and changed[8] != S[8]
            and changed[7] != S[7]
            and changed[15] != S[15],
            hx(changed),
        )
        note("changed")
        nl = r.cell("fxp_nload")
        out = r.load_kit(2, "k2")
        c.check(
            "fx: Load Kit went through the load hook",
            r.cell("fxp_nload") > nl,
            f"{nl} -> {r.cell('fxp_nload')}",
        )
        got = r.values()
        c.check(
            "fx: Load Kit gives the saved values back, all 16 of the two rows with an effect (kit array)",
            got[:16] == S[:16],
            f"{hx(got)} want {hx(S)}",
        )
        want = expected_record(ids, S, r.spare_hw)
        c.check(
            "fx: the kit file's spare halfwords as the loader read them are the 24 values and the 3 IDs at the documented offsets (0x60 + 2 i, IDs after the values)",
            out["file"] == want,
            f"{hx(out['file'])} want {hx(want)}",
        )
        c.check(
            "fx: the stock value is as saved (Delay Time)",
            abs(r.stock() - stock_s) <= 0x40,
            f"saved {stock_s:#x}, now {r.stock():#x}",
        )
        c.check(
            "fx: after the load the DSP rows hold the floats of the saved values",
            [r.row_words(k) for k in range(2)] == words_s,
            f"{[tuple(hex(x) for x in r.row_words(k)) for k in range(2)]} want {[tuple(hex(x) for x in w) for w in words_s]}",
        )
        no_zero("fx", out["seen"])
        note("loaded")
        same("fx loaded", "turned", "loaded", "the saved values are in force again")
        x = scaled(pcm["changed"], pcm["loaded"], 1.0)
        c.check(
            "fx loaded: the PCM differs from the changed state's (the load changed the sound)",
            not x["identical"],
            f"{x.get('max_abs_err')}",
        )
        h4, h5 = r.page(0, "fx-page4-loaded"), r.page(1, "fx-page5-loaded")
        c.check(
            "fx loaded: the pages show the saved values (pixel for pixel the pages after turning)",
            (h4, h5) == (shot4, shot5),
            f"{h4[:8]} {h5[:8]} want {shot4[:8]} {shot5[:8]}",
        )
        r.to_main()

        # Load Kit A:001: the kit saved without effects (ID 0 in every row): the defaults
        out = r.load_kit(1, "k1b")
        got = r.values()
        c.check(
            "fx: Load Kit of the kit saved without effects gives each effect its defaults, not zeros and not the old values",
            is_default(got, DEF_AB) and got[:16] != S[:16],
            hx(got),
        )
        no_zero("fx stock kit", out["seen"])
        c.check(
            "fx: the stock kit's spare halfwords as the loader read them are all 0 (no values, ID 0 in every row)",
            out["file"] == [0] * r.spare_hw,
            hx(out["file"]),
        )
        c.check(
            "fx: the stock value is the stock kit's",
            abs(r.stock() - stock_k1) <= 0x40,
            f"{r.stock():#x} want {stock_k1:#x}",
        )
        note("again")
        # the record's default 0.5 reaches the DSP exactly at load; the knob's raw 0x3200 turns into 0.49999997 (the raw steps are 1/32512 of the range): 1 ulp of float
        ratio(
            "fx defaults again",
            "default",
            "again",
            1.0,
            "the defaults are in force again (p0 within one float ulp of the record's default)",
            ulps=4.0,
        )

        # ---- project: Save Project and Load Project keep the values too ------------------------------------------------------------------
        vi = cfg["value_indices"]
        # the project save serialises the live kit as it is then (the saver runs for every kit of the project)
        P = {0: 0x5200, 7: 0x3000, 8: 0x1000, 15: 0x6000}
        for i, v in P.items():
            r.poke(vi[i], v)
        rig.s.run_ms(300)
        words_p = [r.row_words(k) for k in range(2)]
        ns = r.cell("fxp_nsave")
        r.save_project()
        r.shots.take(rig, "project-saved")
        c.check(
            "project: Save Project ran the saver for the kits of the project (one call each)",
            r.cell("fxp_nsave") - ns >= 100,
            f"{ns} -> {r.cell('fxp_nsave')}",
        )
        for i in P:
            r.poke(vi[i], 0x7F00 - P[i] // 2)
        rig.s.run_ms(300)
        ni, nl = r.cell("fxp_ninit"), r.cell("fxp_nload")
        seen = r.load_project()
        r.shots.take(rig, "project-loaded")
        got = r.values()
        c.check(
            "project: Load Project gives the saved values back (kit array, the two rows with an effect)",
            [got[i] for i in P] == list(P.values()),
            f"{hx(got)} want {[hex(v) for v in P.values()]} at {list(P)}; saves {ns} -> {r.cell('fxp_nsave')} (before the load), init {ni} -> {r.cell('fxp_ninit')}, load {nl} -> {r.cell('fxp_nload')}, seeds {fxp_state(rig)['seeds']}",
        )
        c.check(
            "project: after the load the DSP rows hold the floats of the saved values",
            [r.row_words(k) for k in range(2)] == words_p,
            f"{[tuple(hex(x) for x in r.row_words(k)) for k in range(2)]} want {[tuple(hex(x) for x in w) for w in words_p]}",
        )
        no_zero("project", seen)

        # ---- swap: the effects in the other rows ---------------------------------------------------------------------------------------
        if full:
            sel = rig.set_selection([sine, fxb, fxa])
            c.check(
                "swap: SELECT with the effects in the other order is accepted",
                sel == 0,
                f"{sel}",
            )
            restart()
            info = rig.boot_info()
            c.check(
                "swap: after the restart the sine and both effects are loaded",
                info["results"] == [0, 0, 0] and info["safe"] == 0,
                f"{info['results']}",
            )
            ids2 = r.ids()
            c.check(
                "swap: row 0 now holds effect B and row 1 effect A",
                ids2[:3] == [ids[1], ids[0], 0],
                hx(ids2),
            )
            select_sine()
        else:
            r.swap_rows(0, 1)
            c.check(
                "swap: the hooks' tables hold B in row 0 and A in row 1",
                r.ids()[:3] == [ids[1], ids[0], 0],
                hx(r.ids()),
            )
        out = r.load_kit(2, "swap")
        got = r.values()
        c.check(
            "swap: Load Kit of the kit saved with A in row 0 and B in row 1 gives each row the defaults of the effect now in it (another ID), not the stored values",
            is_default(got, DEF_BA) and got[0] != S[0],
            hx(got),
        )
        no_zero("swap", out["seen"])
        c.done(rig)
    c.common_checks(KIT_AREAS[c.device])
