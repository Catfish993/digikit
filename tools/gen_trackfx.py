#!/usr/bin/env python3
"""Generate scenarios/dn2-trackfx.json: one sweep per control of the DN2 track FX page.

    uv run python tools/gen_trackfx.py [OUT]

Every scenario has the same event times: page open, a 160-slot window on the
swept encoder (1M instructions per slot), a 10-slot routing window, Trig 1 at
210M instructions. Zero-detent turns fill the slots that do not turn, so
runs differ only in the parameter values. Encoders on the page: 1 BR, 2 OVER,
3 SRR, 4 SR.RT, 5 DEL, 6 REV, 7 CHR, 8 OD.RT.

The UI value reached by N events of +4 detents (read back, DN2 1.11) is
independent of the encoder: the encoder accelerates, so it is superlinear.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import scenario_dsl as d

# events of +4 detents -> UI value (high byte of the s16), measured by read-back
UPS = {28: 22, 44: 26, 71: 30, 93: 40, 127: 44}
ROUTE_UPS = 8  # events of +4 detents that set a routing parameter to 1 (0x100)
SLOTS = 160
ROUTE_START = d.TURN_START + SLOTS * d.SLOT
ROUTE_SLOTS = 10
NOTE_AT = ROUTE_START + ROUTE_SLOTS * d.SLOT + 20_000_000

BR, OVER, SRR, SRRT, DEL, REV, CHR, ODRT = range(1, 9)
NAMES = {BR: "br", OVER: "over", SRR: "srr", DEL: "del", REV: "rev", CHR: "chr"}


FLT_PAGE = 3  # filter page; encoder 5 is Frequency (Filter 1), 127 at start
FLT_TURNS = 46  # events of -4 detents: Frequency to about UI 60 (read back)
FLT_OFFSET = 70_000_000  # the FX page work starts this late when the filter is set


def build(
    name: str,
    text: str,
    sweep: tuple[int, int] | None,
    route: int | None,
    hold_s: float,
    tail_s: float,
    low_filter: bool = False,
) -> dict:
    """sweep = (encoder, UI target in UPS or 0), route = routing encoder set to 1.
    low_filter first closes the filter (page 3, encoder 5) and shifts the rest."""
    tl = d.Timeline()
    off = 0
    if low_filter:
        off = FLT_OFFSET
        d.open_page(tl, FLT_PAGE)
        for i in range(FLT_TURNS):
            tl.turn(5_000_000 + i * d.SLOT, 5, -4)
    d.open_page(tl, at=1_000_000 + off)
    enc = sweep[0] if sweep else BR
    d.turn_sweep(
        tl, enc, UPS.get(sweep[1], 0) if sweep else 0, SLOTS, start=d.TURN_START + off
    )
    d.turn_sweep(
        tl,
        route or SRRT,
        ROUTE_UPS if route else 0,
        ROUTE_SLOTS,
        start=ROUTE_START + off,
    )
    end = d.trig(tl, d.TRIG1, NOTE_AT + off, hold_s)
    return d.scenario(name, text, tl, end, tail_s)


def scenarios() -> list[dict]:
    out = []
    hold: dict[str, Any] = dict(hold_s=1.5, tail_s=1.0)
    tap: dict[str, Any] = dict(hold_s=0.3, tail_s=4.0)
    out.append(
        build(
            "tf-dry-hold",
            "DN2 track FX page, no turns, Trig 1 held 1.5 s.",
            None,
            None,
            **hold,
        )
    )
    for enc, levels in ((BR, UPS), (OVER, UPS), (SRR, UPS)):
        for ui in levels:
            out.append(
                build(
                    f"tf-{NAMES[enc]}-{ui}",
                    f"DN2 track FX page: encoder {enc} ({NAMES[enc]}) to about UI {ui}, Trig 1 held 1.5 s.",
                    (enc, ui),
                    None,
                    **hold,
                )
            )
        out.append(
            build(
                f"tf-{NAMES[enc]}-0",
                f"As tf-dry-hold, with zero-detent turns on encoder {enc}.",
                (enc, 0),
                None,
                **hold,
            )
        )
    for enc, rt, nm in ((SRR, SRRT, "srr"), (OVER, ODRT, "over")):
        for ui in (71, 127):
            out.append(
                build(
                    f"tf-{nm}-{ui}-rt1",
                    f"As tf-{nm}-{ui}, with the routing encoder {rt} set to 1 (256).",
                    (enc, ui),
                    rt,
                    **hold,
                )
            )
    out.append(
        build(
            "tf-dry-tap",
            "DN2 track FX page, no turns, Trig 1 tap (0.3 s), 4 s tail.",
            None,
            None,
            **tap,
        )
    )
    for enc in (DEL, REV, CHR):
        for ui in (28, 71, 127):
            out.append(
                build(
                    f"tf-{NAMES[enc]}-{ui}",
                    f"DN2 track FX page: send encoder {enc} ({NAMES[enc]}) to about UI {ui}, Trig 1 tap, 4 s tail.",
                    (enc, ui),
                    None,
                    **tap,
                )
            )
        out.append(
            build(
                f"tf-{NAMES[enc]}-0",
                f"As tf-dry-tap, with zero-detent turns on encoder {enc}.",
                (enc, 0),
                None,
                **tap,
            )
        )
    for ui in (71, 127):
        out.append(
            build(
                f"tf-chr-{ui}-hold",
                f"DN2 track FX page: chorus send to about UI {ui}, Trig 1 held 1.5 s, "
                "for the modulation rate.",
                (CHR, ui),
                None,
                **hold,
            )
        )
    # Routing with the filter closed (Frequency about UI 60): SRR/OVR before or
    # after the filter should now differ clearly.
    out.append(
        build(
            "tf-dry-flt",
            "Filter Frequency lowered to about UI 60, FX page, no FX, Trig 1 held 1.5 s.",
            None,
            None,
            low_filter=True,
            **hold,
        )
    )
    for enc, rt, nm in ((SRR, SRRT, "srr"), (OVER, ODRT, "over")):
        for ui in (71, 127):
            for route in (None, rt):
                suffix = "-rt1" if route else ""
                out.append(
                    build(
                        f"tf-{nm}-{ui}-flt{suffix}",
                        f"As tf-{nm}-{ui}{suffix}, with the filter closed first.",
                        (enc, ui),
                        route,
                        low_filter=True,
                        **hold,
                    )
                )
    return out


def main(argv: list[str]) -> int:
    default = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scenarios"
        / "dn2-trackfx.json"
    )
    path = pathlib.Path(argv[1]) if len(argv) > 1 else default
    sc = scenarios()
    d.write(path, sc)
    print(f"{path}: {len(sc)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
