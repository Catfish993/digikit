#!/usr/bin/env python3
"""Digitone II 1.12 emulator probes for the parameter-page / voice research (finding 02 "DN2 parameter pages", 06).

    uv run python -m tools.coldfire.dn2_probe knobs [--pages SYN] [--cycle 4] [--track 0]  # which frame words each knob moves
    uv run python -m tools.coldfire.dn2_probe words [--track 0]            # the voice-slot block of the TX frame at boot
    uv run python -m tools.coldfire.dn2_probe edge [--hold MS --after MS --repeat N]   # header words of the frame at trig / release
    uv run python -m tools.coldfire.dn2_probe slotmap [--track 6 --notes 3 --live]     # voice slot -> track, live parameter blocks
    uv run python -m tools.coldfire.dn2_probe dspblock [--notes 10]        # CF block vs the DSP's frame copy per slot
    uv run python -m tools.coldfire.dn2_probe edgepcm [--trig N --notes N] # test/dn2-edge clicks per note (patches and boots)
    uv run python -m tools.coldfire.dn2_probe pagepoke --page 1 --code 0xe3   # bind a page field to another parameter code
    uv run python -m tools.coldfire.dn2_probe shots                        # OLED screenshots of every stock SYN page

`knobs`: starts a stock DN2 1.12 daemon from a ready dir (env DIGI_PK_READY_DN2_112, default out/ready-fastweb-dn2), opens each page,
turns every encoder A..H with 30 events of +4 detents 2 ms apart (the encoder accelerates; slow single turns change
nothing), and prints which halfwords of the track's TX frame block (0xda + 0x92 t, 0x92 bytes, frame at 0x80005e60)
changed, as (frame byte offset, slot = 25 + (offset - 0xda) / 2 if inside the machine range, old -> new). The engine
parameter halfword of a slot is what the DSP's voice stages read. Bounded: a handful of seconds of emulated time per knob.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.environ.setdefault("DIGI_PK_READY_DN2_112", str(ROOT / "out" / "ready-fastweb-dn2"))
from tools.patchkit import pk_harness as H  # noqa: E402

TX = 0x80005E60
BLOCK, STRIDE = 0xDA, 0x92


def block(s: H.Session, track: int) -> list[int]:
    out = []
    base = TX + BLOCK + STRIDE * track
    for o in range(0, STRIDE, 2):
        out.append(s._u16(base + o))
    return out


def diff(a: list[int], b: list[int]) -> list[str]:
    r = []
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            off = BLOCK + 2 * i
            r.append(f"+{2 * i:#x} (frame {off:#x}) {x:#x}->{y:#x}")
    return r


def cmd_knobs(a: argparse.Namespace) -> int:
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        s.do_tap("TRIG 1")
        s.run_ms(300)
        for page in a.pages.split(","):
            for n in range(a.cycle):
                s.do_tap(page)
                s.run_ms(300)
                print(f"== page {page} press {n + 1}")
                for enc in "ABCDEFGH":
                    before = block(s, a.track)
                    s.turn_burst(f"ENC {enc}", 4, 30)
                    s.run_ms(100)
                    after = block(s, a.track)
                    print(
                        f"  ENC {enc}: "
                        + ("; ".join(diff(before, after)) or "no change")
                    )
                    s.turn_burst(f"ENC {enc}", -4, 30)
    return 0


def cmd_words(a: argparse.Namespace) -> int:
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        w = block(s, a.track)
        for i in range(0, len(w), 8):
            print(f"+{2 * i:#04x}", " ".join(f"{x:04x}" for x in w[i : i + 8]))
    return 0


def dsp_hw(s: H.Session, addr: int, n: int) -> list[int]:
    """N halfwords of the DSP's frame copy at word address ADDR (the engine stores the frame as 32-bit words, two
    halfwords per word, the earlier one in the high half: the frame is big-endian on the wire)."""
    raw = bytes.fromhex(
        s.emu.dsp_peek(addr, 4 * ((n + 1) // 2))["hex"].replace("??", "00")
    )
    return [int.from_bytes(raw[2 * i : 2 * i + 2], "little") for i in range(n)]


def cmd_edge(a: argparse.Namespace) -> int:
    """Hold TRIG N for HOLD ms, release, and log every change of the frame header halfwords (TX frame on the ColdFire side
    and the DSP's copy at 0x25c48c) at STEP ms resolution: the trig / release signals a voice routine could use."""
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        nhw = 0x60

        def snap() -> tuple[list[int], list[int]]:
            cf = [s._u16(TX + 2 * i) for i in range(nhw)]
            return cf, dsp_hw(s, 0x25C48C, nhw)

        prev = snap()
        t = 0.0

        def log(tag: str) -> None:
            nonlocal prev
            now = snap()
            for side, p, q in (("CF ", prev[0], now[0]), ("DSP", prev[1], now[1])):
                ch = [
                    f"hw{i:#x}:{x:#x}->{y:#x}"
                    for i, (x, y) in enumerate(zip(p, q, strict=False))
                    if x != y
                ]
                if ch:
                    print(f"t={t:7.2f} {tag:7s}{side} " + " ".join(ch))
            prev = now

        for _n in range(a.repeat):
            s.emu.button(H.button(f"TRIG {a.trig}"), True)
            for _ in range(int(a.hold / a.step)):
                s.run_ms(a.step)
                t += a.step
                log("hold")
            s.emu.button(H.button(f"TRIG {a.trig}"), False)
            for _ in range(int(a.after / a.step)):
                s.run_ms(a.step)
                t += a.step
                log("release")
    return 0


def cmd_edgepcm(a: argparse.Namespace) -> int:
    """test/dn2-edge on the Digitone II: play NOTES trigs of one key (HOLD ms down, GAP ms up) and count the clicks the
    routine writes for the frame's trig / release pulses (finding 06, "note-on edge"). Expect two clicks per pulse pair,
    16 samples apart (masks 0x11 and 0x13 at sample 0 and 16 of the block, 0x12 and 0x14 at 8 and 24), and one group of
    them at the note-on and one at the release for every note, however often the same key is retriggered."""

    import numpy as np

    from tools.patchkit import hook_ab

    os.environ["DIGI_PK_READY_DN2_112"] = os.environ.get(
        "DIGI_PK_READY_DN2_112", str(ROOT / "out" / "ready-fastweb-dn2")
    )
    mods = ["core/machines", "core/dsp-voice", "test/dn2-edge"]
    s = H.Session(
        H.Cell("dn2", "1.12", mods), H.Goldens(), log=None, run_name="dn2edge"
    )
    s.open()
    out = ROOT / "out" / "runs" / "dn2-edge"
    out.mkdir(parents=True, exist_ok=True)
    with s:
        s.reset()
        s.run_ms(1500)
        if a.trig != 1:
            s.do_tap(
                ["TRK", f"TRIG {a.trig}"]
            )  # select the track whose key is played: its voice goes to slot 0, not slot trig-1
            s.run_ms(300)
        s.do_tap(["FUNC", "SRC"])
        s.run_ms(300)
        for _ in range(4):
            s.do_tap("DOWN")
            s.run_ms(80)
        s.do_tap("YES")
        s.run_ms(300)
        s.do_tap("YES")
        s.run_ms(300)
        wav = out / "edge.wav"
        s.emu.call("pcm.tap", action="capture", path=str(wav))
        for _ in range(a.notes):
            s.emu.tap(H.button(f"TRIG {a.trig}"), hold=int(a.hold * H.MS))
            s.run_ms(a.hold + a.gap)
        s.run_ms(300)
        s.emu.call("pcm.tap", action="capture_stop")
        s.health("edgepcm")
    x = hook_ab.read_wav(wav)[0::2].astype(np.float64)
    mag = np.abs(x)
    thr = 0.02 * float(mag.max()) if mag.max() > 0 else 1.0
    idx = np.flatnonzero(mag > thr)
    groups: list[list[int]] = []
    for i in idx:
        if groups and i - groups[-1][-1] <= a.merge:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    print(
        f"notes played {a.notes}; peak {mag.max():.4f}; click groups {len(groups)} (merge {a.merge} samples)"
    )
    starts = [g[0] for g in groups]
    gaps = np.diff(starts)
    print(
        "group start samples:",
        starts[: a.show],
        "... gaps (ms):",
        [round(g / 48.0, 1) for g in gaps[: a.show]],
    )
    return 0 if groups else 1


def blocks(s: H.Session) -> list[list[int]]:
    return [block(s, v) for v in range(16)]


def cmd_slotmap(a: argparse.Namespace) -> int:
    """Voice slot -> track (finding 06, "slot to track"): select track N, turn encoder A of the SYN page by 30 x +4, then
    trig the track's key. Print which slot blocks (frame 0xda + 0x92 v) change and the ColdFire voice table
    (0x40288300 + 0x14 v: byte +6 = track, +7 = note, -1 when free) during the hold."""
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        s.do_tap(["TRK", f"TRIG {a.track}"])
        s.run_ms(300)
        s.do_tap("SYN")
        s.run_ms(300)
        base = blocks(s)
        for _ in range(a.presses - 1):
            s.do_tap("SYN")
            s.run_ms(300)
        s.turn_burst("ENC A", 4, 30)
        s.run_ms(300)
        after = blocks(s)
        print("after the turn (no trig), changed halfwords per slot:")
        for v in range(16):
            d = diff(base[v], after[v])
            if d:
                print(f"  slot {v}: " + "; ".join(d))
        for n in range(a.notes):
            s.emu.button(H.button(f"TRIG {a.track}"), True)
            s.run_ms(200)
            vt = []
            for v in range(a.notes + 2):
                w = s.emu.peek32(0x40288304 + 0x14 * v) or 0
                vt.append((v, (w >> 8) & 0xFF, w & 0xFF))
            print(
                f"note {n} held: voice table (slot, track byte, note byte):",
                [(x, t if t < 128 else t - 256, nn) for x, t, nn in vt],
            )
            now = blocks(s)
            for v in range(16):
                d = diff(base[v], now[v])
                if d:
                    print(f"  slot {v}: " + "; ".join(d))
            if a.live:
                s.turn_burst("ENC B", 4, 40)
                s.run_ms(200)
                live = blocks(s)
                print(
                    "  after ENC B +4 x40 while the key is held, changes against the held snapshot:"
                )
                for v in range(16):
                    d = diff(now[v], live[v])
                    if d:
                        print(f"  slot {v}: " + "; ".join(d))
            s.emu.button(H.button(f"TRIG {a.track}"), False)
            s.run_ms(300)
    return 0


def cmd_pagepoke(a: argparse.Namespace) -> int:
    """Prove how a page slot binds to a parameter descriptor: poke parameter code CODE into field INDEX of FM TONE's page
    PAGE (the heap descriptor the start-up code built; read from the page table at 0x42433ad4), open the SYN page and turn
    the encoder INDEX: the halfword of the code's sound-array slot (T1 row +4) must move in the TX frame, and the
    original slot's must not. Writes a PNG of the OLED to out/runs/dn2-pagepoke/."""
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        begin = s.emu.peek32(0x42433AD4 + 4) or 0
        desc = begin + 0x2C * a.page
        old = s.emu.peek32(desc + 8 + 4 * a.index)
        s.emu.poke32(desc + 8 + 4 * a.index, a.code)
        print(f"page descriptor {desc:#x}: field {a.index} {old:#x} -> {a.code:#x}")
        s.do_tap("TRIG 1")
        s.run_ms(300)
        out = ROOT / "out" / "runs" / "dn2-pagepoke"
        out.mkdir(parents=True, exist_ok=True)
        s.do_tap(
            "SYN"
        )  # the first press shows the second page (index 1) on a fresh boot
        s.run_ms(300)
        before = block(s, 0)
        s.turn_burst(f"ENC {'ABCDEFGH'[a.index]}", 4, 30)
        s.run_ms(300)
        after = block(s, 0)
        print("changed:", "; ".join(diff(before, after)) or "none")
        (out / "page.png").write_bytes(s.emu.frame_png())
    return 0


def cmd_shots(a: argparse.Namespace) -> int:
    """Screenshots of every SYN page of the four stock synth machines (which pages draw their own graphics): the machine
    menu entries 0..3 are FM TONE, FM DRUM, WAVETONE, SWARMER. PNGs in out/runs/dn2-shots/<machine>-<page>.png."""
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    out = ROOT / "out" / "runs" / "dn2-shots"
    out.mkdir(parents=True, exist_ok=True)
    names = ["fmtone", "fmdrum", "wavetone", "swarmer"]
    with s:
        s.reset()
        s.run_ms(1500)
        for k, name in enumerate(names):
            s.do_tap(["FUNC", "SRC"])
            s.run_ms(300)
            for _ in range(k):
                s.do_tap("DOWN")
                s.run_ms(80)
            s.do_tap("YES")
            s.run_ms(300)
            s.do_tap("YES")
            s.run_ms(300)
            for p in range(a.pages):
                s.do_tap("SYN")
                s.run_ms(300)
                (out / f"{name}-{p}.png").write_bytes(s.emu.frame_png())
    return 0


def cmd_dspblock(a: argparse.Namespace) -> int:
    """After NOTES retrigs (round-robin voice slots 0 .. NOTES-1) compare the TX frame block of every used slot on the
    ColdFire side with the DSP's frame copy at 0x25c48c (byte offsets are the same; the DSP stores two halfwords per
    32-bit word, the first in the high half): the engine parameter words of slot v are readable at DSP word address
    0x25c48c + (0xda + 0x92 v + 2 (slot - 25)) / 4. Slots >= 6 lie beyond halfword 511 of the frame."""
    s = H.Session(H.Cell("dn2", "1.12", []), H.Goldens(), log=None, run_name="dn2probe")
    s.open()
    with s:
        s.reset()
        s.run_ms(1500)
        s.do_tap("SYN")
        s.run_ms(300)
        s.turn_burst("ENC A", 4, 30)
        for _ in range(a.notes):
            s.emu.tap(H.button("TRIG 1"), hold=int(10 * H.MS))
            s.run_ms(60)
        s.run_ms(200)
        frame = dsp_hw(s, 0x25C48C, 0xDA // 2 + 0x49 * 16 + 4)
        for v in range(a.notes):
            cf = block(s, v)
            k0 = (0xDA + 0x92 * v) // 2
            dsp = frame[k0 : k0 + 0x92 // 2]
            same = sum(1 for x, y in zip(cf, dsp, strict=False) if x == y)
            print(
                f"slot {v}: frame halfword {k0:#x}; {same}/{len(cf)} halfwords equal; slot 35 word cf={cf[10]:#x} dsp={dsp[10]:#x}"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("knobs")
    k.add_argument("--pages", default="SYN")
    k.add_argument(
        "--cycle",
        type=int,
        default=4,
        help="presses of the page key (SYN has up to 4 pages)",
    )
    k.add_argument("--track", type=int, default=0)
    k.set_defaults(fn=cmd_knobs)
    w = sub.add_parser("words")
    w.add_argument("--track", type=int, default=0)
    w.set_defaults(fn=cmd_words)
    e = sub.add_parser("edge")
    e.add_argument("--trig", type=int, default=1)
    e.add_argument("--hold", type=float, default=20.0)
    e.add_argument("--after", type=float, default=20.0)
    e.add_argument("--step", type=float, default=0.35)
    e.add_argument("--repeat", type=int, default=1)
    e.set_defaults(fn=cmd_edge)
    c = sub.add_parser("edgepcm")
    c.add_argument("--trig", type=int, default=1)
    c.add_argument("--notes", type=int, default=24)
    c.add_argument("--hold", type=float, default=40.0)
    c.add_argument("--gap", type=float, default=60.0)
    c.add_argument("--merge", type=int, default=400)
    c.add_argument("--show", type=int, default=12)
    c.set_defaults(fn=cmd_edgepcm)
    m = sub.add_parser("slotmap")
    m.add_argument("--track", type=int, default=6)
    m.add_argument("--notes", type=int, default=3)
    m.add_argument("--presses", type=int, default=1)
    m.add_argument(
        "--live", action="store_true", help="turn a second knob while the key is held"
    )
    m.set_defaults(fn=cmd_slotmap)
    pp = sub.add_parser("pagepoke")
    pp.add_argument("--page", type=int, default=0)
    pp.add_argument("--index", type=int, default=0)
    pp.add_argument("--code", type=lambda x: int(x, 0), default=0xE3)
    pp.set_defaults(fn=cmd_pagepoke)
    sh = sub.add_parser("shots")
    sh.add_argument("--pages", type=int, default=4)
    sh.set_defaults(fn=cmd_shots)
    db = sub.add_parser("dspblock")
    db.add_argument("--notes", type=int, default=10)
    db.set_defaults(fn=cmd_dspblock)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
