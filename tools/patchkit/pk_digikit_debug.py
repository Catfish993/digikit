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

E_NOSTORE, E_INUSE, E_PEEK = -61, -62, -63
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
    # a valid selection record and nothing else in the range makes the area ours; with blank header sectors it is an empty store, with foreign
    # bytes in both header sectors it is not (these two come before the first pack, like the cards above)
    selonly = c.card([])
    selforeign = c.card([], foreign={FIRST: pat, FIRST + B.HEADER_B: pat})
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

    def part_selonly() -> None:
        # only a selection record is valid. Blank header sectors: an empty store, an upload works. Foreign bytes in header A and B: the store is
        # somebody's data, BEGIN, DELETE and LIST refuse (E_NOSTORE) and nothing is written to the range.
        with c.rig(img, selonly, "selblank", cf_only=True) as rig:
            ci = rig.card_info()
            up = uploader(rig)
            st = U.put_status(up, "modules/s.dkm", U.blob(3_000, 4))
            c.check(
                "selection record only, blank header sectors: the card is ours, the store is empty and an upload works",
                ci["state_name"] == "ours" and "modules/s.dkm" in U.device_ls(up) and st == 0,
                f"{ci['state_name']} {st}",
            )
            c.done(rig)
        with c.rig(img, selforeign, "selforeign", cf_only=True) as rig:
            ci = rig.card_info()
            up = uploader(rig)
            ops = [
                up.request(
                    DK.CMD_BEGIN, b"modules/x.dkm\0" + struct.pack(">I", 10) + bytes(32)
                )[0],
                up.request(DK.CMD_DELETE, b"modules/x.dkm\0")[0],
                up.request(DK.CMD_LIST, struct.pack(">I", 0))[0],
            ]
            up.request(
                DK.CMD_DATA, struct.pack(">I", 0) + bytes(10)
            )  # no upload is open (BEGIN was refused): E_STATE, nothing staged
            c.check(
                "selection record only, foreign bytes in header A and B: the card counts as ours, but BEGIN, DELETE and LIST answer E_NOSTORE",
                ci["state_name"] == "ours" and ops == [E_NOSTORE] * 3,
                f"{ci['state_name']} {ops}",
            )
            c.check(
                "selection record only, foreign bytes in header A and B: nothing was written to the store range",
                rig.card_store_writes() == [] and rig.card_info()["writes"] == 0,
                f"{rig.card_store_writes()}",
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

    c.parallel(part_blank, part_foreign, part_selonly, part_ours, part_claim)
    c.common_checks_upload(project_writes_ok=True)


# ---- debug ------------------------------------------------------------------------------------------------------------------


def run_debug(c: Any) -> None:
    if c.table().get("part") == "hang":
        return run_hang(c)
    if c.table().get("part") == "guard":
        return run_guard(c)
    if c.table().get("part") == "sticky":
        return run_sticky(c)
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
        # LOGCFG: levels (finding 46; docs/design/module-sdk.md 13.2)
        cfg = up.logcfg()
        c.check(
            "LOGCFG get: default level info (2), no module level set, debug on",
            cfg == {"default": 2, "modules": [0xFF] * 8, "debug": True},
            str(cfg),
        )
        m0 = len(events(rig, "UPLOAD"))
        up.logcfg(default=0)
        up.request(DK.CMD_ABORT)  # an info event: dropped at level error
        mid = len(events(rig, "UPLOAD"))
        up.request(DK.CMD_DELETE, b"..\0")  # an error event: kept
        m1 = len(events(rig, "UPLOAD"))
        c.check(
            "LOGCFG default error (0): an info event is dropped, an error event is kept",
            mid == m0 and m1 == mid + 1,
            f"{m0} {mid} {m1}",
        )
        back = up.logcfg(default=2)
        up.request(DK.CMD_ABORT)
        c.check(
            "LOGCFG default back to info: the upload command is logged again",
            back["default"] == 2 and len(events(rig, "UPLOAD")) == m1 + 1,
            str(back),
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
    if c.dt2:
        part_modules(c, img)
    if c.tier == "full":
        periodic(c, img, blank, has_diag)
    c.common_checks_upload(project_writes_ok=True)


def part_modules(c: Any, img: Any) -> None:
    """MODULES (protocol command 3, docs/design/digikit-upload.md): after a boot with a module with DSP code (pluck) and a ColdFire-only one (xslice),
    the reply lists both with ranges inside the arena and the DSP area, the contract bytes and version of the file, the assigned machine types and
    the voice rows; paging by start index; it is not logged as an UPLOAD event."""
    files = {m: c.pack(m, m.split("/")[-1] + ".dkm") for m in ("test/dyn-pluck", "test/dyn-xslice")}
    card = c.card(list(files.values()))
    with c.rig(img, card, "modules") as rig:
        up = uploader(rig)
        info = up.info()
        c.check(
            "INFO sets the MODULES capability",
            DK.has_cap(info, "MODULES"),
            str(info["caps"]),
        )
        n_up = len(events(rig, "UPLOAD"))
        m = up.modules()
        names = [x["name"] for x in m["modules"]]
        pl = next(x for x in m["modules"] if x["name"] == "test/dyn-pluck")
        xs = next(x for x in m["modules"] if x["name"] == "test/dyn-xslice")
        st = M.sym(rig.plan, "core/runtime", "DKRT_STATE")
        arena_lo, arena_hi = rig.w(st + 28), rig.w(st + 32)
        dkm = {k: (Path(v).read_bytes()) for k, v in ((k, c.env.folder / "digikit" / v) for k, v in files.items())}

        def hdr(name: str) -> tuple[int, bytes]:
            raw = dkm[name]
            return int.from_bytes(raw[0x34:0x38], "big"), raw[0x38:0x40]

        c.check(
            "MODULES lists the two loaded modules by record index, with the file's version and contract bytes",
            m["total"] == 2
            and names == ["test/dyn-pluck", "test/dyn-xslice"]
            and [x["index"] for x in m["modules"]] == [0, 1]
            and all(
                x["version"] == hdr(x["name"])[0]
                and bytes(b for pair in x["contracts"] for b in pair) == hdr(x["name"])[1]
                for x in m["modules"]
            ),
            str(m["modules"]),
        )
        c.check(
            "the ColdFire ranges lie inside the arena, do not overlap, and with the free figure stay inside its size",
            all(arena_lo <= x["cf_base"] and x["cf_base"] + x["cf_size"] <= arena_hi and x["cf_size"] > 0 for x in m["modules"])
            and (pl["cf_base"] + pl["cf_size"] <= xs["cf_base"] or xs["cf_base"] + xs["cf_size"] <= pl["cf_base"])
            and m["arena_free"] + pl["cf_size"] + xs["cf_size"] <= arena_hi - arena_lo
            and 0 < m["arena_largest"] <= m["arena_free"],
            f"arena {arena_lo:#x}..{arena_hi:#x}, {m['arena_free']} free, {[(hex(x['cf_base']), x['cf_size']) for x in m['modules']]}",
        )
        c.check(
            "the DSP part: pluck has a range (and the free figure leaves room for it), xslice has none",
            pl["dsp_len"] > 0 and xs["dsp_len"] == 0 and m["dsp_free"] > 0
            and 0 < m["dsp_largest"] <= m["dsp_free"] and pl["dsp_off"] + pl["dsp_len"] > pl["dsp_off"],
            f"dsp free {m['dsp_free']}, pluck {pl['dsp_off']:#x}+{pl['dsp_len']:#x}",
        )
        t_pl = rig.dyn_type("test/dyn-pluck", "pluck")
        t_xs = rig.dyn_type("test/dyn-xslice", "xslice_d")
        rows = {k: r[0] for k, r in enumerate(rig.voice_rows())}
        pl_rows = [k for k in range(8) if pl["rows"] >> k & 1]
        c.check(
            "the assigned machine types are the ones in the boot's table; pluck owns the one voice row, with its type; xslice owns none",
            pl["types"] == [t_pl] and xs["types"] == [t_xs] and (t_pl, t_xs) == (16, 17)
            and len(pl_rows) == 1 and rows[pl_rows[0]] == t_pl and xs["rows"] == 0,
            f"{pl['types']} {xs['types']} rows {pl['rows']:#x} {xs['rows']:#x}",
        )
        c.check(
            "no guard fault so far, and the guard has switched nothing off",
            pl["faults"] == 0 and xs["faults"] == 0 and not pl["guard_off"] and not xs["guard_off"],
            "",
        )
        # paging: from record index 1 only xslice; past the end nothing
        b1 = up._ok(DK.CMD_MODULES, "modules", bytes((1,)))
        b9 = up._ok(DK.CMD_MODULES, "modules", bytes((9,)))
        c.check(
            "MODULES pages by start index: from 1 one record (index 1), from 9 none; the total stays 2",
            (b1[16], b1[17], b1[18]) == (2, 1, 1) and (b9[16], b9[17]) == (2, 0),
            f"{b1[16:19].hex()} {b9[16:18].hex()}",
        )
        c.check(
            "MODULES is not logged as an UPLOAD event",
            len(events(rig, "UPLOAD")) == n_up,
            f"{n_up} -> {len(events(rig, 'UPLOAD'))}",
        )
        c.report["modules"] = m
        c.done(rig)


def key_of(module: str, machine: str) -> int:
    """The machine key of a machine in a .dkm and in the sticky map: the 24-bit hash above a zero type byte."""
    return M.machine_hash(module, machine) << 8


def run_sticky(c: Any) -> None:
    """Sticky machine type numbers (docs/design/module-sdk.md 11.5 D7): the runtime gives a run-time machine a number in 16..23 at the boot load and the card
    remembers it in the selection record's map (8 entries of hash24 << 8 | type). On ColdFire-only rigs, one boot after the other on the card the
    previous boot left: (1) pluck and xslice: two numbers, written; (2) only xslice: it keeps its number, nothing is written; (3) xslice and params: the
    new machine does not take the number of the absent pluck; (4) the same set again: the map survived; (5) a forged full map: the new machine drops the lowest
    entry of a machine that is not in this boot, and the entry of a machine that is in it is safe."""
    from tools.patchkit.pk_digikit_project import card_after, export

    PL, XS, PA = (
        ("test/dyn-pluck", "pluck"),
        ("test/dyn-xslice", "xslice_d"),
        ("test/dyn-params", "dynp"),
    )
    files = {m: c.pack(m, m.split("/")[-1] + ".dkm") for m in (PL[0], XS[0], PA[0])}
    img = c.image_for(B.RT, "run")
    mapk = lambda rig: B.current(rig.sel_records())["map"]  # noqa: E731

    def boot(card: Any, tag: str, mods: list[tuple[str, str]]) -> tuple[Any, Any, list, list]:
        """Boot CARD, return (assigned types by module, map on the card, ASSIGNED events, EVICTED events, card after)."""
        with c.rig(img, card, tag, cf_only=True) as rig:
            info = rig.boot_info()
            c.check(
                f"{tag}: every module loaded (ColdFire part)",
                info["count"] == len(mods)
                and all(r in (0, B.E_LOADING) for r in info["results"]),
                str(info["results"]),
            )
            got = {m: rig.dyn_type(*m) for m in mods}
            table = rig.assign_table()
            asg = [e[4] for e in events(rig, "MACHINE_TYPE_ASSIGNED")]
            ev = [e for e in events(rig, "MACHINE_TYPE_EVICTED")]
            m = mapk(rig)
            # SELECT (the menu's save and the SysEx command) writes a whole new record: the map in it is the old one
            r = rig.set_selection([files[x[0]] for x in mods])
            c.check(
                f"{tag}: SELECT keeps the map bytes",
                r == 0 and mapk(rig) == m,
                f"{r} {[hex(x) for x in mapk(rig)]}",
            )
            ov = export(rig, tag)
            c.done(rig)
        return got, (m, table), asg, ev, card_after(c, card, ov)

    h = lambda mm: M.machine_hash(*mm)  # noqa: E731
    # 1: a fresh card
    card1 = c.card([files[PL[0]], files[XS[0]]])
    got, (m, tb), asg, _ev, after1 = boot(card1, "st1", [PL, XS])
    a, b = got[PL], got[XS]
    c.check(
        "boot 1 (pluck, xslice): the first two numbers, in load order, and the map on the card holds both entries",
        (a, b) == (16, 17)
        and m[:2] == [key_of(*PL) | a, key_of(*XS) | b]
        and m[2:] == [0] * 6
        and tb["dirty"] == 1
        and [x[:3] for x in asg] == [(h(PL), a, 0), (h(XS), b, 0)],
        f"types {got}, map {[hex(x) for x in m]}, events {asg}",
    )
    # 2: only xslice, on the card boot 1 left (the selection is the builder's, the map is kept: as SELECT does)
    card2 = c.card([files[XS[0]]], base=after1)
    got, (m2, tb), asg, _ev, after2 = boot(card2, "st2", [XS])
    c.check(
        "boot 2 (xslice only): xslice keeps its number (sticky), the map is unchanged and not rewritten",
        got[XS] == b and m2 == m and tb["dirty"] == 0 and [x[:3] for x in asg] == [(h(XS), b, 1)],
        f"types {got}, map {[hex(x) for x in m2]}, events {asg}",
    )
    # 3: add params: it must not take pluck's number (16), which no loaded module has but the card still remembers
    card3 = c.card([files[XS[0]], files[PA[0]]], base=after2)
    got, (m3, tb), asg, _ev, after3 = boot(card3, "st3", [XS, PA])
    c.check(
        "boot 3 (xslice, params): params does not take pluck's remembered number while a free one exists; xslice keeps its own",
        got[XS] == b and got[PA] == 18 and 16 not in got.values() and tb["dirty"] == 1
        and m3[:3] == [key_of(*PL) | a, key_of(*XS) | b, key_of(*PA) | 18],
        f"types {got}, map {[hex(x) for x in m3]}",
    )
    # 4: the same set again (the map written by boot 3 is on the card), then pluck back: it gets its own number again
    card4 = c.card([files[PA[0]], files[PL[0]], files[XS[0]]], base=after3)
    got, (m4, tb), asg, _ev, _after4 = boot(card4, "st4", [PA, PL, XS])
    c.check(
        "boot 4 (params, pluck, xslice): every machine has the number the card remembered, whatever the load order; nothing is rewritten",
        (got[PA], got[PL], got[XS]) == (18, a, b) and tb["dirty"] == 0 and m4 == m3
        and all(x[2] == 1 for x in asg),
        f"types {got}, events {asg}",
    )
    # 5: a forged map of eight entries: slot 2 is xslice's (a machine of this boot), the others belong to no module
    fakes = [0x111100 + 0x100 * i for i in range(8)]
    forged = [fakes[i] | (16 + i) for i in range(8)]
    forged[2] = key_of(*XS) | 18
    rec = B.make_record([files[PL[0]], files[XS[0]]], 1, 0, 0, 0, forged)
    card5 = c.card(None, recs=(rec, rec))
    got, (m5, tb), asg, ev, _after5 = boot(card5, "st5", [PL, XS])
    want_ev = [(fakes[0] >> 8, 16)]
    c.check(
        "boot 5 (a full map): xslice keeps its entry's number (18); pluck drops the lowest entry of a machine that is not in this boot (slot 0, logged as an error event), takes its number and records its own entry there",
        got[XS] == 18 and got[PL] == 16
        and [tuple(e[4][:2]) for e in ev] == want_ev and all(e[3] & 1 for e in ev)
        and m5 == [key_of(*PL) | 16, *forged[1:]]
        and tb["dirty"] == 1,
        f"types {got}, map {[hex(x) for x in m5]}, evicted {[e[4][:2] for e in ev]}",
    )
    # 6: a full map and a machine that is protected in slot 0: the next lowest entry goes
    forged2 = list(forged)
    forged2[0] = key_of(*XS) | 16
    forged2[2] = fakes[2] | 18
    rec = B.make_record([files[PL[0]], files[XS[0]]], 1, 0, 0, 0, forged2)
    card6 = c.card(None, recs=(rec, rec))
    got, (m6, tb), asg, ev, _after6 = boot(card6, "st6", [PL, XS])
    c.check(
        "boot 6 (a full map, xslice in slot 0): the entry of a machine that loads in this boot is never the one dropped: slot 1 goes and pluck takes its number (17)",
        got[XS] == 16 and got[PL] == 17
        and [tuple(e[4][:2]) for e in ev] == [(fakes[1] >> 8, 17)]
        and m6[0] == key_of(*XS) | 16 and m6[1] == key_of(*PL) | 17,
        f"types {got}, map {[hex(x) for x in m6]}, evicted {[e[4][:2] for e in ev]}",
    )
    c.common_checks_upload(project_writes_ok=True)


HANG_HINT = 7  # seven steps down the machine list: the first run-time machine; its type is the one the boot assigned (the only module: the first number)
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
        # the machine-change watch takes its baseline 2 s of firmware blocks after the first UI pass; the coupled emulator runs the blocks slower
        # than emulated time, so wait on the DSP's frame counter
        ui_first = next(e for e in events(rig) if e[2] == L.K["UI_FIRST"])[4][0]
        for _ in range(60):
            if int(rig.emu.call("status")["dsp_frames"]) >= ui_first + 3300:
                break
            rig.s.run_ms(500)
        HANG_TYPE = rig.dyn_type("test/dyn-dsp-hang", "hang")
        rig.select_type(HANG_TYPE, HANG_HINT)
        rig.s.run_ms(3000)
        before = events(rig)
        chg = [e for e in before if e[2] == L.K["MACHINE_CHANGE"]]
        c.check(
            "changing track 1's machine logs MACHINE_CHANGE once with the track and the old and new type (read from the firmware's own per-track state)",
            len(chg) >= 1
            and chg[-1][4][0] == 0
            and chg[-1][4][2] == HANG_TYPE
            and chg[-1][4][1] != HANG_TYPE
            and all(e[4][2] != e[4][1] for e in chg)
            and L.event_dict(chg[-1])["fields"]["track"] == 1,
            str([(e[4][:3]) for e in chg]),
        )
        c.check(
            "before the note: the DSP runs, the log has its first DSP_DIAG set (24 words in six events) and no DSP_STOPPED",
            len([e for e in before if e[2] == L.K["DSP_DIAG"]]) in (6, 12)
            and not [e for e in before if e[2] == L.K["DSP_STOPPED"]],
            f"{len(before)} events",
        )
        # the first set only (the wait for the machine-change baseline may let the ten-second refresh of the set in)
        first = [e for e in before if e[2] == L.K["DSP_DIAG"]][:6]
        words = [
            w for e in sorted(first, key=lambda e: e[4][0] & 0xFF) for w in e[4][1:]
        ][:24]
        c.check(
            "the first set is the layout of the README: header 0xD1A6 and 24 words, beats rising, context registers, the breadcrumb of the last voice's hook entry, no dispatch yet",
            len(words) == 24
            and words[0] == 0xD1A6 | 24 << 16
            and words[1] > 0
            and words[2] > 0
            and words[3:9] == [0, 1, 0xFFFFFFFF, 0, 1, 0xFFFFFFFF]
            and words[14] == 0x1F01
            and words[15:24]
            == [0] * 9,  # no dispatch yet, and the guard's words 17 to 23 are 0
            " ".join(f"{w:08x}" for w in words),
        )
        n_diag0 = len(
            [e for e in before if e[2] == L.K["DSP_DIAG"]]
        )  # 6, or 12 with the refresh
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
        c.check(
            "no resumed=True follows a stop (the two reply records of a frozen DSP never read as a beat), in the log and as dklog decodes it",
            not [e for e in evs if e[2] == L.K["DSP_STOPPED"] and e[4][4] == 1]
            and not [
                e
                for e in evs
                if e[2] == L.K["DSP_STOPPED"]
                and L.event_dict(e)["fields"].get("resumed")
            ],
            str([L.event_dict(e)["fields"] for e in stop]),
        )
        crumb = stop[0][4][2] if stop else 0
        want = CRUMB_CALLED | 0 << 8 | HANG_TYPE << 16
        c.check(
            "the stop event's breadcrumb: ROUTINE_CALLED, voice 0, the hang machine's type (the track with the hang machine)",
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
            "after the stop the full diagnostic set is logged, all 24 words (six DSP_DIAG error events per distinct reply)",
            len(err_diag) >= 6
            and len(err_diag) % 6 == 0
            and all(
                [e[4][0] & 0xFF for e in g] == [0, 1, 2, 3, 4, 5] for g in sets.values()
            ),
            f"{len(err_diag)} events, {len(sets)} sets",
        )
        stop_set = sorted(err_diag[:6], key=lambda e: e[4][0] & 0xFF)
        sw = [w for e in stop_set for w in e[4][1:]][:24]
        c.check(
            "the full set logged with the stop says one routine is still running: dispatches - returns = 1, the breadcrumb word again, the 24-word header",
            len(sw) == 24
            and sw[0] == 0xD1A6 | 24 << 16
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
            n_diag0 in (6, 12)
            and len([e for e in evs if e[2] == L.K["DSP_DIAG"] and not e[3] & 1])
            <= n_diag0 + 10,
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
            "ABOUT shows DSP STOPPED and the step: LAST: CALL V0 T<the hang machine's type>",
            "DSP STOPPED" in rows and f"LAST: CALL V0 T{HANG_TYPE}" in rows,
            str(rows),
        )
        text = c.work / "hang.log"
        L.follow(uploader(rig), text, once=True, echo=False)
        c.report["hang_log"] = str(text)
        c.done(rig)
    c.common_checks_upload(project_writes_ok=True)


# list positions of the two run-time machines in the guard scenario (the pluck is the first entry of the run-time machines, the fixture the second);
# their type numbers are the ones the boot assigned (card order: pluck, then the fixture)
GUARD_NAN_HINT = 8
GUARD_PLUCK_HINT = 7


def run_guard(c: Any) -> None:
    """The runtime guard of core/dsp-voice (docs/findings/51-pluck-tune-guard.md), on the coupled emulator. A run-time pluck on track 1 and the NaN
    fixture (test/dyn-guard-nan) on track 2, whose delay send is up: the fixture's block is NaN while its voice is active, which without the
    guard reaches the send FX and silences every track. Ten TRIG 2 presses in a second: the guard zeroes each block, counts it, logs MACHINE_FAULT
    and switches the type off after the 8th (MACHINE_DISABLED); the pluck on track 1 stays audible and finite, and the master output holds no
    NaN or Inf. (The stock sample machine has no sample on this card, so the audible track is a second run-time machine; the guard treats both alike.)"""
    import numpy as np

    from tools.patchkit import hook_ab

    nan = c.pack("test/dyn-guard-nan", "nan.dkm")
    plk = c.pack("test/dyn-pluck", "pluck.dkm")
    img = c.image_for(B.RT, "guard")
    card = c.card([plk, nan])
    with c.rig(img, card, "guard") as rig:
        info = rig.boot_info()
        c.check(
            "both modules loaded at boot (CF part, DSP part, two voice rows)",
            info["count"] == 2
            and info["results"] == [0, 0]
            and len([r for r in rig.voice_rows() if r[0] != B.FREE_TYPE]) == 2,
            str(info["results"]),
        )
        ui_first = next(e for e in events(rig) if e[2] == L.K["UI_FIRST"])[4][0]
        for _ in range(60):
            if int(rig.emu.call("status")["dsp_frames"]) >= ui_first + 3300:
                break
            rig.s.run_ms(500)

        def pick(track: int, want: int, hint: int) -> None:
            if track > 1:
                rig.s.do_tap(["TRK", f"TRIG {track}"])
                rig.s.run_ms(300)
            for downs in (hint, hint + 1, hint - 1):
                rig.select(downs)
                rig.s.run_ms(800)
                chg = [
                    e
                    for e in events(rig, "MACHINE_CHANGE")
                    if e[4][0] == track - 1 and e[4][2] == want
                ]
                if chg:
                    return
            raise M.Fail(f"no list position gives type {want} on track {track}")

        t_pluck = rig.dyn_type("test/dyn-pluck", "pluck")
        t_nan = rig.dyn_type("test/dyn-guard-nan", "guardnan")
        pick(1, t_pluck, GUARD_PLUCK_HINT)
        pick(2, t_nan, GUARD_NAN_HINT)
        # track 2's delay send up: FX page, encoder E (docs/findings: the panel of the DT2 FX pages)
        rig.s.do_tap("FX")
        rig.s.run_ms(300)
        rig.s.turn_burst("ENC E", 4, 30)
        wav = c.work / "guard.wav"
        rig.emu.call("pcm.tap", action="capture", path=str(wav))
        rig.emu.button(H.button("TRIG 1"), True)  # the pluck, held
        rig.s.run_ms(400)
        for _ in range(10):  # ten presses of the NaN machine in about a second
            rig.s.do_tap("TRIG 2")
            rig.s.run_ms(100)
        rig.s.run_ms(1500)  # a poll of the guard's words is a second apart
        rig.emu.call("pcm.tap", action="capture_stop")
        rig.emu.button(H.button("TRIG 1"), False)
        rig.s.run_ms(2500)
        evs = events(rig)
        faults = [e for e in evs if e[2] == L.K["MACHINE_FAULT"]]
        disabled = [e for e in evs if e[2] == L.K["MACHINE_DISABLED"]]
        pcm = hook_ab.read_wav(wav)
        c.check(
            "MACHINE_FAULT is logged (an error event): the fixture's type, voice 2 (track 2), kind nonfinite, the parameter words of the call",
            len(faults) >= 1
            and all(e[3] & 1 for e in faults)
            and L.event_dict(faults[-1])["fields"]["type"] == t_nan
            and L.event_dict(faults[-1])["fields"]["track"] == 2
            and L.event_dict(faults[-1])["fields"]["kind"] == "nonfinite",
            str([L.event_dict(e)["fields"] for e in faults]),
        )
        last = L.event_dict(faults[-1])["fields"] if faults else {}
        c.check(
            "the faults are counted: at least 8 (the type is switched off at the 8th within a second)",
            bool(faults) and last.get("faults", 0) >= 8,
            str(last),
        )
        c.check(
            "MACHINE_DISABLED is logged once for the fixture's type",
            len(disabled) == 1
            and disabled[0][4][0] == t_nan
            and disabled[0][3] & 1,
            str([L.event_dict(e)["fields"] for e in disabled]),
        )
        # no fault after the switch-off: a press now changes nothing
        n_before = len(faults)
        rig.s.do_tap("TRIG 2")
        rig.s.run_ms(2500)
        faults2 = events(rig, "MACHINE_FAULT")
        c.check(
            "a press after the switch-off causes no new fault",
            len(faults2) == n_before,
            f"{n_before} -> {len(faults2)}",
        )
        left = pcm if pcm.ndim == 1 else pcm[:, 0]
        win = 4800  # 100 ms
        rms = [
            float(np.sqrt(np.mean(left[i : i + win] ** 2)))
            for i in range(0, len(left) - win, win)
        ]
        c.check(
            "the master output stays finite (no NaN or Inf in the capture)",
            bool(np.all(np.isfinite(pcm))) and len(pcm) > 48000,
            f"{len(pcm)} samples",
        )
        c.check(
            "the pluck on track 1 stays audible while the fixture faults (every 100 ms window of the first 1.1 s above -70 dBFS: the string decays about 3 dB per window)",
            len(rms) >= 20 and min(rms[0:11]) > 10 ** (-70 / 20),
            f"{[round(20 * np.log10(max(r, 1e-9)), 1) for r in rms[:24]]}",
        )
        c.report["guard_wav"] = str(wav)
        text = c.work / "guard.log"
        L.follow(uploader(rig), text, once=True, echo=False)
        c.report["guard_log"] = str(text)
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
                "DSP diagnostics are logged quietly: a full set (24 words, six events) at the first poll, then only every ten seconds while just the beats move",
                len(diag) == 6 * len(starts)
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
