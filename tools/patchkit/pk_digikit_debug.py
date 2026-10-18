#!/usr/bin/env python3
"""The `card`, `debug` and `crash` scenarios of the digikit runtime (docs/findings/46-digikit-debug.md). Run by `tools/patchkit/pk_digikit_scen.py`.

card   card safety. A blank card works as before; the one card-write function refuses every write outside the store range of the device file
       and counts the refusals; a card with foreign bytes in the range goes read-only (nothing written, no selection, no upload, a line on
       ABOUT, a log event with the hash and the first non-blank sector); a card with our store keeps working; the claim of a blank range
       before the first upload write leaves a card that the next boot calls ours when the upload is cut.
debug  the debug log: LOG and PEEK over SysEx, INFO's sequence numbers, the DEBUG switch (errors only when off, saved in the record's flags
       and kept by the selection), the periodic events, `tools/device/dklog.py` against the emulator.
crash  a CPU exception in a module's slot: the runtime's hook records it in RAM that the soft reset keeps and resets; the next boot reports
       the record with the right PC; two in a row lead to safe mode, for a crash before the stable period and for one after it.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.device import dklog as L
from tools.device import dkupload as DK
from tools.patchkit import pk_device
from tools.patchkit import pk_digikit_upload as U
from tools.patchkit import pk_dkrt_boot as B
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit import pk_harness as H

E_INUSE, E_PEEK = -62, -63
FIRST, SECTORS = B.STORE_FIRST, B.STORE_SECTORS
END = FIRST + SECTORS
HEADER_B = B.HEADER_B  # absolute sector of store header copy B


def uploader(rig: Any) -> DK.Uploader:
    return DK.Uploader(U.transport(rig))


def events(
    rig: Any, kind: str | None = None
) -> list[tuple[int, int, int, int, tuple[int, ...]]]:
    """Every event the device's log holds, read over SysEx (the way dklog reads it)."""
    evs = L.read_all(uploader(rig))[2]
    return [e for e in evs if kind is None or e[2] == L.K[kind]]


def about_rows(rig: Any, plan: Any) -> list[str]:
    """The rows of the ABOUT page as the menu last built them (`DK_ABOUT`)."""
    base, n = plan.addr("DK_ABOUT"), rig.w(plan.addr("DK_ABOUT_N"))
    rows = []
    for i in range(n):
        raw = b"".join(
            rig.w(base + 24 * i + 4 * k).to_bytes(4, "big") for k in range(6)
        )
        rows.append(raw.split(b"\0")[0].decode("latin-1"))
    return rows


# ---- card -------------------------------------------------------------------------------------------------------------------


