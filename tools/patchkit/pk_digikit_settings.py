#!/usr/bin/env python3
"""The `settings` kind of the digikit scenarios: the settings store of the runtime (docs/design/digikit-store.md, "The settings record"; docs/findings/63-settings-store.md).

Run by `tools/patchkit/pk_digikit_scen.py` (scenarios `settings-dt2`, `settings-dn2`). Boots of the runtime alone, each on its own daemon, through the mailbox test commands
(`CMD_SETTINGS_PUT`, `_GET`, `_REMOVE`) and the SETTINGS and CAPACITY commands over SysEx:

  main    a card that is ours (an empty selection record): no record yet (SETTINGS_LOAD, CAPACITY SETTINGS 8192 bytes, 18 used, SETTINGS answers E_SET_NOKEY); a key put and got back; a
          bad key refused; nothing is written for 2 s, and a second change inside that time starts the 2 s again; then the card write lands in settings copy A only (seq 1, one
          sector); the SETTINGS command and `patchkit store dump` on the card as the guest left it show the keys decoded; the next change goes to copy B (seq 2) and
          copy A is byte for byte what it was; a soft restart reads copy B back (SETTINGS_LOAD says copy B, seq 2) and the keys are the last ones; the next change goes to A (seq 3);
          a removed key stays removed after the write. The only store sectors the guest writes are the two copies.
  blank   a blank card (no structure of ours): the first settings write claims the area (the empty header pair, `claims` 1) and then writes copy A.
  copies  cards with both copies set by hand: a newer copy that is torn (crc) leaves the older one (SETTINGS_LOAD error event, damaged 1), and the next write goes to the damaged copy with
          seq + 1; of two good copies the higher seq is read and the other one is written next; two damaged copies are an empty record and the first write goes to copy A with seq 1.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any

from tools.device import dkupload as DK
from tools.device.dksettings import decode as decode_record
from tools.device.dksettings import encode as encode_record
from tools.patchkit import pk_dkrt_boot as B
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit.pk_digikit_debug import events, uploader
from tools.patchkit.pk_digikit_project import card_after

ROUTE = "fx/route/test/dyn-fx-gain"
SEND = "fx/send/test/dyn-fx-gain"
AMOUNTS = bytes([0, 127, 64, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100])
E_SET_KEY, E_SET_NOKEY, E_SET_OFF, E_NOSTORE, E_BAD_STORE = -66, -68, -70, -61, -2
DELAY_MS = 2000  # the runtime writes the record this long after the last change


def arg(key: str, value: bytes = b"") -> bytes:
    """The mailbox argument of the settings test commands: u8 key_len, key, u8 value_len, value."""
    return bytes([len(key)]) + key.encode() + bytes([len(value)]) + value


def put(rig: Any, key: str, value: bytes) -> int:
    return rig.post_raw(B.CMD_SETTINGS_PUT, arg(key, value))


def remove(rig: Any, key: str) -> int:
    return rig.post_raw(B.CMD_SETTINGS_REMOVE, arg(key))


def get(rig: Any, key: str) -> tuple[int, bytes]:
    """(the result, the value): the runtime answers the value's length and leaves the bytes at the start of the mailbox argument."""
    n = rig.post_raw(B.CMD_SETTINGS_GET, arg(key))
    if n <= 0:
        return n, b""
    words = [rig.w(rig.mb + M.MB_ARG + 4 * i) for i in range((n + 3) // 4)]
    return n, b"".join(w.to_bytes(4, "big") for w in words)[:n]


def sector_bytes(img: Path, sector: int) -> bytes:
    with open(img, "rb") as f:
        f.seek(sector * 512)
        return f.read(512).ljust(512, b"\0")


def written(rig: Any) -> list[int]:
    return sorted(rig.card_store_writes())


def run_settings(c: Any) -> None:
    img = c.image_for(B.RT, "run")
    st = c.store
    settings_area = {st.set_a, st.set_b}
    cf_only = bool(c.table().get("cf_only", False))
    ours = c.card(
        []
    )  # an empty selection record: the area is ours, the settings copies are zero
    tools = c.env.tools
    # Every card without a store of the folder is built before a module is packed (a packed module puts a store in every card built after it).
    blank = c.card(None)
    pattern = bytes(
        (i * 7 + 3) & 0xFF for i in range(512)
    )  # a sector that is not a record, not a header and not blank

    def store_header(count: int, table_crc: int, seq: int, used: int) -> bytes:
        b = bytearray(512)
        b[0:4] = b"DKST"
        struct.pack_into(
            ">IIIIIIII", b, 4, 3, count, 1, 1, table_crc, seq, used, st.sectors
        )
        struct.pack_into(">I", b, 508, zlib.crc32(bytes(b[:508])) & 0xFFFFFFFF)
        return bytes(b)

    # An older store (written before the settings area: its allocator ended at sectors - 3) with a file that reaches into the settings area:
    # file "x" from 2 sectors before header B for 4 sectors, so it covers header B and the first sector of settings copy A; the sectors hold a pattern.
    entry = bytearray(256)
    entry[0:1] = b"x"
    first_rel = st.limit - 2
    struct.pack_into(">II", entry, 160, first_rel, 4 * 512)
    table = bytes(entry).ljust(512, b"\0")
    hdr_over = store_header(1, zlib.crc32(table) & 0xFFFFFFFF, 1, 1 + 1 + 4)
    foreign_over = {st.first: hdr_over, st.first + 1: table}
    for k in range(4):
        foreign_over[st.first + first_rel + k] = pattern
    overlap = c.card([], foreign=foreign_over)

    def settings_events(rig: Any) -> list[tuple[int, int, int, int, tuple[int, ...]]]:
        return events(rig, "SETTINGS_LOAD") + events(rig, "SETTINGS_WRITE")

    def left_behind(rig: Any, base: Path, tag: str) -> Path:
        """The card as the guest left it: BASE with the guest's writes laid over it."""
        ov = rig.work / f"{tag}.after.bin"
        rig.emu.call("card.overlay", path=str(ov))
        return card_after(c, base, ov)

    def dump(card: Path, *flags: str) -> str:
        return tools.patchkit_run("store", "dump", card, "--device", c.device, *flags)

    def part_main() -> None:
        with c.rig(img, ours, "set-main", cf_only=cf_only) as rig:
            up = uploader(rig)
            ev = events(rig, "SETTINGS_LOAD")
            c.check(
                "main: the boot read the settings area: no valid copy (state 0, copy none, seq 0, 18 bytes, no entry)",
                len(ev) == 1 and ev[0][4] == (0, 2, 0, 18, 0),
                str(ev),
            )
            cap = up.capacity()["SETTINGS"]
            info = up.info()
            c.check(
                "main: CAPACITY reports SETTINGS in bytes (the area of one copy, 18 used) and INFO has the capability",
                cap["limit"] == 16 * 512
                and cap["used"] == 18
                and DK.has_cap(info, "SETTINGS"),
                f"{cap} caps {info.get('caps')}",
            )
            try:
                up.settings_record()
                err = 0
            except DK.UploadError as e:
                err = e.status
            c.check(
                "main: SETTINGS answers E_SET_NOKEY while there is no record",
                err == E_SET_NOKEY,
                str(err),
            )
            r1 = put(rig, ROUTE, b"\x01")
            r2 = put(rig, SEND, AMOUNTS)
            n1, v1 = get(rig, ROUTE)
            n2, v2 = get(rig, SEND)
            c.check(
                "main: put and get give the values back (route 1 byte, send amounts 16 bytes)",
                (r1, r2, n1, v1, n2, v2) == (0, 0, 1, b"\x01", 16, AMOUNTS),
                f"{(r1, r2, n1, v1, n2, v2)}",
            )
            n3, _ = get(rig, "fx/route/none")
            bad = (
                put(rig, "has space", b"x"),
                get(rig, "has space")[0],
                remove(rig, "has space"),
            )
            c.check(
                "main: a key that is not there answers E_SET_NOKEY, a key with a space E_SET_KEY (put, get, remove)",
                n3 == E_SET_NOKEY and bad == (E_SET_KEY,) * 3,
                f"{n3} {bad}",
            )
            c.check(
                "main: the same value again changes nothing",
                put(rig, ROUTE, b"\x01") == 0,
            )
            cap = up.capacity()["SETTINGS"]
            c.check(
                "main: CAPACITY used is the header and the two entries (18 + 29 + 43)",
                cap["used"] == 18 + (1 + len(ROUTE) + 2 + 1) + (1 + len(SEND) + 2 + 16),
                str(cap),
            )
            rig.s.run_ms(1000)
            c.check(
                "main: nothing is written in the first second",
                written(rig) == [],
                str(written(rig)),
            )
            flags, copy, seq, raw = up.settings_record()
            rec = decode_record(raw)
            c.check(
                "main: SETTINGS gives the RAM record before the write (flag 'not on the card', copy none) with the two entries",
                flags == 1
                and copy == 2
                and rec is not None
                and dict(rec.entries)
                == {ROUTE.encode(): b"\x01", SEND.encode(): AMOUNTS},
                f"{flags} {copy} {seq}",
            )
            # a second change at about 1 s: the 2 s start again
            put(rig, ROUTE, b"\x00")
            rig.s.run_ms(DELAY_MS - 500)
            c.check(
                "main: 1.5 s after the second change (2.5 s after the first) still nothing is written",
                written(rig) == [],
                str(written(rig)),
            )
            rig.s.run_ms(1000)
            w = written(rig)
            c.check(
                "main: about 2 s after the last change the record is written: one card write, settings copy A only (not B, not the selection, not the headers)",
                w == [st.set_a],
                f"{[hex(x) for x in w]}",
            )
            flags, copy, seq, raw = up.settings_record()
            rec = decode_record(raw)
            c.check(
                "main: SETTINGS now says copy A, seq 1, nothing waiting; the record has the last values",
                (flags, copy, seq) == (0, 0, 1)
                and rec is not None
                and rec.seq == 1
                and dict(rec.entries)[ROUTE.encode()] == b"\x00",
                f"{(flags, copy, seq)}",
            )
            rec_a = sector_bytes(left_behind(rig, ours, "m1"), st.set_a)
            c.check(
                "main: copy A on the card is a valid record (the vectors' reader), seq 1, 90 bytes, zero after them",
                decode_record(rec_a) is not None and rec_a[90:] == bytes(512 - 90),
                "",
            )
            after1 = left_behind(rig, ours, "m1")
            text = dump(after1)
            js = dump(after1, "--json")
            d = json.loads(js)
            sc = d["settings"]
            c.check(
                "main: `patchkit store dump` on the card as the guest left it: copy A in use, seq 1, the keys decoded (route master, the 16 amounts), copy B empty",
                sc["active"] == "A"
                and sc["copies"][0]["seq"] == 1
                and sc["copies"][1]["state"] == "empty"
                and {e["key"]: e["value"] for e in sc["copies"][0]["entries"]}
                == {ROUTE: "master", SEND: list(AMOUNTS)}
                and f"{SEND}  [0, 127, 64, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100]"
                in text
                and f"{ROUTE}  master (0)" in text
                and "copy A: sector" in text,
                text,
            )
            # the next change goes to the other copy, A stays as it is
            amounts2 = bytes([127] * 16)
            put(rig, SEND, amounts2)
            rig.s.run_ms(DELAY_MS + 500)
            w = written(rig)
            after2 = left_behind(rig, ours, "m2")
            c.check(
                "main: the next change goes to copy B (seq 2); copy A is byte for byte what it was",
                w == sorted(settings_area)
                and sector_bytes(after2, st.set_a) == rec_a
                and (rb := decode_record(sector_bytes(after2, st.set_b))) is not None
                and rb.seq == 2
                and dict(rb.entries)[SEND.encode()] == amounts2,
                f"{[hex(x) for x in w]}",
            )
            rig.restart()
            ev = events(rig, "SETTINGS_LOAD")
            n_r, v_r = get(rig, ROUTE)
            n_s, v_s = get(rig, SEND)
            c.check(
                "main: after a soft restart the boot reads copy B (state 1, copy 1, seq 2, 2 entries) and the values are the last ones",
                len(ev) == 1
                and ev[0][4][:3] == (1, 1, 2)
                and ev[0][4][4] == 2
                and (n_r, v_r, n_s, v_s) == (1, b"\x00", 16, amounts2),
                f"{ev} {(n_r, v_r, n_s, v_s)}",
            )
            flags, copy, seq, raw = up.settings_record()
            c.check(
                "main: SETTINGS after the restart: copy B, seq 2, nothing waiting",
                (flags, copy, seq) == (0, 1, 2),
                f"{(flags, copy, seq)}",
            )
            # the next change goes to A again (seq 3); a removed key stays removed
            put(rig, "fx/route/test/other", b"\x01")
            remove(rig, SEND)
            c.check(
                "main: a removed key is gone at once (get answers E_SET_NOKEY)",
                get(rig, SEND)[0] == E_SET_NOKEY,
            )
            rig.s.run_ms(DELAY_MS + 500)
            after3 = left_behind(rig, ours, "m3")
            ra = decode_record(sector_bytes(after3, st.set_a))
            rb = decode_record(sector_bytes(after3, st.set_b))
            c.check(
                "main: after the restart the next write goes to copy A (seq 3) with the new key and without the removed one; copy B is as it was",
                ra is not None
                and ra.seq == 3
                and dict(ra.entries)
                == {ROUTE.encode(): b"\x00", b"fx/route/test/other": b"\x01"}
                and sector_bytes(after3, st.set_b) == sector_bytes(after2, st.set_b),
                str(ra),
            )
            only = set(written(rig)) - {st.sel_a, st.sel_b}
            c.check(
                "main: the only store sectors written are the two settings copies",
                only == settings_area,
                f"{[hex(x) for x in sorted(only)]}",
            )
            c.done(rig)

    def part_blank() -> None:
        with c.rig(img, blank, "set-blank", cf_only=cf_only) as rig:
            ci = rig.card_info()
            put(rig, ROUTE, b"\x01")
            rig.s.run_ms(DELAY_MS + 500)
            w = written(rig)
            ci2 = rig.card_info()
            c.check(
                "blank: the first settings write claims the blank area (the empty header pair) and then writes copy A",
                ci["state_name"] == "blank"
                and ci2["state_name"] == "ours"
                and ci2["claims"] == 1
                and sorted(w) == sorted({st.first, st.first + st.limit, st.set_a}),
                f"{ci['state_name']} -> {ci2['state_name']} claims {ci2['claims']} {[hex(x) for x in w]}",
            )
            card = left_behind(rig, blank, "blank")
            d = json.loads(dump(card, "--json"))
            c.check(
                "blank: `patchkit store dump` shows an empty store (header pair, no file) and the key in copy A",
                d["store"]["files"] == []
                and d["settings"]["active"] == "A"
                and d["settings"]["copies"][0]["entries"][0]["key"] == ROUTE,
                str(d["settings"]["active"]),
            )
            c.done(rig)

    def torn(b: bytes) -> bytes:
        x = bytearray(b)
        x[20] ^= 0x55
        return bytes(x)

    def one_copies(name: str, card: Path, want: dict[str, Any]) -> None:
        with c.rig(img, card, f"set-{name}", cf_only=cf_only) as rig:
            ev = events(rig, "SETTINGS_LOAD")
            args = ev[0][4] if ev else ()
            n, v = get(rig, "k")
            c.check(
                f"copies/{name}: the boot reads {want['what']} (state, copy, seq) and get gives its value",
                len(ev) == 1
                and args[:3] == want["load"]
                and (n, v) == want["value"]
                and (ev[0][3] & 1) == want["error"],
                f"{ev} {(n, v)}",
            )
            put(rig, "k2", b"new")
            rig.s.run_ms(DELAY_MS + 500)
            after = left_behind(rig, card, f"copies-{name}")
            target = st.set_a if want["next"] == 0 else st.set_b
            keep = st.set_b if want["next"] == 0 else st.set_a
            rec = decode_record(sector_bytes(after, target))
            c.check(
                f"copies/{name}: the next write goes to copy {'AB'[want['next']]} with seq {want['seq']}; the other copy is untouched",
                written(rig) == [target]
                and rec is not None
                and rec.seq == want["seq"]
                and dict(rec.entries).get(b"k2") == b"new"
                and sector_bytes(after, keep) == sector_bytes(card, keep),
                f"{[hex(x) for x in written(rig)]} {rec and rec.seq}",
            )
            c.done(rig)

    old = encode_record(4, [(b"k", b"old")])
    new_torn = torn(encode_record(5, [(b"k", b"new")]))
    good_a, good_b = (
        encode_record(9, [(b"k", b"A9")]),
        encode_record(3, [(b"k", b"B3")]),
    )

    def two(a: bytes, b: bytes) -> Path:
        return c.card(
            [], foreign={st.set_a: a.ljust(512, b"\0"), st.set_b: b.ljust(512, b"\0")}
        )

    cards = {
        "torn": two(old, new_torn),
        "higher": two(good_a, good_b),
        "both": two(torn(old), new_torn),
    }

    def part_overlap() -> None:
        with c.rig(img, overlap, "set-overlap", cf_only=cf_only) as rig:
            up = uploader(rig)
            ev = events(rig, "SETTINGS_LOAD")
            c.check(
                "overlap: an older store with a file in the settings area turns the settings store off at the boot (SETTINGS_LOAD state 4, an error event, the store's status E_BAD_STORE)",
                len(ev) == 1
                and ev[0][4][0] == 4
                and (ev[0][3] & 1) == 1
                and ev[0][4][3] == E_BAD_STORE & 0xFFFFFFFF,
                str(ev),
            )
            cap = up.capacity()["SETTINGS"]
            c.check(
                "overlap: CAPACITY SETTINGS has limit 0 and nothing used",
                (cap["limit"], cap["used"]) == (0, 0),
                str(cap),
            )
            r = (put(rig, ROUTE, b"\x01"), get(rig, ROUTE)[0], remove(rig, ROUTE))
            try:
                up.settings_record()
                err = 0
            except DK.UploadError as e:
                err = e.status
            c.check(
                "overlap: put, get, remove and the SETTINGS command answer E_SET_OFF",
                r == (E_SET_OFF,) * 3 and err == E_SET_OFF,
                f"{r} {err}",
            )
            try:
                up.ls()
                ls = 0
            except DK.UploadError as e:
                ls = e.status
            c.check(
                "overlap: the store is not used either (LIST answers E_NOSTORE: no header copy is read as a store over a file)",
                ls == E_NOSTORE,
                str(ls),
            )
            rig.s.run_ms(DELAY_MS + 500)
            after = left_behind(rig, overlap, "overlap")
            c.check(
                "overlap: nothing is written in the store range, and the four sectors of the file are byte for byte as they were (header B and the first settings sector included)",
                written(rig) == []
                and all(
                    sector_bytes(after, st.first + first_rel + k) == pattern
                    for k in range(4)
                ),
                f"{[hex(x) for x in written(rig)]}",
            )
            c.done(rig)

    # the module of the old-layout card: packed last (see `blank`)
    mod = c.pack("test/c-demo", "a.dkm")
    new_layout = c.card([mod])
    hdr_a = sector_bytes(new_layout, st.first)
    # the card as an older runtime left it: header A and a copy of it at sectors - 3 (the old header B), nothing at today's header B
    old_layout = c.card(
        [mod],
        foreign={st.header_b: bytes(512), st.first + st.sectors - 3: hdr_a},
    )
    extra_ok: set[int] = set()

    def part_oldcard() -> None:
        with c.rig(img, old_layout, "set-oldcard", cf_only=cf_only) as rig:
            up = uploader(rig)
            info = rig.boot_info()
            c.check(
                "oldcard: a card with the older header B at sectors - 3 boots: the module of the selection loads, no safe mode, the selection is kept",
                info["results"] == [0]
                and info["safe"] == 0
                and info["count"] == 1
                and info["seq"] == 1,
                str(info),
            )
            ev = events(rig, "SETTINGS_LOAD")
            cap = up.capacity()
            c.check(
                "oldcard: the settings store is on (no valid copy yet) and the store reports its file",
                len(ev) == 1
                and ev[0][4][0] == 0
                and cap["SETTINGS"]["limit"] == 8192
                and [p for p, _, _ in up.ls()] == [mod],
                f"{ev} {cap.get('SETTINGS')}",
            )
            put(rig, ROUTE, b"\x01")
            rig.s.run_ms(DELAY_MS + 500)
            c.check(
                "oldcard: a settings write goes to copy A only (besides the selection record's boot counter)",
                sorted(set(written(rig)) - {st.sel_a, st.sel_b}) == [st.set_a],
                str([hex(x) for x in written(rig)]),
            )
            # an upload repairs the lagging header copy: header B is written at today's place, over free space
            up.put("t.bin", bytes(range(256)) * 3)
            after = left_behind(rig, old_layout, "oldcard")
            hb = sector_bytes(after, st.header_b)
            ha = sector_bytes(after, st.first)
            c.check(
                "oldcard: an upload then writes header B at the new place (a header of the same seq as A), and the store lists both files",
                hb[:4] == b"DKST"
                and hb[24:28] == ha[24:28]
                and sorted(p for p, _, _ in up.ls()) == sorted([mod, "t.bin"]),
                f"{hb[:4].hex()} {hb[24:28].hex()} {ha[24:28].hex()}",
            )
            d = json.loads(dump(after, "--json"))
            c.check(
                "oldcard: `patchkit store dump` on the result: two files, the selection kept, the key in settings copy A",
                sorted(f["path"] for f in d["store"]["files"]) == sorted([mod, "t.bin"])
                and d["selection"]["a"]["paths"] == [mod]
                and d["settings"]["active"] == "A",
                str(d["store"]["files"]),
            )
            extra_ok.update(written(rig))
            c.done(rig)

    c.parallel(
        part_main,
        part_blank,
        part_overlap,
        part_oldcard,
        lambda: one_copies(
            "torn",
            cards["torn"],
            {
                "what": "copy A (B is torn)",
                "load": (2, 0, 4),
                "value": (3, b"old"),
                "error": 1,
                "next": 1,
                "seq": 5,
            },
        ),
        lambda: one_copies(
            "higher",
            cards["higher"],
            {
                "what": "copy A (seq 9 over 3)",
                "load": (1, 0, 9),
                "value": (2, b"A9"),
                "error": 0,
                "next": 1,
                "seq": 10,
            },
        ),
        lambda: one_copies(
            "both",
            cards["both"],
            {
                "what": "no valid copy",
                "load": (0, 2, 0),
                "value": (E_SET_NOKEY, b""),
                "error": 1,
                "next": 0,
                "seq": 1,
            },
        ),
    )
    c.common_checks(
        store_extra=frozenset(
            settings_area | {st.first, st.first + st.limit} | extra_ok
        )
    )
