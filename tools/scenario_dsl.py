#!/usr/bin/env python3
"""Builders for scenario event lists (scenarios/README.md), DN2/DT2 panel codes.

    tl = Timeline()
    open_page(tl, FX_PAGE)                      # button down at 1M, up at 3M
    turn_sweep(tl, 5, ups=40, slots=160)        # encoder E: 40 x +4 detents
    end = trig(tl, TRIG1, at=200_000_000, hold_s=1.5)
    sc = scenario("name", "text", tl, end_instr=end, tail_s=1.0)

Times are ColdFire instructions (about CF_HZ per audio second). ``turn_sweep``
keeps every sweep on the same clock: it always fills the same ``slots`` of
``spacing`` instructions, with zero-detent turns first and the real turns
last, so runs with different targets have the same event times and chunk
boundaries. (+step/-step pairs do not cancel: the encoder accelerates, so 80
pairs leave the value at 2 and the first pair at the end of a sweep at 6.)
The response is superlinear in the number of events for the same reason.
"""

from __future__ import annotations

import json
import math
import pathlib
from typing import Any

CF_HZ = 132_000_000  # ColdFire instructions per audio second (about)
FX_PAGE = 5  # track FX page button
TRIG1 = 25  # Trig 1 button: plays the 261.6 Hz audition sine
SLOT = 1_000_000  # instructions between encoder events
TURN_START = 20_000_000

Event = dict[str, Any]


class Timeline:
    """Event list that can be built in any order and is emitted sorted."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def button(self, at: int, code: int, down: bool) -> None:
        self.events.append({"at_instr": at, "button": code, "down": down})

    def turn(self, at: int, encoder: int, detents: int) -> None:
        self.events.append({"at_instr": at, "turn": encoder, "detents": detents})

    def as_list(self) -> list[Event]:
        return sorted(self.events, key=lambda e: e["at_instr"])


def open_page(tl: Timeline, button: int = FX_PAGE, at: int = 1_000_000) -> None:
    """Press and release a page button (2M instructions apart)."""
    tl.button(at, button, True)
    tl.button(at + 2_000_000, button, False)


def turn_sweep(
    tl: Timeline,
    encoder: int,
    ups: int,
    slots: int,
    step: int = 4,
    start: int = TURN_START,
    spacing: int = SLOT,
) -> int:
    """Fill ``slots`` event slots on ``encoder``: zero-detent events first,
    then ``ups`` events of +step at the end. Returns the instruction of the
    slot after the last one."""
    if not 0 <= ups <= slots:
        raise ValueError("ups must be within 0..slots")
    first_up = slots - ups
    for i in range(first_up):
        tl.turn(start + i * spacing, encoder, 0)
    for i in range(ups):
        tl.turn(start + (first_up + i) * spacing, encoder, step)
    return start + slots * spacing


def trig(tl: Timeline, button: int, at: int, hold_s: float) -> int:
    """Hold a button for hold_s audio seconds. Returns the release instruction."""
    up = at + round(hold_s * CF_HZ)
    tl.button(at, button, True)
    tl.button(up, button, False)
    return up


def scenario(
    name: str, description: str, tl: Timeline, end_instr: int, tail_s: float
) -> dict[str, Any]:
    """Scenario dict ending ``tail_s`` audio seconds after ``end_instr``."""
    seconds = math.ceil((end_instr / CF_HZ + tail_s) * 100) / 100
    return {
        "name": name,
        "description": description,
        "seconds": seconds,
        "exact_events": True,
        "events": tl.as_list(),
    }


def write(path: str | pathlib.Path, scenarios: list[dict[str, Any]]) -> None:
    """Write a scenario file in the layout of scenarios/dn2-fx.json."""
    pathlib.Path(path).write_text(json.dumps({"scenarios": scenarios}, indent=2) + "\n")