def run_card(c: Any) -> None:
    from tools.coldfire import digikit_menu_proof as D
    from tools.patchkit.pk_digikit_project import card_after

    img = c.image_for(B.RT, "run")
    menu_img = c.image_for(f"{B.RT},core/digikit-menu", "menu")
    pat = bytes(
        (i * 7 + 3) & 0xFF for i in range(512)
    )  # a sector that is neither all 0x00 nor all 0xFF
    # the cards without a store come first: the card folder is shared, and packing a module puts a store in every card built after it
    blank = c.card(None)
    stray = 100  # relative sector of the foreign bytes
    foreign = c.card(None, foreign={FIRST + stray: pat})
    mod = c.pack("test/c-demo", "a.dkm")
    ours = c.card([mod])
    plan = D.Plan(menu_img.plan)

    def part_blank() -> None:
        with c.rig(img, blank, "blank", cf_only=True) as rig:
            ci = rig.card_info()
            c.check(
                "blank card: the check finds the range blank; nothing refused, claimed or written",
                ci["state_name"] == "blank"
                and ci["first_nonblank"] == 0xFFFFFFFF
                and (ci["refused"], ci["claims"], ci["writes"]) == (0, 0, 0),
                str(ci),
            )
            bad = [
                (FIRST - 1, 512),
                (END, 512),
                (END - 1, 1024),
                (FIRST, 100),
                (0, 512),
                (FIRST, 0),
                (0xFFFFFFFF, 512),
                (FIRST - 1, 1024),
            ]
            res = [rig.probe_write(s, n) for s, n in bad]
            c.check(
                "blank card: the card-write function refuses every write outside the store range (E_RANGE), whatever its size",
                res == [B.E_RANGE] * len(bad),
                str(res),
            )
            ci = rig.card_info()
            c.check(
                "blank card: the refusals are counted and nothing was written",
                ci["refused"] == len(bad)
                and ci["refused_range"] == len(bad)
                and ci["writes"] == 0
                and ci["state_name"] == "blank",
                str(ci),
            )
            r = rig.set_selection([mod])
            recs = rig.sel_records()
            ci = rig.card_info()
            c.check(
                "blank card: a selection is written as before (two record sectors, no store header), and the area is ours from then on",
                r == 0
                and recs[0] is not None
                and recs[0] == recs[1]
                and (ci["state_name"], ci["claims"], ci["writes"]) == ("ours", 0, 2),
                f"{r} {recs} {ci}",
            )
            evs = events(rig)
            refused = [e for e in evs if e[2] == L.K["CARD_REFUSED"]]
            check = [e for e in evs if e[2] == L.K["CARD_CHECK"]]
            c.check(
                "blank card: the log has the card check (blank) and one error event per refused write (reason: outside the range)",
                len(check) == 1
                and check[0][4][0] == 2
                and not check[0][3] & 1
                and len(refused) == len(bad)
                and all(e[3] & 1 and e[4][2] == 1 for e in refused),
                str([(e[2], e[4]) for e in refused[:2]]),
            )
            c.done(rig)

    def part_foreign() -> None:
        with c.rig(menu_img, foreign, "foreign", cf_only=True) as rig:
            ci = rig.card_info()
            want_hash = zlib.crc32(pat)
            c.check(
                "foreign bytes in the range: the check says foreign, names the first non-blank sector and its crc32",
                ci["state_name"] == "foreign"
                and ci["first_nonblank"] == stray
                and ci["hash"] == want_hash,
                f"{ci} want hash {want_hash:08x}",
            )
            b = rig.boot_info()
            c.check(
                "foreign card: the boot runs normally (no module selected, no safe mode, UI loop reached)",
                b["safe"] == 0 and b["count"] == 0 and b["ui_reached"] == 1,
                str(b),
            )
            r_sel = rig.set_selection([mod])
            r_probe = rig.probe_write(FIRST + 5, 512)
            r_range = rig.probe_write(FIRST - 1, 512)
            r_dbg = rig.set_debug(False)
            ci = rig.card_info()
            c.check(
                "foreign card: a selection, a write at a sector in the range and the saving of the debug setting are refused (E_CARD_RO); one outside the range is E_RANGE",
                (r_sel, r_probe, r_dbg, r_range)
                == (B.E_CARD_RO, B.E_CARD_RO, B.E_CARD_RO, B.E_RANGE)
                and ci["refused"] == 3
                and ci["writes"] == 0,
                f"{(r_sel, r_probe, r_dbg, r_range)} {ci}",
            )
            c.check(
                "foreign card: no selection record was written",
                rig.sel_records() == [None, None],
            )
            up = uploader(rig)
            info = up.info()
            c.check(
                "foreign card: INFO says the card is read-only; LIST works (empty)",
                info["card_read_only"] and not info["debug_on"] and up.ls() == [],
                str(info),
            )
            ops = [
                up.request(
                    DK.CMD_BEGIN, b"modules/x.dkm\0" + struct.pack(">I", 10) + bytes(32)
                )[0],
                up.request(DK.CMD_DELETE, b"modules/x.dkm\0")[0],
                up.request(DK.CMD_CLEAR, struct.pack(">I", DK.CLEAR_MAGIC))[0],
            ]
            c.check(
                "foreign card: BEGIN, DELETE and CLEAR answer E_INUSE",
                ops == [E_INUSE] * 3,
                str(ops),
            )
            evs = events(rig)
            chk = [e for e in evs if e[2] == L.K["CARD_CHECK"]]
            c.check(
                "foreign card: the log reports what the check found (error event: state foreign, the sector, the hash)",
                len(chk) == 1
                and chk[0][3] & 1
                and chk[0][4][:3] == (3, stray, want_hash),
                str(chk),
            )

            # ABOUT: a short line
            def tap(key: str, gap_ms: int = 350) -> None:
                rig.s.do_tap(key)
                rig.s.run_ms(gap_ms)

            rig.s.run_ms(
                3000
            )  # a daemon without DSP is handed out before the first screen has settled
            tap("NO", 400)
            tap("SETTINGS", 600)
            for _ in range(8):
                tap("DOWN", 200)
            tap("YES", 700)  # DIGIKIT
            tap("DOWN", 300)  # ABOUT
            tap("YES", 800)
            rows = about_rows(rig, plan)
            png = c.work / "about-foreign.png"
            png.write_bytes(rig.emu.frame_png())
            c.report["about_foreign_png"] = str(png)
            c.check(
                "foreign card: ABOUT shows CARD READ-ONLY, where the foreign data starts, and that the boot counter is off; every row fits 21 characters",
                "CARD READ-ONLY" in rows
                and f"NOT OURS AT {stray}" in rows
                and "NO BOOT COUNTER" in rows
                and max(len(x) for x in rows) <= 21,
                str(rows),
            )
            c.check(
                "foreign card: the runtime wrote nothing in the store range",
                rig.card_store_writes() == [],
                str(rig.card_store_writes()),
            )
            c.done(rig)

    def part_ours() -> None:
        with c.rig(img, ours, "ours", cf_only=True) as rig:
            ci = rig.card_info()
            c.check(
                "our store on the card: the check says ours without reading the range (no non-blank sector found, no hash)",
                ci["state_name"] == "ours"
                and ci["first_nonblank"] == 0xFFFFFFFF
                and ci["hash"] == 0
                and ci["refused"] == 0,
                str(ci),
            )
            b = rig.boot_info()
            c.check(
                "our store: the selection loads as before",
                b["count"] == 1 and b["loaded"] == 1 and b["safe"] == 0,
                str(b),
            )
            r = rig.set_selection([mod, mod])
            up = uploader(rig)
            data = U.blob(70_000, 5)
            st = U.put_status(up, "modules/b.dkm", data)
            files = U.device_ls(up)
            ci = rig.card_info()
            c.check(
                "our store: a selection and an upload still work; the range check refused nothing of theirs",
                r == 0
                and st == 0
                and "modules/b.dkm" in files
                and ci["refused"] == 0
                and ci["claims"] == 0,
                f"{r} {st} {sorted(files)} {ci}",
            )
            c.done(rig)

    def part_claim() -> None:
        # an upload on a blank card claims the range before it stages data; a power cut in the middle leaves a card the next boot calls ours
        base = blank
        with c.rig(img, base, "claim", cf_only=True) as rig:
            rig.emu.poke32(M.sym(rig.plan, "core/runtime", "DKRT_UPLOAD_CUT"), 1)
            up = uploader(rig)
            data = U.blob(70_000, 9)  # three DATA chunks: the second write is refused
            st = U.put_status(up, "modules/c.dkm", data)
            ci = rig.card_info()
            c.check(
                "claim: the first data write of an upload on a blank card claimed the range (empty store headers, both copies), and the cut upload failed",
                ci["claims"] == 1 and ci["state_name"] == "ours" and st != 0,
                f"{st} {ci}",
            )
            ov = rig.work / "claim.ov.bin"
            rig.emu.call("card.overlay", path=str(ov))
            nxt = card_after(c, base, ov)
            c.done(rig)
        with c.rig(img, nxt, "claim2", cf_only=True) as rig:
            ci = rig.card_info()
            up = uploader(rig)
            ls = up.ls()
            st = U.put_status(up, "modules/c.dkm", U.blob(5_000, 3))
            c.check(
                "claim: after the cut upload the next boot calls the area ours (not foreign), the store is empty, and an upload works",
                ci["state_name"] == "ours" and ls == [] and st == 0,
                f"{ci} {ls} {st}",
            )
            c.done(rig)

    c.parallel(part_blank, part_foreign, part_ours, part_claim)
    c.common_checks_upload(project_writes_ok=True)


