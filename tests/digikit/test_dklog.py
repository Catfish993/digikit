"""The host side of the debug log (tools/device/dklog.py): event decoding, the crash record from its events, `follow` with gaps and restarts,
the output file. No emulator: a fake device answers LOG. The runtime's side is `tests/digikit/scenarios/debug-log.toml`."""

import json

from tools.device import dklog as L


class FakeUp:
    """What `Uploader.log_page` gives, from a list of rings the test sets up (a restart swaps the ring)."""

    def __init__(self, ring):
        self.ring = ring  # list of (seq, time, kind, flags, args)

    def log_page(self, from_seq):
        if not self.ring:
            return 0, 0, []
        oldest, newest = self.ring[0][0], self.ring[-1][0]
        evs = [e for e in self.ring if e[0] >= max(from_seq, oldest)][:27]
        return oldest, newest, evs


def ev(seq, kind, a=(0, 0, 0, 0, 0), t=None, flags=0):
    return (seq, t if t is not None else seq * 150, L.K[kind], flags, tuple(a))


def word(s):
    return int.from_bytes(s.encode().ljust(4, b"\0")[:4], "big")


def test_every_kind_has_a_name_and_five_arguments():
    assert sorted(L.KINDS) == list(range(1, 29))
    assert all(len(v[1]) == 5 for v in L.KINDS.values())
    assert len(set(v[0] for v in L.KINDS.values())) == len(L.KINDS)


def test_the_guard_events_decode():
    # type 18, voice 2 (track 2), kind 1 (nonfinite), 9 faults, knobs 0 .. 5 as the DSP packed them
    d = L.event_dict(
        ev(1, "MACHINE_FAULT", (18 | 2 << 16, 1 | 9 << 8, 0x1000_4000, 0x0000_2000, 0x0100_7F00), flags=1)
    )
    f = d["fields"]
    assert d["error"] and d["kind"] == "MACHINE_FAULT"
    assert (f["type"], f["voice"], f["track"], f["kind"], f["faults"]) == (18, 2, 2, "nonfinite", 9)
    assert f["params"] == ["4000", "1000", "2000", "0000", "7f00", "0100"]
    assert L.event_dict(ev(2, "MACHINE_FAULT", (18, 2 | 3 << 8, 0, 0, 0)))["fields"]["kind"] == "overlevel"
    f = L.event_dict(ev(3, "MACHINE_DISABLED", (18, 1, 17, 0, 0), flags=1))["fields"]
    assert (f["type"], f["disabled"], f["faults"]) == (18, 1, 17)
    # the diagnostic words 17 to 23 are named
    assert L.DIAG_NAMES[17:] == (
        "guard_faults", "guard_last", "guard_p01", "guard_p23", "guard_p45", "guard_p67", "guard_disabled_type"
    )


def test_events_decode_to_readable_fields():
    d = L.event_dict(ev(7, "CARD_CHECK", (3, 100, 0x0F498B0E, 0, 0), flags=1))
    assert d["error"] and d["kind"] == "CARD_CHECK" and d["t"] == 0.7
    assert (
        d["fields"]["state"] == "foreign"
        and d["fields"]["first_nonblank"] == 100
        and d["fields"]["hash"] == "0f498b0e"
    )
    assert (
        L.event_dict(ev(1, "CARD_CHECK", (2, 0xFFFFFFFF, 0, 0, 0)))["fields"][
            "first_nonblank"
        ]
        is None
    )
    d = L.event_dict(ev(2, "MOD_LOAD", (0, 0xFFFFFFD3, word("dyn-"), word("plu"), 0)))
    assert d["fields"]["name"] == "dyn-plu" and d["fields"]["result"] == -45
    d = L.event_dict(ev(3, "UPLOAD", (0x12, 0xFFFFFFC7, 5, 0, 0)))
    assert d["fields"]["status"] == -57 and d["fields"]["cmd"] == "0x12"
    d = L.event_dict(
        ev(4, "HEALTH", ((2 | 4) << 16 | 371, 120, 450, 100000, 0xFFFFFFFF))
    )
    assert (
        d["fields"]["cpu_pct"] == 37.1
        and d["fields"]["dsp_max_pct"] == 45.0
        and d["fields"]["heap_free_kb"] is None
    )
    d = L.event_dict(ev(5, "MOD_LOG", (word("cras"), word("h-pc"), 0, 0, 0x47E8B610)))
    assert d["fields"] == {"msg": "crash-pc", "value": 0x47E8B610}
    assert (
        L.event_dict((9, 1, 999, 0, (1, 2, 3, 4, 5)))["kind"] == "KIND999"
    )  # a kind a newer runtime has
    line = L.text_of(L.event_dict(ev(10, "SAFE", (1, 2, 3, 0, 0), flags=1)))
    assert "!SAFE" in line and "reason=counter" in line


