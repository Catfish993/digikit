#!/usr/bin/env python3
"""The `fxroutes` kind of the digikit scenarios: SETTINGS > DIGIKIT > FX ROUTES and the manifest's route fields (FX routing step 4; docs/findings/65-fx-routes.md).

Run by `tools/patchkit/pk_digikit_scen.py` (scenarios `fx-routes-dt2` and `fx-routes-dn2`). The image is the runtime with `core/dynfx`, `core/fxpages` and `core/digikit-menu`; the sound is the run-time machine
`test/sine`, the effects are `test/dyn-fx-gain` (no route fields: master only, the module before the fields), `test/dyn-fx-gain-send` (`route = "send"`: allowed send and master, default
send) and `test/dyn-fx-gain-sendonly` (`routes = ["send"]`), and the first again under another name. The settings record (`fx/route/<module id>`) is put on the card before a boot,
as the runtime's settings copy A (step 2's record format), where a part needs a stored route. Boots, each in its own daemon:

  dry       the sine alone: the reference PCM.
  none      no effect: the page says NO EFFECTS LOADED (screenshot).
  list      a master-only and a send-default effect, no stored route: the send one starts as a send effect (route word, FX key page, CAPACITY); Function + FX lists the send effect before the master one (the
            list the runtime hands the firmware, and the screenshots); FX ROUTES lists `GAN  MASTER` and `GSN  SEND`; YES on the master-only one says `GAN: MASTER ONLY`; YES on the other
            switches the pending route and offers APPLY & RESTART, YES again switches back and the offer goes.
  sendonly  a send-only effect: `GSO: SEND ONLY`, the pending route does not change.
  fallback  stored routes that the effect does not allow (send for a master-only one, master for a send-only one): the default holds.
  full      one effect more than the device has send slots (three on the Digitakt II, two on the Digitone II): `NO SEND SLOT` on the last (nothing changes); the pending routes count, so moving a send effect to the master frees the slot.
  apply     a send-default effect: PCM = dry (send, no amounts); FX ROUTES, YES to MASTER, APPLY & RESTART (a popup on the first press, the write and the restart on the second); after the restart the
            route is the master (row word, no send page on the FX key, PCM = dry x gain) and `patchkit store dump` shows the key.
  back      the same effect with a stored `master`: the other way, to SEND, after a restart (row word, FX key page 2, `patchkit store dump`, PCM = dry). A boot of its own: two restarts inside the first
            30 s of a session are two unstable boots in a row, and the third boot would be safe mode.

Screenshots go to the scenario's work folder; `DIGIKIT_SHOTS=DIR` copies them to DIR (`step4-<name>.png`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from tools.coldfire import digikit_menu_proof as D
from tools.device.dksettings import encode as encode_record
from tools.patchkit import pk_device
from tools.patchkit import pk_digikit_project as P
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit.pk_digikit_debug import uploader
from tools.patchkit.pk_digikit_fx import SINE, chain_rows, scaled
from tools.patchkit.pk_digikit_fxpage import Shots, open_send_fx, page_down
from tools.patchkit.pk_digikit_fxpersist import KIT_AREAS
from tools.patchkit.pk_harness import text_rows

W_ROUTE = 18  # row word of the route (core/fx module.toml)
GAIN, SEND, SENDONLY = (
    "test/dyn-fx-gain",
    "test/dyn-fx-gain-send",
    "test/dyn-fx-gain-sendonly",
)
SEND3 = SEND + "-3"
MASTER, SENDR = 1, 2  # the routes as the menu and DkrtFx hold them
# DKRT_FXS (core/runtime fxsend.rs `SendState`): 10 words, then `allow` and `dflt`, one byte per effect row
FXS_ALLOW, FXS_DFLT = 40, 44


def route_key(name: str) -> bytes:
    return b"fx/route/" + name.encode()


def run_fxroutes(c: Any) -> None:
    from tools.patchkit.pk_digikit_scen import RT

    t = c.table()
    hint = t.get("hint", 7)
    trig = t.get("trig_frame", M.TRIG_FRAME)
    slots = pk_device.limits(c.device).fx_slots
    send_slots = pk_device.limits(c.device).send_slots
    st = c.store
    img = c.image_for(RT + ",core/dynfx,core/fxpages,core/digikit-menu", "fxroutes")
    sine = c.pack(SINE, "m0.dkm")
    gain = c.pack(GAIN, "m1.dkm")
    snd = c.pack(SEND, "m2.dkm")
    so = c.pack(SENDONLY, "m3.dkm")
    out = c.env.folder / "digikit" / "modules" / "m4.dkm"
    c.env.tools.patchkit_run(
        "pack", SEND, "--device", c.device, "--name", SEND3, "-o", out
    )
    snd3 = "modules/m4.dkm"
    pcm: dict[str, np.ndarray] = {}

    def seeded(entries: list[tuple[bytes, bytes]], mods: list[str]) -> Path:
        """A card with MODS and the settings record (copy A, seq 1) holding ENTRIES."""
        rec = encode_record(1, entries)
        pad = rec.ljust(((len(rec) + 511) // 512) * 512, b"\0")
        return c.card(mods, foreign={st.set_a: pad})

    card_dry = c.card([sine])
    card_none = c.card([])
    card_list = c.card([sine, gain, snd])
    card_so = c.card([so, gain])
    card_fb = seeded(
        [(route_key(GAIN), b"\x01"), (route_key(SENDONLY), b"\x00")], [gain, so]
    )
    # the effects of the `full` boot: one more than the device has send slots (the second slot of the Digitakt II goes to the send-only effect)
    full_mods = [snd, so, snd3] if send_slots > 1 else [snd, snd3]
    full_routes = [1] * send_slots + [0]
    full_rows = (
        ["GSN  SEND", "GSO  SEND", "GSN  MASTER"]
        if send_slots > 1
        else ["GSN  SEND", "GSN  MASTER"]
    )
    full_pending = [SENDR] * send_slots + [MASTER]
    card_full = seeded([(route_key(SEND3), b"\x00")], full_mods)
    card_apply = c.card([sine, snd])
    card_back = seeded([(route_key(SEND), b"\x00")], [sine, snd])

    def tap(rig: Any, keys: Any, ms: int = 300) -> None:
        rig.s.do_tap(keys, 20)
        rig.s.run_ms(ms)

    def sym(rig: Any, owner: str, name: str) -> int:
        return M.sym(rig.plan, owner, name)

    def fxr_rows(rig: Any) -> list[str]:
        """The rows of the FX ROUTES page as the menu kept them."""
        base = sym(rig, "core/digikit-menu", "DK_FXR")
        n = rig.w(sym(rig, "core/digikit-menu", "DK_FXR_N"))
        return text_rows(rig.w, base, n, 24)

    def pending(rig: Any) -> list[int]:
        w = rig.w(sym(rig, "core/digikit-menu", "DK_FXR_PEND"))
        return [(w >> (24 - 8 * k)) & 0xFF for k in range(4)]

    def popup_text(rig: Any) -> str:
        return text_rows(rig.w, sym(rig, "core/digikit-menu", "DK_POPUP"), 1, 48)[0]

    def to_fxr(rig: Any) -> None:
        """SETTINGS > DIGIKIT (the ninth row) > FX ROUTES (the row after MODULES)."""
        tap(rig, "NO", 400)
        tap(rig, "SETTINGS", 600)
        for _ in range(8):
            tap(rig, "DOWN", 200)
        tap(rig, "YES", 700)
        tap(rig, "DOWN", 300)
        tap(rig, "YES", 800)

    def leave_menu(rig: Any) -> None:
        for _ in range(3):
            tap(rig, "NO", 400)

    def routes(rig: Any, n: int) -> list[int]:
        return [r[W_ROUTE] for r in chain_rows(rig, slots)[:n]]

    def count_cell(rig: Any) -> int:
        return rig.w(sym(rig, "core/fxpages", "fxp_send_count"))

    def row_flags(rig: Any, n: int) -> tuple[list[int], list[int]]:
        """The routes each effect row allows and its default, as the runtime kept them from the records (1 master, 2 send)."""
        fxs = sym(rig, "core/runtime", "DKRT_FXS")
        a, d = rig.w(fxs + FXS_ALLOW), rig.w(fxs + FXS_DFLT)
        return (
            [(a >> (24 - 8 * k)) & 0xFF for k in range(n)],
            [(d >> (24 - 8 * k)) & 0xFF for k in range(n)],
        )

    def send_used(rig: Any) -> dict[str, int]:
        return uploader(rig).capacity()["SEND_SLOTS"]

    def sine_pcm(rig: Any, wav: Path) -> np.ndarray:
        rig.pick(rig.dyn_type(SINE, "sine"), hint)
        tap(rig, "NO", 300)
        return rig.note(wav, max(trig, rig.frames() + 300))

    def fx_page_order(rig: Any) -> list[int]:
        """The page numbers of the FX view's list (core/fxpages `fxpages_info`: list address, count cell), the stock pages first."""
        inf = sym(rig, "core/fxpages", "fxpages_info")
        first, lst = rig.w(inf + 4 * 5), rig.w(inf + 4 * 7)
        n = rig.w(rig.w(inf + 4 * 9))  # word 9 is the address of the count cell
        return [rig.w(lst + 4 * i) - first for i in range(n)]

    # ---- dry, none ----
    def part_dry() -> None:
        with c.rig(img, card_dry, "dry") as rig:
            rig.boot_info()
            pcm["dry"] = sine_pcm(rig, c.work / "dry.wav")
            c.done(rig)

    def part_none() -> None:
        with c.rig(img, card_none, "none") as rig:
            rig.boot_info()
            shots = Shots(c.work, "step4")
            to_fxr(rig)
            shots.take(rig, "none")
            c.check(
                "none: with no effect loaded the page says NO EFFECTS LOADED",
                fxr_rows(rig) == ["NO EFFECTS LOADED"],
                f"{fxr_rows(rig)}",
            )
            tap(rig, "YES", 400)
            c.check(
                "none: YES on that row changes nothing",
                fxr_rows(rig) == ["NO EFFECTS LOADED"],
                f"{fxr_rows(rig)}",
            )
            c.done(rig)

    # ---- list ----
    def part_list() -> None:
        with c.rig(img, card_list, "list") as rig:
            info = rig.boot_info()
            c.check(
                "list: the sine and the two effects loaded",
                info["results"] == [0, 0, 0],
                f"{info['results']}",
            )
            allow, dflt = row_flags(rig, 2)
            c.check(
                "list: the records say what the manifests say: test/dyn-fx-gain (no route fields) allows the master only and defaults to it, test/dyn-fx-gain-send allows both and defaults to send",
                allow == [MASTER, MASTER | SENDR] and dflt == [MASTER, SENDR],
                f"allow {allow} default {dflt}",
            )
            c.check(
                "list: with no stored route the manifest's default holds: the master-only effect is on the master chain (route word 0), the send one starts as a send effect (route word 1)",
                routes(rig, 2) == [0, 1],
                f"{routes(rig, 2)}",
            )
            c.check(
                "list: the FX key's count cell is 2 (the send page exists) and CAPACITY SEND_SLOTS has one used",
                count_cell(rig) == 2
                and send_used(rig) == {"limit": send_slots, "used": 1, "largest": 0},
                f"{count_cell(rig)} {send_used(rig)}",
            )
            order = fx_page_order(rig)
            c.check(
                "list: the list of the FX view is the three stock pages, then the send effect's page (row 1), then the master effect's (row 0)",
                len(order) == 5 and order[3:] == [1, 0],
                f"{order}",
            )
            shots = Shots(c.work, "step4")
            open_send_fx(rig)
            shots.take(rig, "fxview-1")
            for k in range(2, 6):
                page_down(rig)
                shots.take(rig, f"fxview-{k}")
            leave_menu(rig)
            to_fxr(rig)
            shots.take(rig, "list")
            c.check(
                "list: the page has a row for each effect: the tab and the route (GAN MASTER, GSN SEND)",
                fxr_rows(rig) == ["GAN  MASTER", "GSN  SEND"]
                and pending(rig)[:2] == [MASTER, SENDR],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            tap(rig, "YES", 400)  # the master-only effect
            shots.take(rig, "master-only")
            c.check(
                "list: YES on the master-only effect says `GAN: MASTER ONLY` and changes nothing (no APPLY & RESTART)",
                fxr_rows(rig) == ["GAN  MASTER", "GSN  SEND", "GAN: MASTER ONLY"]
                and pending(rig)[:2] == [MASTER, SENDR],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            tap(rig, "DOWN", 300)
            tap(rig, "YES", 400)  # the send effect goes to the master
            shots.take(rig, "switched")
            c.check(
                "list: YES on the send effect switches the pending route to the master (`GSN  MASTER`), clears the message and offers APPLY & RESTART; nothing is saved",
                fxr_rows(rig) == ["GAN  MASTER", "GSN  MASTER", "APPLY & RESTART"]
                and pending(rig)[:2] == [MASTER, MASTER],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            tap(rig, "YES", 400)  # and back
            c.check(
                "list: YES again switches back to send and the offer goes",
                fxr_rows(rig) == ["GAN  MASTER", "GSN  SEND"]
                and pending(rig)[:2] == [MASTER, SENDR],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            c.done(rig)

    # ---- sendonly ----
    def part_sendonly() -> None:
        with c.rig(img, card_so, "sendonly") as rig:
            info = rig.boot_info()
            c.check(
                "sendonly: both effects loaded",
                info["results"] == [0, 0],
                f"{info['results']}",
            )
            allow, dflt = row_flags(rig, 2)
            c.check(
                'sendonly: test/dyn-fx-gain-sendonly allows send only (`routes = ["send"]`)',
                allow == [SENDR, MASTER] and dflt == [SENDR, MASTER],
                f"allow {allow} default {dflt}",
            )
            to_fxr(rig)
            shots = Shots(c.work, "step4")
            c.check(
                "sendonly: the rows are `GSO  SEND`, `GAN  MASTER`",
                fxr_rows(rig) == ["GSO  SEND", "GAN  MASTER"],
                f"{fxr_rows(rig)}",
            )
            tap(rig, "YES", 400)
            shots.take(rig, "send-only")
            c.check(
                "sendonly: YES on the send-only effect says `GSO: SEND ONLY` and changes nothing",
                fxr_rows(rig) == ["GSO  SEND", "GAN  MASTER", "GSO: SEND ONLY"]
                and pending(rig)[:2] == [SENDR, MASTER],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            c.done(rig)

    # ---- fallback ----
    def part_fallback() -> None:
        with c.rig(img, card_fb, "fallback") as rig:
            info = rig.boot_info()
            c.check(
                "fallback: both effects loaded",
                info["results"] == [0, 0],
                f"{info['results']}",
            )
            c.check(
                "fallback: a stored `send` for the master-only effect and a stored `master` for the send-only one are not allowed: the defaults hold (route words 0 and 1)",
                routes(rig, 2) == [0, 1] and send_used(rig)["used"] == 1,
                f"{routes(rig, 2)} {send_used(rig)}",
            )
            to_fxr(rig)
            c.check(
                "fallback: the page shows the routes that hold: `GAN  MASTER`, `GSO  SEND`",
                fxr_rows(rig) == ["GAN  MASTER", "GSO  SEND"],
                f"{fxr_rows(rig)}",
            )
            c.done(rig)

    # ---- full ----
    def part_full() -> None:
        with c.rig(img, card_full, "full") as rig:
            n = len(full_mods)
            info = rig.boot_info()
            c.check(
                f"full: the {n} effects loaded",
                info["results"] == [0] * n,
                f"{info['results']}",
            )
            c.check(
                f"full: {send_slots} effect{'s' if send_slots > 1 else ''} run on the send route and the stored `master` holds for the last (route words {full_routes}); CAPACITY SEND_SLOTS is {send_slots} of {send_slots}",
                routes(rig, n) == full_routes
                and send_used(rig)
                == {"limit": send_slots, "used": send_slots, "largest": 0},
                f"{routes(rig, n)} {send_used(rig)}",
            )
            to_fxr(rig)
            shots = Shots(c.work, "step4")
            c.check(
                f"full: the rows are {', '.join(f'`{x}`' for x in full_rows)}",
                fxr_rows(rig) == full_rows,
                f"{fxr_rows(rig)}",
            )
            for _ in range(n - 1):
                tap(rig, "DOWN", 250)
            tap(rig, "YES", 400)  # the last effect to send: every slot is taken
            shots.take(rig, "no-send-slot")
            c.check(
                "full: YES on the last effect says `NO SEND SLOT` and changes nothing",
                fxr_rows(rig) == [*full_rows, "NO SEND SLOT"]
                and pending(rig)[:n] == full_pending,
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            for _ in range(n - 1):
                tap(rig, "UP", 250)
            tap(rig, "YES", 400)  # the first one to the master: a slot is free
            for _ in range(n - 1):
                tap(rig, "DOWN", 250)
            tap(rig, "YES", 400)  # now the last may take it
            shots.take(rig, "pending-counts")
            after_rows = [
                "GSN  MASTER",
                *(["GSO  SEND"] if send_slots > 1 else []),
                "GSN  SEND",
                "APPLY & RESTART",
            ]
            c.check(
                f"full: the pending routes count: after the first goes to the master the last may go to send ({', '.join(f'`{x}`' for x in after_rows[:-1])}), and APPLY & RESTART is offered",
                fxr_rows(rig) == after_rows
                and pending(rig)[:n] == [MASTER, *full_pending[1:-1], SENDR],
                f"{fxr_rows(rig)} {pending(rig)}",
            )
            c.done(rig)

    # ---- apply ----
    def dump_route(rig: Any, base: Path, tag: str) -> str | None:
        """The value of `fx/route/<SEND>` that `patchkit store dump` shows on the card as the guest left it (None: no such key)."""
        ov = rig.work / f"{tag}.after.bin"
        rig.emu.call("card.overlay", path=str(ov))
        after = P.card_after(c, base, ov)
        text = c.env.tools.patchkit_run(
            "store", "dump", after, "--device", c.device, "--json"
        )
        d = json.loads(text)["settings"]
        cur = d["copies"][0 if d["active"] == "A" else 1]
        for e in cur["entries"]:
            if e["key"] == route_key(SEND).decode():
                return str(e["value"])
        return None

    def apply_flow(rig: Any, shots: Shots, tag: str, want_text: list[str]) -> None:
        """On the FX ROUTES page with one effect: YES switches the route, then APPLY & RESTART with its two presses; waits for the restart."""
        tap(rig, "YES", 400)
        shots.take(rig, f"{tag}-switched")
        c.check(
            f"apply {tag}: YES switched the pending route and offered APPLY & RESTART",
            fxr_rows(rig) == want_text,
            f"{fxr_rows(rig)}",
        )
        tap(rig, "DOWN", 300)
        applies = rig.w(sym(rig, "core/digikit-menu", "DK_FXR_APPLIES"))
        tap(rig, "YES", 700)  # the first press: the popup
        shots.take(rig, f"{tag}-popup")
        c.check(
            f"apply {tag}: the first press shows a popup that the device restarts and writes nothing",
            popup_text(rig) == "DEVICE WILL RESTART"
            and rig.w(sym(rig, "core/digikit-menu", "DK_FXR_APPLIES")) == applies,
            f"{popup_text(rig)!r}",
        )
        tap(rig, "NO", 500)
        rig.emu.drain_events()
        tap(rig, "YES", 300)  # the second press: put, flush, restart
        D._wait_restart(rig)

    def part_apply() -> None:
        with c.rig(img, card_apply, "apply") as rig:
            info = rig.boot_info()
            c.check(
                "apply: the sine and the send-default effect loaded",
                info["results"] == [0, 0],
                f"{info['results']}",
            )
            c.check(
                "apply: it starts as a send effect: route word 1, the send page's count cell 2, one send slot used",
                routes(rig, 1) == [1]
                and count_cell(rig) == 2
                and send_used(rig)["used"] == 1,
                f"{routes(rig, 1)} {count_cell(rig)} {send_used(rig)}",
            )
            pcm["send0"] = sine_pcm(rig, c.work / "send0.wav")
            shots = Shots(c.work, "step4")
            tap(rig, "FX", 400)
            fx_a = shots.take(rig, "apply-fx1-send")
            tap(rig, "FX", 400)
            fx_b = shots.take(rig, "apply-fx2-send")
            c.check(
                "apply: the FX key has a second page while the effect is routed send",
                fx_a != fx_b,
                f"{fx_a[:8]} {fx_b[:8]}",
            )
            to_fxr(rig)
            shots.take(rig, "apply-start")
            c.check(
                "apply: the page shows `GSN  SEND`",
                fxr_rows(rig) == ["GSN  SEND"],
                f"{fxr_rows(rig)}",
            )
            apply_flow(rig, shots, "to-master", ["GSN  MASTER", "APPLY & RESTART"])
            info = rig.boot_info()
            c.check(
                "apply: after the restart the module loaded again",
                info["results"] == [0, 0] and info["safe"] == 0,
                f"{info['results']}",
            )
            c.check(
                "apply: the route is the master now: route word 0, the count cell 0 (no send page), no send slot used",
                routes(rig, 1) == [0]
                and count_cell(rig) == 0
                and send_used(rig)["used"] == 0,
                f"{routes(rig, 1)} {count_cell(rig)} {send_used(rig)}",
            )
            c.check(
                "apply: `patchkit store dump` on the card as the guest left it shows `fx/route/test/dyn-fx-gain-send` = master",
                dump_route(rig, card_apply, "r1") == "master",
                f"{dump_route(rig, card_apply, 'r1')}",
            )
            pcm["master"] = sine_pcm(rig, c.work / "master.wav")
            tap(rig, "FX", 400)
            shots.take(rig, "apply-fx1-master")
            tap(rig, "FX", 400)
            shots.take(rig, "apply-fx1-master-again")
            to_fxr(rig)
            shots.take(rig, "apply-master")
            c.check(
                "apply: the page shows the saved route (`GSN  MASTER`) and offers nothing",
                fxr_rows(rig) == ["GSN  MASTER"],
                f"{fxr_rows(rig)}",
            )
            c.done(rig)

    def part_back() -> None:
        """The other way, on its own boot: a second restart within 30 s of the session would be the second unstable boot in a row, and safe mode."""
        with c.rig(img, card_back, "back") as rig:
            info = rig.boot_info()
            c.check(
                "back: the stored `master` holds: route word 0, no send page, no send slot used",
                info["results"] == [0, 0]
                and routes(rig, 1) == [0]
                and count_cell(rig) == 0
                and send_used(rig)["used"] == 0,
                f"{info['results']} {routes(rig, 1)} {count_cell(rig)} {send_used(rig)}",
            )
            shots = Shots(c.work, "step4")
            to_fxr(rig)
            c.check(
                "back: the page shows `GSN  MASTER`",
                fxr_rows(rig) == ["GSN  MASTER"],
                f"{fxr_rows(rig)}",
            )
            apply_flow(rig, shots, "to-send", ["GSN  SEND", "APPLY & RESTART"])
            info = rig.boot_info()
            c.check(
                "back: after the restart the module loaded again",
                info["results"] == [0, 0] and info["safe"] == 0,
                f"{info['results']}",
            )
            c.check(
                "back: the route is send now: route word 1, the count cell 2 (the send page), one send slot used",
                routes(rig, 1) == [1]
                and count_cell(rig) == 2
                and send_used(rig)["used"] == 1,
                f"{routes(rig, 1)} {count_cell(rig)} {send_used(rig)}",
            )
            c.check(
                "back: `patchkit store dump` shows the key as send (written to copy B, seq 2)",
                dump_route(rig, card_back, "r2") == "send",
                f"{dump_route(rig, card_back, 'r2')}",
            )
            pcm["send1"] = sine_pcm(rig, c.work / "send1.wav")
            tap(rig, "FX", 400)
            shots.take(rig, "back-fx1-send")
            tap(rig, "FX", 400)
            shots.take(rig, "back-fx2-send")
            c.done(rig)

    parts = {
        "dry": part_dry,
        "none": part_none,
        "list": part_list,
        "sendonly": part_sendonly,
        "fallback": part_fallback,
        "full": part_full,
        "apply": part_apply,
        "back": part_back,
    }
    only = t.get("only")
    c.parallel(*[f for n, f in parts.items() if not only or n in only])

    def across() -> None:
        r = scaled(pcm["dry"], pcm["send0"], 1.0)
        c.check(
            "apply: on the send route with no amount the PCM is the dry PCM, bit for bit from the onset on",
            bool(r["identical"]) and abs(r["shift"]) <= 128,
            f"onset shift {r['shift']}, max err {r.get('max_abs_err')}",
        )
        r = scaled(pcm["dry"], pcm["master"], 0.5)
        c.check(
            "apply: after the restart on the master route the PCM is the dry PCM times the effect's gain 0.5 (the effect is in the master chain), within 2 ulps",
            r["rms_db"] is not None
            and r["max_err_ulps"] <= 2.0
            and abs(r["rms_db"] - 20 * np.log10(0.5)) < 0.001
            and abs(r["shift"]) <= 128,
            f"rms {r['rms_db']}, max err {r['max_err_ulps']:.2f} ulps, shift {r['shift']}",
        )
        r = scaled(pcm["dry"], pcm["send1"], 1.0)
        c.check(
            "back: after the restart on the send route again (no amount) the PCM is the dry PCM again",
            bool(r["identical"]) and abs(r["shift"]) <= 128,
            f"onset shift {r['shift']}, max err {r.get('max_abs_err')}",
        )

    if not only or {"dry", "apply", "back"} <= set(only):
        across()
    c.common_checks(KIT_AREAS[c.device], store_extra=frozenset({st.set_a, st.set_b}))