# ---- debug ------------------------------------------------------------------------------------------------------------------


def run_debug(c: Any) -> None:
    if c.table().get("part") == "hang":
        return run_hang(c)
    img = c.image_for(B.RT, "run")
    blank = c.card(None)
    has_diag = any(k.endswith(":dl_diag") for k in img.plan["symbols"])
    with c.rig(img, blank, "debug", cf_only=True) as rig:
        up = uploader(rig)
        evs = events(rig)
        oldest, newest = evs[0][0], evs[-1][0]
        by_kind = {e[2] for e in evs}
        c.check(
            "LOG returns the boot's events in sequence, from 1, with the block counter as time (not going back)",
            [e[0] for e in evs] == list(range(oldest, newest + 1))
            and oldest == 1
            and all(a[1] <= b[1] for a, b in zip(evs, evs[1:], strict=False)),
            f"{len(evs)} events, kinds {sorted(by_kind)}",
        )
        # This boot runs without the DSP: with dl_diag present the first DSP_DIAG needs a running DSP (the coupled part below checks it);
        # without it, DSP_ABSENT is logged at the first poll.
        want = {"BOOT_START", "CARD_CHECK", "RAM_KEEP", "BOOT_RECORD", "UI_FIRST"} | (
            set() if has_diag else {"DSP_ABSENT"}
        )
        c.check(
            "the boot steps are logged: boot start, card check, RAM probe, record, first UI pass, and DSP_ABSENT when the link is absent",
            all(L.K[k] in by_kind for k in want),
            str(sorted(L.KINDS[k][0] for k in by_kind)),
        )
        start = next(e for e in evs if e[2] == L.K["BOOT_START"])
        c.check(
            "BOOT_START carries the runtime version and the device id",
            start[4][0] == B.RUNTIME_VERSION and start[4][1] in (1, 2),
            str(start),
        )
        info = up.info()
        c.check(
            "INFO reports the oldest and newest sequence of the log, debug on, no crash",
            info["log_oldest"] == 1
            and info["log_newest"] >= newest
            and info["debug_on"]
            and not info["crash_found"]
            and not info["card_read_only"],
            str(info),
        )
        # PEEK
        state_sym = M.sym(rig.plan, "core/runtime", "DKRT_STATE")
        got = up.peek(state_sym, 8)
        c.check(
            "PEEK reads the runtime's own region (DKRT_STATE: magic 'DKRT', runtime version)",
            got[:4] == b"DKRT" and int.from_bytes(got[4:8], "big") == B.RUNTIME_VERSION,
            got.hex(),
        )
        big = L.peek_range(up, state_sym, 1300)
        c.check(
            "PEEK of 1300 bytes in three requests (512 at most each)",
            len(big) == 1300 and big[:4] == b"DKRT",
            f"{len(big)}",
        )
        refused = {}
        rs = pk_device.peek_ranges(c.device)
        (lo0, hi0), (_lo1, hi1) = rs[0], rs[-1]
        fw = up.peek(lo0 + 0x400, 16)
        c.check("PEEK reads the firmware's data range", len(fw) == 16, fw.hex())
        for name, (addr, n) in {
            "a peripheral register (the reset controller)": (0xEC090000, 4),
            "just below the first range": (lo0 - 4, 8),
            "a range that runs past the end of the last": (hi1 - 4, 8),
            "the gap between the ranges": (hi0, 16),
            "address wrap": (0xFFFFFFF0, 32),
        }.items():
            st, _ = up.request(DK.CMD_PEEK, struct.pack(">IH", addr, n))
            refused[name] = st
        st0, _ = up.request(DK.CMD_PEEK, struct.pack(">IH", state_sym, 0))
        st513, _ = up.request(DK.CMD_PEEK, struct.pack(">IH", state_sym, 513))
        c.check(
            "PEEK refuses everything outside the ranges of the device file, and lengths 0 and over 512 (E_PEEK)",
            all(v == E_PEEK for v in refused.values())
            and st0 == E_PEEK
            and st513 == E_PEEK,
            f"{refused} {st0} {st513}",
        )
        # debug off: errors only
        n0 = len(events(rig, "UPLOAD"))
        up.request(DK.CMD_ABORT)
        n1 = len(events(rig, "UPLOAD"))
        c.check("debug ON: an upload command is logged", n1 == n0 + 1, f"{n0} {n1}")
        r = rig.set_debug(False)
        info = up.info()
        rec = B.current(rig.sel_records())
        c.check(
            "DEBUG OFF: saved in the record's flags (bit 1), INFO says off",
            r == 0 and not info["debug_on"] and rec is not None and rec["flags"] & 2,
            f"{r} {rec} {info}",
        )
        up.request(DK.CMD_ABORT)
        bad = up.request(DK.CMD_DELETE, b"..\0")[0]
        n2 = events(rig, "UPLOAD")
        c.check(
            "DEBUG OFF: the log keeps only errors (the abort is not logged, the refused path is)",
            len(n2) == n1 + 1 and n2[-1][3] & 1 and bad != 0,
            f"{len(n2) - n1} new, status {bad}",
        )
        dbg = events(rig, "DEBUG_SET")
        c.check(
            "the switch itself is logged (an error event, so it is kept when turned off)",
            len(dbg) == 1 and dbg[0][4][0] == 0 and dbg[0][3] & 1,
            str(dbg),
        )
        r2 = rig.set_selection([])
        rec = B.current(rig.sel_records())
        c.check(
            "a selection saved afterwards keeps the DEBUG setting",
            r2 == 0 and rec is not None and rec["flags"] & 2,
            str(rec),
        )
        r3 = rig.set_debug(True)
        rec = B.current(rig.sel_records())
        up.request(DK.CMD_ABORT)
        c.check(
            "DEBUG ON again: saved, and logging resumes",
            r3 == 0
            and rec is not None
            and not rec["flags"] & 2
            and len(events(rig, "UPLOAD")) == len(n2) + 1,
            f"{r3} {rec}",
        )
        # dklog against the emulator
        out = c.work / "dklog.log"
        n = L.follow(up, out, once=True, echo=False)
        text = out.read_text().splitlines()
        js = [json.loads(x) for x in text if not x.startswith("#")]
        c.check(
            "dklog follow --once writes every event as a readable line and a JSON line",
            n == len(js)
            and n >= 8
            and [j["seq"] for j in js] == sorted(j["seq"] for j in js)
            and any(x.startswith("#") and "CARD_CHECK" in x for x in text),
            f"{n} events, {len(text)} lines",
        )
        c.check(
            "dklog crash: no crash record in this log",
            L.crash_record(L.read_all(up)[2]) is None,
        )
        # the command line, as Em runs it: a second connection to the daemon (`--run-dir`)
        run_dir = H.OUT / f"run-{rig.s.run_name}"
        cli = c.work / "dklog-cli.log"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc_follow = L.main(
                [
                    "--emu",
                    "--run-dir",
                    str(run_dir),
                    "follow",
                    "--once",
                    "--out",
                    str(cli),
                ]
            )
            rc_peek = L.main(
                ["--emu", "--run-dir", str(run_dir), "peek", hex(state_sym), "16"]
            )
            rc_crash = L.main(["--emu", "--run-dir", str(run_dir), "crash"])
        c.check(
            "dklog on the command line (follow --once, peek, crash) works against the daemon",
            (rc_follow, rc_peek, rc_crash) == (0, 0, 0)
            and "BOOT_START" in out.getvalue()
            and "DKRT" in out.getvalue()
            and "no crash record" in out.getvalue()
            and cli.read_text().count("\n{") >= 6,
            out.getvalue()[-300:],
        )
        c.done(rig)
    if c.tier == "full":
        periodic(c, img, blank, has_diag)
    c.common_checks_upload(project_writes_ok=True)


