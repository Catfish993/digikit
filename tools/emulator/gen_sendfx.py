#!/usr/bin/env python3
"""Generate scenarios/dn2-sendfx.json: one sweep per control of the DN2 send FX pages.

    uv run python -m tools.emulator.gen_sendfx [OUT]

Navigation (found by read-back, docs/findings/15): FUNC + FX opens the Delay
page; each further FUNC + FX chord steps Delay -> Reverb -> Chorus -> Delay.
The chord must latch (FUNC down, FX down/up, FUNC up); FUNC need not be held
while turning. Encoders 1..8 = A..H, 9 = LEVEL (Mix Volume on every send FX page):

    Delay   1 Time  2 Pingpong  3 Width  4 Feedback Gain  5 Feedback HPF
            6 Feedback LPF  7 -  8 Reverb Send  9 Mix Volume
    Reverb  1 Pre-delay  2 Decay  3 Shelving Freq  4 Shelving Gain
            5 Input HPF  6 Input LPF  7 -  8 -  9 Mix Volume
    Chorus  1 Depth  2 Speed  3 High-pass  4 Width  5 -  6 -
            7 Delay Send  8 Reverb Send  9 Mix Volume

Every scenario has the same event times: track FX page (button 5), a 50-slot
window on the track send encoder (5 DEL, 6 REV, 7 CHR) that raises the send to
UI 127 (44 events of +4), the FUNC + FX chords, a 130-slot window on the swept
encoder (1M instructions per slot) and Trig 1 at 245M instructions. The dry
references turn nothing; sf-<fx>-ref raises the send only.
Names: sf-<fx>-<param>-u<N> / -d<N> for N events of +4 / -4 detents.
"""

from __future__ import annotations

import pathlib
import sys

from tools.emulator import scenario_dsl as d

SEND_SLOTS = 50
SEND_UPS = 44  # events of +4 detents that bring a track send to UI 127
SEND_START = 20_000_000
CHORD_START = 75_000_000
CHORD_GAP = 6_000_000
SWEEP_SLOTS = 130
SWEEP_START = 100_000_000
NOTE_AT = 245_000_000

TRACK_SEND = {"del": 5, "rev": 6, "chr": 7}
PAGE_CHORDS = {"del": 1, "rev": 2, "chr": 3, "mst": 1}
PAGE_KEY = {"del": d.FX_PAGE, "rev": d.FX_PAGE, "chr": d.FX_PAGE, "mst": d.MOD_PAGE}
SWEEP_SKEW = 500_000  # second and third sweeps start this much later each

U = (20, 44, 127)
DN = (-127, -44, -20)
MIX = (-127, -44, 127)
# (fx, param, encoder, settings in events: +N up, -N down)
PARAMS: list[tuple[str, str, int, tuple[int, ...]]] = [
    ("del", "time", 1, U),
    ("del", "pp", 2, (20,)),
    ("del", "width", 3, (-127, -44, -30, -20, 20, 44, 127)),
    ("del", "fb", 4, (-127, 10, 20, 30, 44, 127)),
    ("del", "hpf", 5, U),
    ("del", "lpf", 6, DN),
    ("del", "rsend", 8, U),
    ("del", "mix", 9, MIX),
    ("rev", "pre", 1, (20, 28, 32, 36, 44, 127)),
    ("rev", "decay", 2, (-127, 20, 44, 127)),
    ("rev", "shfreq", 3, MIX),
    ("rev", "shgain", 4, MIX),
    ("rev", "hpf", 5, U),
    ("rev", "lpf", 6, DN),
    ("rev", "mix", 9, MIX),
    ("chr", "depth", 1, DN),
    ("chr", "speed", 2, (-127, 20, 44, 127)),
    ("chr", "hpf", 3, U),
    ("chr", "width", 4, DN),
    ("chr", "dsend", 7, (44, 127)),
    ("chr", "rsend", 8, (44, 127)),
    ("chr", "mix", 9, MIX),
]


def build(
    name: str,
    text: str,
    fx: str | None,
    send: bool,
    sweeps: list[tuple[int, int]],
    hold_s: float,
    tail_s: float,
) -> dict:
    """fx = the page to open (None: the Delay page, nothing turned; mst: the
    Master compressor page), send = raise that effect's track send, sweeps =
    (encoder, signed events) windows that run side by side."""
    tl = d.Timeline()
    d.open_page(tl)
    d.turn_sweep(
        tl,
        TRACK_SEND.get(fx or "del", 5),
        SEND_UPS if send else 0,
        SEND_SLOTS,
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
    end = d.trig(tl, d.TRIG1, NOTE_AT, hold_s)
    return d.scenario(name, text, tl, end, tail_s)


def scenarios() -> list[dict]:
    out = []
    for hold, tail, tag in ((0.3, 4.0, "tap"), (1.5, 1.0, "hold")):
        out.append(
            build(
                f"sf-dry-{tag}",
                f"DN2 send FX sweep reference: no sends, no turns, Trig 1 held {hold} s.",
                None,
                False,
                [],
                hold,
                tail,
            )
        )
    for fx in ("del", "rev", "chr"):
        hold, tail = (1.5, 1.0) if fx == "chr" else (0.3, 4.0)
        out.append(
            build(
                f"sf-{fx}-ref",
                f"Track {fx} send at UI 127, send FX page left at its defaults.",
                fx,
                True,
                [],
                hold,
                tail,
            )
        )
        for f, param, enc, settings in PARAMS:
            if f != fx:
                continue
            for n in settings:
                tag = f"u{n}" if n > 0 else f"d{-n}"
                out.append(
                    build(
                        f"sf-{fx}-{param}-{tag}",
                        f"Track {fx} send at UI 127; send FX page encoder {enc} "
                        f"({param}): {abs(n)} events of {'+' if n > 0 else '-'}4 detents.",
                        fx,
                        True,
                        [(enc, n)],
                        hold,
                        tail,
                    )
                )
    # Pingpong on with the stereo width away from the centre (second sweep)
    for n in (-127, -44, 20, 127):
        tag = f"u{n}" if n > 0 else f"d{-n}"
        out.append(
            build(
                f"sf-del-ppwidth-{tag}",
                f"Track del send at UI 127; Pingpong on, Stereo Width {n:+d} events of 4 detents.",
                "del",
                True,
                [(2, 20), (3, n)],
                0.3,
                4.0,
            )
        )
    # Master compressor page (FUNC + MOD): A threshold, B attack, C release,
    # D makeup, E ratio, F sidechain src, G sidechain filter, H dry/wet
    for nm, sw in (
        ("mix", [(8, 127)]),
        ("thr0", [(8, 127), (1, -127)]),
        ("ratio", [(8, 127), (1, -127), (5, 127)]),
        ("makeup", [(8, 127), (4, 127)]),
    ):
        out.append(
            build(
                f"sf-mst-{nm}",
                f"Master compressor page, encoders {[e for e, _ in sw]} turned; Trig 1 held 1.5 s.",
                "mst",
                False,
                sw,
                1.5,
                1.0,
            )
        )
    return out


def main(argv: list[str]) -> int:
    default = (
        pathlib.Path(__file__).resolve().parent.parent.parent / "scenarios" / "dn2-sendfx.json"
    )
    path = pathlib.Path(argv[1]) if len(argv) > 1 else default
    sc = scenarios()
    d.write(path, sc)
    print(f"{path}: {len(sc)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
