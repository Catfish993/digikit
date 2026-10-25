"""A runner step's data accesses, read from the runner's own trace events.

Tools that measure what the Python runner does (cycle models, DM/PM bus checks, write
watches) need every load and store of a step. The forms already record each one as a
trace event: action ``load``, ``store``, ``load-pey`` or ``store-pey`` (the SIMD
companion), with its space, address and width. This module reads them back.

Read the events rather than wrapping ``sharc_core.memory._dm_read`` or ``_dm_write``:
the forms modules import those names directly, so a wrapper installed on the memory
module sees almost none of their accesses.

    enable(runner.state)                      # once: a snapshot may have events off
    start = len(runner.state.trace)
    ... step ...
    for access in step_accesses(runner.state.trace[start:]):
        ...
    del runner.state.trace[:]                 # keep the trace bounded on a long run

Python runner only. The native engine has its own write watch (SHARC_WWATCH,
docs/INSTRUMENTATION.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tools.sharc_dev.sharc_core.forms_move import _ACCESS_WIDTH_BYTES
from tools.sharc_dev.sharc_core.state import State

# Trace actions that move data, and whether each reads or writes memory.
KINDS = {"load": "r", "load-pey": "r", "store": "w", "store-pey": "w"}


@dataclass(frozen=True, slots=True)
class Access:
    kind: str  # "r" or "w"
    space: str  # "DM" or "PM": the bus the instruction names
    address: int  # the byte address the event records
    width: int  # bytes: 1, 2, 4 or 8 (a long-word or LW pair)
    pc_sw: int
    companion: bool  # the SIMD companion (PEy) transfer of a pair


def enable(state: State) -> None:
    """Make the runner append one trace entry per event from now on.

    With ``record_events`` off (the default of some saved snapshots, to keep the
    trace small), each step keeps only its last action, without addresses.
    """
    state.record_events = True


def step_accesses(events: Iterable[dict]) -> Iterator[Access]:
    """The data accesses among EVENTS, in order. Symbolic addresses are skipped."""
    for event in events:
        kind = KINDS.get(event.get("action", ""))
        address = event.get("address")
        if kind is None or not isinstance(address, int):
            continue
        width = (
            8
            if event.get("long_word")
            else _ACCESS_WIDTH_BYTES.get(event.get("access_width", ""), 4)
        )
        yield Access(
            kind=kind,
            space=event.get("space", "DM"),
            address=address,
            width=width,
            pc_sw=event.get("pc_sw", -1),
            companion=event["action"].endswith("-pey"),
        )