HANG_TYPE, HANG_HINT = (
    19,
    7,
)  # its permanent run-time type (modules/core/machines/types.toml); seven steps down the machine list
CRUMB_CALLED = 4  # core/dsp-voice's stage "routine about to be called"


def run_hang(c: Any) -> None:
    """The SHARC stops inside a run-time machine's routine (test/dyn-dsp-hang): the debug log names the step, the voice and the machine type
    (docs/findings/48-dsp-breadcrumbs.md). The routine loops forever on its third block of a note; the reply records the ColdFire keeps
    receiving carry the last breadcrumb that core/dsp-voice wrote straight into them. Checks: the stop event, the full diagnostic set after it,
    the module list, the ABOUT page, and that the log of a running DSP stays quiet."""
    from tools.coldfire import digikit_menu_proof as D

    mod = c.pack("test/dyn-dsp-hang", "hang.dkm")
    menu_img = c.image_for(f"{B.RT},core/digikit-menu", "menu")
    plan = D.Plan(menu_img.plan)
    card = c.card([mod])
    with c.rig(menu_img, card, "hang") as rig:
        info = rig.boot_info()
        c.check(
            "the hang module loaded at boot (CF part, DSP part, voice row)",
            info["count"] == 1
            and info["results"] == [0]
            and len([r for r in rig.voice_rows() if r[0] != B.FREE_TYPE]) == 1,
            str(info["results"]),
        )
        rig.select_type(HANG_TYPE, HANG_HINT)
        rig.s.run_ms(3000)
        before = events(rig)
        c.check(
            "before the note: the DSP runs, the log has its first DSP_DIAG set (17 words in five events) and no DSP_STOPPED",
            len([e for e in before if e[2] == L.K["DSP_DIAG"]]) == 5
            and not [e for e in before if e[2] == L.K["DSP_STOPPED"]],
            f"{len(before)} events",
        )
        first = [e for e in before if e[2] == L.K["DSP_DIAG"]]
        words = [
            w for e in sorted(first, key=lambda e: e[4][0] & 0xFF) for w in e[4][1:]
        ][:17]
        c.check(
            "the first set is the layout of the README: header 0xD1A6 and 17 words, beats rising, context registers, the breadcrumb of the last voice's hook entry, no dispatch yet",
            len(words) == 17
            and words[0] == 0xD1A6 | 17 << 16
            and words[1] > 0
            and words[2] > 0
            and words[3:9] == [0, 1, 0xFFFFFFFF, 0, 1, 0xFFFFFFFF]
            and words[14] == 0x1F01
            and words[15:17] == [0, 0],
            " ".join(f"{w:08x}" for w in words),
        )
        n_diag0 = len([e for e in before if e[2] == L.K["DSP_DIAG"]])
        # the key down, held: the voice is active and the routine loops on its third block
        # (a stopped core burns the DSP's whole period every frame, so the emulator runs this part slowly: look every emulated second)
        rig.emu.button(H.button(M.TRIG1), True)
        t0 = time.monotonic()
        for sec in range(14):
            rig.s.run_ms(1000)
            print(
                f"hang: {sec + 1} s emulated, {time.monotonic() - t0:.0f} s wall",
                flush=True,
            )
            if events(rig, "DSP_STOPPED"):
                break
        rig.s.run_ms(700)  # the samples of the two reply records after the stop
        rig.emu.button(H.button(M.TRIG1), False)
        evs = events(rig)
        stop = [e for e in evs if e[2] == L.K["DSP_STOPPED"]]
        c.check(
            "one DSP_STOPPED error event, not a resume",
            len(stop) == 1 and stop[0][3] & 1 and stop[0][4][4] == 0,
            str(stop),
        )
        crumb = stop[0][4][2] if stop else 0
        want = CRUMB_CALLED | 0 << 8 | HANG_TYPE << 16
        c.check(
            "the stop event's breadcrumb: ROUTINE_CALLED, voice 0, machine type 19 (the track with the hang machine)",
            crumb == want,
            f"{crumb:#x} {L.crumb_text(crumb)}; want {want:#x}",
        )
        c.check(
            "the dispatch count is in the stop event and the voice hook's beat froze below the link hook's",
            stop and stop[0][4][3] > 0 and 0 < stop[0][4][1] <= stop[0][4][0],
            str(stop and stop[0][4]),
        )
        t_stop = stop[0][1] if stop else 0
        err_diag = [e for e in evs if e[2] == L.K["DSP_DIAG"] and e[3] & 1]
        sets = {}
        for e in err_diag:
            sets.setdefault(e[0] - (e[4][0] & 0xFF), []).append(e)
        c.check(
            "after the stop the full diagnostic set is logged, all 17 words (five DSP_DIAG error events per distinct reply)",
            len(err_diag) >= 5
            and len(err_diag) % 5 == 0
            and all(
                [e[4][0] & 0xFF for e in g] == [0, 1, 2, 3, 4] for g in sets.values()
            ),
            f"{len(err_diag)} events, {len(sets)} sets",
        )
        stop_set = sorted(err_diag[:5], key=lambda e: e[4][0] & 0xFF)
        sw = [w for e in stop_set for w in e[4][1:]][:17]
        c.check(
            "the full set logged with the stop says one routine is still running: dispatches - returns = 1, the breadcrumb word again, the 17-word header",
            len(sw) == 17
            and sw[0] == 0xD1A6 | 17 << 16
            and sw[14] == crumb
            and sw[15] - sw[16] == 1,
            " ".join(f"{w:08x}" for w in sw),
        )
        c.check(
            "the stop event comes at least 3 s after the last beat and before the end of the run",
            stop and t_stop > 0 and t_stop - stop[0][4][0] >= 0,
            f"block {t_stop}, heartbeat {stop and stop[0][4][0]}",
        )
        mods = [e for e in evs if e[2] == L.K["MOD_LIST"]]
        c.check(
            "MOD_LIST names the loaded module after the stop (error event)",
            len(mods) == 1
            and mods[0][3] & 1
            and "dyn-dsp" in L.ascii_of(*mods[0][4][2:5]),
            str(mods),
        )
        c.check(
            "the quiet log: the running DSP logged one set before the note, none changed after it except the stop's own",
            n_diag0 == 5
            and len([e for e in evs if e[2] == L.K["DSP_DIAG"] and not e[3] & 1]) <= 10,
            f"{n_diag0} before, {len([e for e in evs if e[2] == L.K['DSP_DIAG'] and not e[3] & 1])} non-error in all",
        )

        def tap(key: str, gap_ms: int = 350) -> None:
            rig.s.do_tap(key)
            rig.s.run_ms(gap_ms)

        tap("NO", 300)
        tap("SETTINGS", 400)
        for _ in range(8):
            tap("DOWN", 120)
        tap("YES", 400)  # DIGIKIT
        tap("DOWN", 200)  # ABOUT
        tap("YES", 500)
        rows = about_rows(rig, plan)
        png = c.work / "about-stopped.png"
        png.write_bytes(rig.emu.frame_png())
        c.report["about_stopped_png"] = str(png)
        c.check(
            "ABOUT shows DSP STOPPED and the step: LAST: CALL V0 T19",
            "DSP STOPPED" in rows and "LAST: CALL V0 T19" in rows,
            str(rows),
        )
        text = c.work / "hang.log"
        L.follow(uploader(rig), text, once=True, echo=False)
        c.report["hang_log"] = str(text)
        c.done(rig)
    c.common_checks_upload(project_writes_ok=True)