def test_dsp_events_show_the_breadcrumb_as_names():
    crumb = 4 | 12 << 8 | 18 << 16  # ROUTINE_CALLED, voice 12, type 18
    d = L.event_dict(ev(7, "DSP_STOPPED", (30000, 29999, crumb, 55, 0), flags=1))
    assert d["error"] and d["fields"]["crumb"] == "ROUTINE_CALLED voice=12 type=18"
    assert d["fields"]["dispatches"] == 55 and d["fields"]["resumed"] is False
    assert L.crumb_of(crumb) == {"stage": "ROUTINE_CALLED", "voice": 12, "type": 18}
    assert L.crumb_of(9 | 2 << 8 | 18 << 16) == {
        "stage": "C3_FRAME_SAVED",
        "voice": 2,
        "type": 18,
    }
    # the diagnostic set, chunk 3 of 4: L7, MODE1, the breadcrumb, the dispatch count
    d = L.event_dict(ev(8, "DSP_DIAG", (16 << 8 | 3, 0x3E2, 0x39011CF8, crumb, 55)))
    assert d["fields"]["crumb"] == "ROUTINE_CALLED voice=12 type=18"
    assert d["fields"]["L7"] == "000003e2" and d["fields"]["dispatches"] == 55
    d = L.event_dict(ev(9, "DSP_DIAG", (16 << 8 | 0, 0xD1A6 | 16 << 16, 1500, 1500, 0)))
    assert d["fields"]["link_beat"] == 1500 and d["fields"]["M5"] == "00000000"


