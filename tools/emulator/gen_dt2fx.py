#!/usr/bin/env python3
"""Generate tests/scenarios/dt2-trackfx.json and tests/scenarios/dt2-sendfx.json: DT2 1.16 FX sweeps.

    uv run python -m tools.emulator.gen_dt2fx [OUT_DIR]

The names and the layout follow the DN2 files (tools/emulator/gen_trackfx.py,
tools/emulator/gen_sendfx.py): `tf-<fx>-<UI>` for a track FX control raised to about UI
<UI>, `sf-<fx>-<param>-u<N>` / `-d<N>` for N events of +4 / -4 detents on a send
FX page, so tools/emulator/fxcheck.py (`configure("dt2")`) and tests/scenarios read both.
They need the one-sample card of tools/emulator/dt2_sample_check.py (a 1 kHz sine, 1 s):
Trig 1 plays it, whatever the hold. `coupled.py scenario --device dt2 --dt2-card`.

The DT2 panel, found by read-back of the live Sound and Fx arrays
(docs/findings/18-dsp-performance.md, "DT2 FX scenarios"): the codes are the
DN2's. Button 12 (NO) closes the boot modal, 5 opens the track FX page, 3 the
filter page, 17 + 5 (FUNC + FX, latched chord) steps Delay -> Reverb -> Chorus ->
Delay, 17 + 6 (FUNC + MOD) opens the Master compressor page, 25 is Trig 1.

    FX page   1 Bit Reduction  2 Overdrive  3 SRR  4 SRR Routing  5 Delay Send
              6 Reverb Send  7 Chorus Send  8 OVR Routing  (9 none)
    Delay     1 Time 2 Pingpong 3 Width 4 Feedback Gain 5 Feedback HPF
              6 Feedback LPF 8 Reverb Send 9 Mix Volume
    Reverb    1 Pre-delay 2 Decay 3 Shelving Freq 4 Shelving Gain 5 HPF 6 LPF
              9 Mix Volume
    Chorus    1 Depth 2 Speed 3 High-pass 4 Width 7 Delay Send 8 Reverb Send
              9 Mix Volume
    Master    1 Threshold 2 Attack 3 Release 4 Makeup 5 Ratio 6 Sidechain Src
              7 Sidechain Filter 8 Dry/Wet Mix
    Filter    5 Frequency (127 at start)

The sweeps are shorter than the DN2 ones (50 slots of 1M instructions: 44
events of +4 reach UI 127), because a no-AOT run of the DT2 DSP costs about 80 s
per audio second.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

from tools.emulator import scenario_dsl as d

NO = 12
FLT_PAGE = 3
# UI value (high byte of the s16) -> events of +4 detents, read back on DT2 1.16
# (`gen_dt2fx.py OUT --ladder`). The encoder accelerates by the time between
# events, so the UI value is not a smooth function of the event count (n = 48
# gives 127 and n = 50 gives 113); these counts were measured in this layout.
UPS = {22: 24, 54: 30, 127: 52}
UPS_FLT = {127: 46}  # the closed-filter layout starts the sweep 60M later
ROUTE_UPS = 8
OPEN_AT = 4_000_000
SLOTS = 56
TURN_START = 14_000_000
ROUTE_START = TURN_START + SLOTS * d.SLOT
ROUTE_SLOTS = 10
NOTE_AT = ROUTE_START + ROUTE_SLOTS * d.SLOT + 6_000_000
FLT_TURNS = 46  # events of -4 detents: Frequency to about UI 60
FLT_OFFSET = 60_000_000

BR, OVER, SRR, SRRT, DEL, REV, CHR, ODRT = range(1, 9)
NAMES = {BR: "br", OVER: "over", SRR: "srr", DEL: "del", REV: "rev", CHR: "chr"}


def prelude(tl: d.Timeline) -> None:
    """NO closes the boot modal (a turn before it is ignored)."""
    tl.button(0, NO, True)
    tl.button(1_000_000, NO, False)


def tf_build(
    name: str,
    text: str,
    sweep: tuple[int, int] | None,
    route: int | None,
    hold_s: float,
    tail_s: float,
    low_filter: bool = False,
) -> dict[str, Any]:
    """sweep = (encoder, UI target in UPS or 0), route = routing encoder set to 1."""
    tl = d.Timeline()
    prelude(tl)
    off = 0
    if low_filter:
        off = FLT_OFFSET
        d.open_page(tl, FLT_PAGE, at=OPEN_AT)
        for i in range(FLT_TURNS):
            tl.turn(10_000_000 + i * d.SLOT, 5, -4)
    d.open_page(tl, at=OPEN_AT + off)
    enc = sweep[0] if sweep else BR
    ups = (UPS_FLT if low_filter else UPS)[sweep[1]] if sweep and sweep[1] else 0
    d.turn_sweep(tl, enc, ups, SLOTS, start=TURN_START + off)
    d.turn_sweep(
        tl,
        route or SRRT,
        ROUTE_UPS if route else 0,
        ROUTE_SLOTS,
        start=ROUTE_START + off,
    )
    end = d.trig(tl, d.TRIG1, NOTE_AT + off, hold_s)
    return d.scenario(name, text, tl, end, tail_s)


LADDER_SLOTS = SLOTS


def ladder() -> list[dict[str, Any]]:
    """Bit Reduction raised by N events of +4 (N = 20..100 step 4), in the track
    FX timeline and in the closed-filter one: read back to fill UPS / UPS_FLT."""
    out = []
    for flt in (False, True):
        for n in range(12, SLOTS + 1, 2):
            tl = d.Timeline()
            prelude(tl)
            off = FLT_OFFSET if flt else 0
            if flt:
                d.open_page(tl, FLT_PAGE, at=OPEN_AT)
                for i in range(FLT_TURNS):
                    tl.turn(10_000_000 + i * d.SLOT, 5, -4)
            d.open_page(tl, at=OPEN_AT + off)
            d.turn_sweep(tl, BR, n, LADDER_SLOTS, start=TURN_START + off)
            end = TURN_START + off + (LADDER_SLOTS + 4) * d.SLOT
            out.append(
                d.scenario(f"lad{'f' if flt else ''}-{n}", "BR ladder", tl, end, 0.0)
            )
    return out


def trackfx() -> list[dict[str, Any]]:
    out = []
    hold: dict[str, Any] = dict(hold_s=1.2, tail_s=0.2)
    tap: dict[str, Any] = dict(hold_s=0.3, tail_s=3.0)
    dev = "DT2 track FX page"
    out.append(
        tf_build(
            "tf-dry-hold", f"{dev}, no turns, Trig 1 held 1.2 s.", None, None, **hold
        )
    )
    for enc in (BR, OVER, SRR):
        for ui in (22, 54, 127):
            out.append(
                tf_build(
                    f"tf-{NAMES[enc]}-{ui}",
                    f"{dev}: encoder {enc} ({NAMES[enc]}) to about UI {ui}, Trig 1 held 1.2 s.",
                    (enc, ui),
                    None,
                    **hold,
                )
            )
    out.append(
        tf_build(
            "tf-br-0",
            f"As tf-dry-hold, with zero-detent turns on encoder {BR}.",
            (BR, 0),
            None,
            **hold,
        )
    )
    for enc, rt, nm in ((SRR, SRRT, "srr"), (OVER, ODRT, "over")):
        out.append(
            tf_build(
                f"tf-{nm}-127-rt1",
                f"As tf-{nm}-127, with the routing encoder {rt} set to 1 (256).",
                (enc, 127),
                rt,
                **hold,
            )
        )
    out.append(
        tf_build(
            "tf-dry-tap",
            f"{dev}, no turns, Trig 1 tap (0.3 s), 3 s tail.",
            None,
            None,
            **tap,
        )
    )
    for enc in (DEL, REV, CHR):
        for ui in (54, 127):
            out.append(
                tf_build(
                    f"tf-{NAMES[enc]}-{ui}",
                    f"{dev}: send encoder {enc} ({NAMES[enc]}) to about UI {ui}, Trig 1 tap, 3 s tail.",
                    (enc, ui),
                    None,
                    **tap,
                )
            )
    out.append(
        tf_build(
            "tf-dry-flt",
            "Filter Frequency lowered to about UI 60, FX page, no FX, Trig 1 held 1.2 s.",
            None,
            None,
            low_filter=True,
            **hold,
        )
    )
    for enc, rt, nm in ((SRR, SRRT, "srr"), (OVER, ODRT, "over")):
        for route in (None, rt):
            suffix = "-rt1" if route else ""
            out.append(
                tf_build(
                    f"tf-{nm}-127-flt{suffix}",
                    f"As tf-{nm}-127{suffix}, with the filter closed first.",
                    (enc, 127),
                    route,
                    low_filter=True,
                    **hold,
                )
            )
    return out


# --- send FX ---------------------------------------------------------------------

SEND_UPS = 52  # events of +4 detents that bring a track send to UI 127 (UPS)
SEND_START = TURN_START
CHORD_START = 74_000_000
CHORD_GAP = 6_000_000
SWEEP_SLOTS = 56
SWEEP_START = 98_000_000
SWEEP_SKEW = 500_000
SF_NOTE_AT = SWEEP_START + SWEEP_SLOTS * d.SLOT + 6_000_000
TRACK_SEND = {"del": DEL, "rev": REV, "chr": CHR}
PAGE_CHORDS = {"del": 1, "rev": 2, "chr": 3, "mst": 1}
PAGE_KEY = {"del": d.FX_PAGE, "rev": d.FX_PAGE, "chr": d.FX_PAGE, "mst": d.MOD_PAGE}

# (fx, param, encoder, settings in events: +N up, -N down; at most SWEEP_SLOTS)
PARAMS: list[tuple[str, str, int, tuple[int, ...]]] = [
    ("del", "time", 1, (-50, 20, 50)),
    ("del", "pp", 2, (20,)),
    ("del", "width", 3, (-50, 50)),
    ("del", "fb", 4, (-50, 20, 50)),
    ("del", "lpf", 6, (-50,)),
    ("del", "rsend", 8, (50,)),
    ("del", "mix", 9, (-50, 50)),
    ("rev", "pre", 1, (50,)),
    ("rev", "decay", 2, (-50, 20, 50)),
    ("rev", "lpf", 6, (-50,)),
    ("rev", "mix", 9, (-50,)),
    ("chr", "depth", 1, (-50,)),
    ("chr", "speed", 2, (-50, 50)),
    ("chr", "mix", 9, (-50,)),
    ("chr", "dsend", 7, (50,)),
]


def sf_build(
    name: str,
    text: str,
    fx: str | None,
    send: bool,
    sweeps: list[tuple[int, int]],
    hold_s: float,
    tail_s: float,
) -> dict[str, Any]:
    """fx = the page to open (None: the Delay page, nothing turned; mst: the
    Master compressor page), send = raise that effect's track send, sweeps =
    (encoder, signed events) windows that run side by side."""
    tl = d.Timeline()
    prelude(tl)
    d.open_page(tl, at=OPEN_AT)
    d.turn_sweep(
        tl,
        TRACK_SEND.get(fx or "del", DEL),
        SEND_UPS if send else 0,
        SWEEP_SLOTS,
        start=SEND_START,
    )
    for i in range(PAGE_CHORDS[fx or "del"]):
        tl.chord(CHORD_START + i * CHORD_GAP, d.FUNC, PAGE_KEY[fx or "del"])
    for k, (enc, n) in enumerate(sweeps or [(1, 0)]):
        d.turn_sweep(
            tl,
            enc,
            abs(n),
            SWEEP_SLOTS,
            step=4 if n >= 0 else -4,
            start=SWEEP_START + k * SWEEP_SKEW,
        )
    end = d.trig(tl, d.TRIG1, SF_NOTE_AT, hold_s)
    return d.scenario(name, text, tl, end, tail_s)


def sendfx() -> list[dict[str, Any]]:
    out = []
    tap = (0.3, 2.5)
    for hold, tail, tag in (tap + ("tap",), (1.2, 0.2, "hold")):
        out.append(
            sf_build(
                f"sf-dry-{tag}",
                f"DT2 send FX sweep reference: no sends, no turns, Trig 1 held {hold} s.",
                None,
                False,
                [],
                hold,
                tail,
            )
        )
    for fx in ("del", "rev", "chr"):
        out.append(
            sf_build(
                f"sf-{fx}-ref",
                f"Track {fx} send at UI 127, send FX page left at its defaults.",
                fx,
                True,
                [],
                *tap,
            )
        )
        for f, param, enc, settings in PARAMS:
            if f != fx:
                continue
            for n in settings:
                tag = f"u{n}" if n > 0 else f"d{-n}"
                out.append(
                    sf_build(
                        f"sf-{fx}-{param}-{tag}",
                        f"Track {fx} send at UI 127; send FX page encoder {enc} "
                        f"({param}): {abs(n)} events of {'+' if n > 0 else '-'}4 detents.",
                        fx,
                        True,
                        [(enc, n)],
                        *tap,
                    )
                )
    for n in (-50, 50):
        tag = f"u{n}" if n > 0 else f"d{-n}"
        out.append(
            sf_build(
                f"sf-del-ppwidth-{tag}",
                f"Track del send at UI 127; Pingpong on, Stereo Width {n:+d} events of 4 detents.",
                "del",
                True,
                [(2, 20), (3, n)],
                *tap,
            )
        )
    # Master compressor page: A threshold, B attack, C release, D makeup,
    # E ratio, F sidechain src, G sidechain filter, H dry/wet
    for nm, sw in (
        ("mix", [(8, 50)]),
        ("thr0", [(8, 50), (1, -50)]),
        ("ratio", [(8, 50), (1, -50), (5, 50)]),
        ("makeup", [(8, 50), (4, 50)]),
    ):
        out.append(
            sf_build(
                f"sf-mst-{nm}",
                f"Master compressor page, encoders {[e for e, _ in sw]} turned; Trig 1 held 1.2 s.",
                "mst",
                False,
                sw,
                1.2,
                0.2,
            )
        )
    return out


def main(argv: list[str]) -> int:
    root = (
        pathlib.Path(argv[1])
        if len(argv) > 1
        else pathlib.Path(__file__).resolve().parent.parent.parent
        / "tests"
        / "scenarios"
    )
    if len(argv) > 2 and argv[2] == "--ladder":
        d.write(root / "dt2-ladder.json", ladder())
        return 0
    for fn, sc in (("dt2-trackfx.json", trackfx()), ("dt2-sendfx.json", sendfx())):
        d.write(root / fn, sc)
        print(f"{root / fn}: {len(sc)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