def periodic(c: Any, img: Any, card: Path, has_diag: bool) -> None:
    """The periodic events need the firmware's block counter, which only a coupled daemon (with the DSP) advances: a health snapshot every ten
    seconds, the stable-period event after 30, and the DSP poll's events."""
    with c.rig(img, card, "periodic") as rig:
        start = rig.frames()
        rig.s.run_ms(32_000)
        health = events(rig, "HEALTH")
        stable = events(rig, "STABLE")
        diag = events(rig, "DSP_DIAG")
        c.check(
            "a HEALTH snapshot every ten seconds (15,000 blocks)",
            len(health) >= 3
            and all(
                b[1] - a[1] in range(14_000, 16_500)
                for a, b in zip(health, health[1:], strict=False)
            ),
            f"{len(health)} snapshots at {[h[1] for h in health]}",
        )
        c.check(
            "the stable period (30 s) is logged once",
            len(stable) == 1,
            f"{len(stable)} events, {rig.frames() - start} frames run",
        )
        if has_diag:
            starts = [e[1] for e in diag if (e[4][0] & 0xFF) == 0]
            c.check(
                "DSP diagnostics are logged quietly: a full set (17 words, five events) at the first poll, then only every ten seconds while just the beats move",
                len(diag) == 5 * len(starts)
                and 3 <= len(starts) <= 5
                and all(
                    b - a in range(14_000, 16_500)
                    for a, b in zip(starts[1:], starts[2:], strict=False)
                ),
                f"{len(diag)} events in {len(starts)} sets at blocks {starts}",
            )
            stopped = events(rig, "DSP_STOPPED")
            c.check(
                "a running DSP is never reported stopped (the heartbeat, dl_diag word 1, moves)",
                not stopped,
                f"{len(stopped)} DSP_STOPPED events",
            )
        else:
            c.check(
                "without dl_diag in the image the DSP poll logs DSP_ABSENT once and nothing else",
                len(events(rig, "DSP_ABSENT")) == 1 and not diag,
                f"{len(diag)}",
            )
        c.done(rig)