def crash_events(first_seq=10):
    """The events a boot writes for a crash record, as `crash.rs at_boot` writes them."""
    s = first_seq
    out = [
        ev(
            s,
            "CRASH_FOUND",
            (0x47E8B610, 4 | 4 << 8, 0x2004, 0x40B2_0000, 15000),
            flags=1,
        )
    ]
    out.append(
        ev(
            s + 1,
            "CRASH_MOD",
            (0, 1 | 0 << 16, word("test"), word("/dyn"), word("-cra")),
            flags=1,
        )
    )
    out.append(ev(s + 2, "CRASH_MOD", (0x100, 0x40102004, 1, word("sh"), 0), flags=1))
    for k in range(0, 16, 4):
        out.append(
            ev(
                s + 3 + k // 4,
                "CRASH_REG",
                (k, 0x1000 + k, 0x1001 + k, 0x1002 + k, 0x1003 + k),
                flags=1,
            )
        )
    for k in range(0, 32, 4):
        out.append(
            ev(s + 7 + k // 4, "CRASH_STACK", (k, k, k + 1, k + 2, k + 3), flags=1)
        )
    # one event before the crash: seq 5, time 700, kind MOD_LOG, flags 0, args (a0..a4), as two events
    kf = L.K["MOD_LOG"] << 16
    a = (word("cras"), word("h-pc"), 0, 0, 0x47E8B610)
    out.append(ev(s + 15, "CRASH_TAIL", (5, 700, kf, a[0], a[1]), flags=1))
    out.append(ev(s + 16, "CRASH_TAIL", (5, a[2], a[3], a[4], 1), flags=1))
    return out


def test_the_crash_record_is_rebuilt_from_its_events():
    evs = [ev(1, "BOOT_START"), *crash_events(2), ev(30, "UI_FIRST")]
    rec = L.crash_record(evs)
    assert (
        rec["pc"] == 0x47E8B610
        and rec["vector"] == 4
        and rec["vector_name"] == "illegal instruction"
        and rec["format"] == 4
    )
    assert rec["sr"] == 0x2004 and rec["a7"] == 0x40B20000
    assert rec["module"] == {
        "index": 0,
        "name": "test/dyn-cra",
        "slots_entered": 1,
        "boot_was_stable": False,
    }
    assert (
        rec["regs"]["d0"] == 0x1000
        and rec["regs"]["a7"] == 0x100F
        and len(rec["regs"]) == 16
    )
    assert rec["stack"] == {i: i for i in range(32)}
    assert rec["frame_word"] == 0x40102004 and rec["faults_in_handler"] == 1
    assert rec["tail"] == [
        (5, 700, L.K["MOD_LOG"], 0, (word("cras"), word("h-pc"), 0, 0, 0x47E8B610))
    ]
    text = L.crash_text(rec)
    assert (
        "pc 47e8b610" in text
        and "illegal instruction" in text
        and "test/dyn-cra" in text
        and "MOD_LOG" in text
        and "crash-pc" in text
    )


def test_the_last_crash_wins_and_a_log_without_one_has_none():
    assert L.crash_record([ev(1, "BOOT_START")]) is None
    first = crash_events(2)
    second = crash_events(40)
    second[0] = ev(40, "CRASH_FOUND", (0x1234, 2, 0x2000, 0x40000000, 1), flags=1)
    rec = L.crash_record([*first, ev(39, "UI_FIRST"), *second])
    assert (
        rec["pc"] == 0x1234
        and rec["vector"] == 2
        and rec["vector_name"] == "access error"
    )
    bad = L.crash_record([ev(1, "CRASH_FOUND", (0, 0, 0, 0, 0xBAD), flags=1)])
    assert bad == {"corrupt": True, "seq": 1}
    assert "CRC" in L.crash_text(bad)


def test_follow_writes_text_and_json_notes_a_gap_and_a_restart(tmp_path):
    ring = [ev(s, "UPLOAD", (1, 0, s, 0, 0)) for s in range(1, 6)]
    up = FakeUp(ring)
    out = tmp_path / "d" / "log.txt"
    assert L.follow(up, out, once=True, echo=False) == 5
    # nothing new: nothing written; then the ring moved on past what was read (a gap), and the events come in order
    assert L.follow(up, out, once=True, echo=False, start=6) == 0
    up.ring = [ev(s, "UPLOAD", (1, 0, s, 0, 0)) for s in range(40, 44)]
    fol = L.Follower(up, lambda text, d: lines.append((text, d)), start=6)
    lines: list = []
    assert fol.poll() == 4
    assert "34 events lost" in lines[0][0] and lines[0][1] is None
    assert [d["seq"] for _, d in lines[1:]] == [40, 41, 42, 43]
    # a restart: the newest sequence number is lower than what was seen
    up.ring = [ev(1, "BOOT_START"), ev(2, "CARD_CHECK", (2, 0xFFFFFFFF, 0, 0, 0))]
    lines.clear()
    assert fol.poll() == 2
    assert "restarted" in lines[0][0] and [d["seq"] for _, d in lines[1:]] == [1, 2]
    # the file: readable lines start with "# ", the rest is JSON lines
    rows = out.read_text().splitlines()
    js = [json.loads(x) for x in rows if not x.startswith("#")]
    assert [j["seq"] for j in js] == [1, 2, 3, 4, 5] and all(
        "host_time" in j and j["kind"] == "UPLOAD" for j in js
    )
    assert sum(1 for x in rows if x.startswith("#") and "UPLOAD" in x) == 5


def test_read_all_pages_through_the_ring():
    up = FakeUp([ev(s, "MENU", (1, 0, 0, 0, 0)) for s in range(1, 70)])
    oldest, newest, evs = L.read_all(up)
    assert (oldest, newest, len(evs)) == (1, 69, 69) and [e[0] for e in evs] == list(
        range(1, 70)
    )


def test_hexdump_has_the_address_the_bytes_and_text():
    out = L.hexdump(0x47E8B000, b"DKRT\x00\x00\x00\x03" + bytes(range(8)))
    assert out.startswith(
        "47e8b000  44 4b 52 54 00 00 00 03 00 01 02 03 04 05 06 07"
    ) and out.endswith("DKRT............")