# ---- crash ------------------------------------------------------------------------------------------------------------------


def run_crash(c: Any) -> None:
    t = c.table()
    mod = c.pack(t["crash"], "crash.dkm")
    img = c.image_for(B.RT, "run")
    cfg = c.env.folder / "digikit" / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    cards: dict[str, Path] = {}
    for kind in ("early", "late"):
        (cfg / "crash-after.bin").write_bytes(
            int(t[f"{kind}_blocks"]).to_bytes(4, "big")
            + bytes([int(t[f"{kind}_mode"])])
        )
        cards[kind] = c.card([mod], stable_blocks=int(t["stable_blocks"]))
    want_vec = {0: 4, 1: 5}  # illegal instruction, divide by zero

    def sequence(kind: str) -> Callable[[], None]:
        def run() -> None:
            mode = int(t[f"{kind}_mode"])
            seen: list[dict[str, Any]] = []
            with c.rig(img, cards[kind], f"crash-{kind}") as rig:
                rig.deliver_exceptions(True)
                for boot in range(3):
                    if boot:
                        rig.wait_reset(settle_ms=0)
                    evs = events(rig)
                    pcs = [
                        e[4][4]
                        for e in evs
                        if e[2] == L.K["MOD_LOG"]
                        and L.ascii_of(*e[4][:4]) == "crash-pc"
                    ]
                    rec = L.crash_record(evs)
                    seen.append(
                        {
                            "info": rig.boot_info(),
                            "crash": rig.crash_info(),
                            "card": rig.card_info(),
                            "rec": rec,
                            "evs": evs,
                            "diag_info": uploader(rig).info(),
                            "sel": B.current(rig.sel_records()),
                        }
                    )
                    # the crash-pc the module logged before it crashed, from the record's own tail (the previous boot's events)
                    seen[-1]["tail_pcs"] = (
                        [
                            e[4][4]
                            for e in rec["tail"]
                            if e[2] == L.K["MOD_LOG"]
                            and L.ascii_of(*e[4][:2]) == "crash-pc"
                        ]
                        if rec and not rec.get("corrupt")
                        else []
                    )
                    seen[-1]["pcs"] = pcs
                    # let the boot run on to its crash (not after the last boot: it is safe mode)
                c.done(rig)
            b0, b1, b2 = seen
            c.check(
                f"{kind}: boot 1 sees no crash record, the exception hook is installed, RAM from before is not there (power-on)",
                b0["crash"]["found"] == 0
                and b0["crash"]["installed"] == 1
                and b0["crash"]["ram_kept"] == 0
                and b0["rec"] is None,
                str(b0["crash"]),
            )
            for n, b in ((2, b1), (3, b2)):
                rec = b["rec"]
                tail_pc = (b["tail_pcs"] or [None])[-1]
                c.check(
                    f"{kind}: boot {n} finds the crash record of the boot before, with the module's own PC (vector {want_vec[mode]}, in the module)",
                    rec is not None
                    and not rec.get("corrupt")
                    and b["crash"]["found"] == 1
                    and b["crash"]["pc"] == rec["pc"] == tail_pc
                    and rec["vector"] == want_vec[mode]
                    and rec["module"] is not None
                    and rec["module"]["index"] == 0
                    and bool(rec["module"]["name"])
                    and b["crash"]["ram_kept"] == 1,
                    f"found {b['crash']} rec pc {rec and rec.get('pc')} vec {rec and rec.get('vector')} tail pc {tail_pc} module {rec and rec.get('module')}",
                )
                c.check(
                    f"{kind}: boot {n}: INFO says a crash was found; the crash events decode (registers, stack, events before the crash)",
                    b["diag_info"]["crash_found"]
                    and rec is not None
                    and len(rec["regs"]) == 16
                    and len(rec["stack"]) == 32
                    and len(rec["tail"]) >= 2
                    and rec["a7"] % 4 == 0,
                    str(
                        {
                            k: v
                            for k, v in (rec or {}).items()
                            if k in ("regs", "a7", "sr", "frame_word")
                        }
                    ),
                )
            c.report[f"crash_{kind}_text"] = (
                L.crash_text(b1["rec"]) if b1["rec"] else ""
            )
            if kind == "early":
                c.check(
                    "early: the crash came before the stable period: boot 2 starts with counter 1 and boot 3 with 2; boot 3 is safe mode (counter), no module loaded",
                    [
                        b0["info"]["counter"],
                        b1["info"]["counter"],
                        b2["info"]["counter"],
                    ]
                    == [0, 1, 2]
                    and b1["info"]["safe"] == 0
                    and b1["info"]["loaded"] == 1
                    and b2["info"]["safe"] == 1
                    and b2["info"]["reason"] == 1
                    and b2["info"]["loaded"] == 0
                    and b1["crash"]["stable"] == 0,
                    str([b["info"]["counter"] for b in seen]),
                )
            else:
                c.check(
                    "late: the crash came after the stable period (counter cleared): the crashed boot is counted bad at the next boot; two such crashes in a row make boot 3 safe mode",
                    b0["info"]["counter"] == 0
                    and b1["info"]["counter"] == 1
                    and b1["info"]["safe"] == 0
                    and b1["info"]["loaded"] == 1
                    and b1["crash"]["stable"] == 1
                    and b2["info"]["counter"] == 2
                    and b2["info"]["safe"] == 1
                    and b2["info"]["reason"] == 1
                    and b2["info"]["loaded"] == 0,
                    str(
                        [
                            (
                                b["info"]["counter"],
                                b["info"]["safe"],
                                b["crash"]["stable"],
                            )
                            for b in seen
                        ]
                    ),
                )
            c.check(
                f"{kind}: the safe boot clears the counter at its first UI-loop pass and keeps the selection",
                b2["info"]["ui_reached"] == 1
                and b2["sel"] is not None
                and b2["sel"]["paths"] == [mod],
                str(b2["sel"]),
            )

        return run

    c.parallel(sequence("early"), sequence("late"))
    c.common_checks()
