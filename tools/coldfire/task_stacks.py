#!/usr/bin/env python3
"""The RTOS task table of a ColdFire image, read from the code that creates the tasks, and the UI task's stack.

    uv run python -m tools.coldfire.task_stacks [--image dt2-1.16] [--json]

The firmware's tasks are made by one function, `task_create(task, entry, priority, stack base, stack size)` (cdecl, five
words on the stack; it stores the initial frame at `base + size - 12` and the stack pointer in the task record at +0x48).
The function is found by its body (the stores to +0x48, +0x4c and +0x50 of the record), the calls by `jsr` over the
Ghidra disassembly dump (out/ghidra/<image>-emac/disasm), and the five arguments by the pushes before each call. The UI
task is the one whose entry is the closest function start at or below the UI loop's queue pop (`jsr pop; addq.l #4,sp;
moveq #0x29,d1`, the site the runtime hooks). The same numbers feed `tools.coldfire.stack_watch` (DIGI_STACK_WATCH).

Not found here: tasks started by a task the table above does not see (a call whose arguments are not constants is listed
with `?`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GHIDRA = ROOT / "out" / "ghidra"

LINE = re.compile(r"^([0-9a-f]{8})  ((?:[0-9a-f]{4} ?)+)\s+(.*)$")


# The queue-pop call of the UI loop on DN2 1.11 and 1.12 (the same address on both), from docs/design/digikit-runtime.md.
DN2_SITE = 0x4002F178


@dataclasses.dataclass
class Task:
    record: int | None
    entry: int | None
    priority: int | None
    base: int | None
    size: int | None
    site: int
    caller: str

    @property
    def top(self) -> int | None:
        return None if self.base is None or self.size is None else self.base + self.size

    def row(self) -> dict:
        return {
            k: (hex(v) if isinstance(v, int) and k != "priority" else v)
            for k, v in dataclasses.asdict(self).items()
        }


def _read(image: str) -> list[tuple[str, list[tuple[int, str]]]]:
    d = GHIDRA / f"{image}-emac" / "disasm"
    out = []
    for f in sorted(d.iterdir()):
        lines = []
        for t in f.read_text().splitlines():
            m = LINE.match(t)
            if m:
                lines.append((int(m.group(1), 16), m.group(3).strip()))
        out.append((f.stem, lines))
    return out


def _push_value(addr: int, text: str) -> int | None | str:
    """The value pushed by an instruction (`str` `"?"` for an unknown value), or None if it is not a push."""
    if m := re.match(r"pea \((0x[0-9a-f]+)\)\.[wl]$", text):
        return int(m.group(1), 16)
    if m := re.match(r"pea \((0x[0-9a-f]+),PC\)$", text):
        return addr + 2 + int(m.group(1), 16)
    if m := re.match(r"move\.l #(-?0x[0-9a-f]+),-\(SP\)$", text):
        return int(m.group(1), 16) & 0xFFFFFFFF
    if text == "clr.l -(SP)":
        return 0
    if re.match(r"move\.l .*,-\(SP\)$", text):
        return "?"
    if re.match(r"pea ", text):
        return "?"
    return None


def create_function(files) -> int:
    for _, lines in files:
        body = "\n".join(t for _, t in lines)
        if (
            "move.l A1,(0x48,A0)" in body
            and "clr.l (0x4c,A0)" in body
            and "clr.l (0x50,A0)" in body
            and len(lines) < 40
        ):
            return lines[0][0]
    raise SystemExit("task_create not found")


def tasks(image: str) -> tuple[int, list[Task], int]:
    files = _read(image)
    create = create_function(files)
    call = re.compile(r"(?:jsr|bsr\.?[lw]?) FUN_%08x(?:\.l)?$" % create)
    out: list[Task] = []
    for name, lines in files:
        for i, (addr, text) in enumerate(lines):
            if not call.match(text):
                continue
            args: list = []
            for a, t in reversed(lines[:i]):
                if t.startswith(("jsr", "bsr")):
                    break
                v = _push_value(a, t)
                if v is not None:
                    args.append(v)
                if len(args) == 5:
                    break
            args += ["?"] * (5 - len(args))
            num = [None if v == "?" else v for v in args]
            out.append(Task(num[0], num[1], num[2], num[3], num[4], addr, name))
    # the UI loop's queue pop
    pop = None
    for _, lines in files:
        for i in range(len(lines) - 2):
            if (
                lines[i][1].startswith("jsr")
                and lines[i + 1][1] == "addq.l 0x4,SP"
                and lines[i + 2][1] == "moveq 0x29,D1"
                and i + 4 < len(lines)
                and lines[i + 3][1] == "movea.l D0,A2"
                and lines[i + 4][1].startswith("mvz.b")
            ):
                pop = lines[i][0]
    if pop is None:
        # DN2: the loop is in code Ghidra has not made into a function, so it is not in the dump (docs/design/digikit-runtime.md)
        pop = DN2_SITE
    if pop is None:
        raise SystemExit("UI loop pop site not found")
    return create, sorted(out, key=lambda t: t.entry or 0), pop


def ui_task(ts: list[Task], pop: int) -> Task:
    cands = [t for t in ts if t.entry is not None and t.entry <= pop]
    return max(cands, key=lambda t: t.entry or 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", default="dt2-1.16")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    create, ts, pop = tasks(a.image)
    ui = ui_task(ts, pop)
    if a.json:
        print(
            json.dumps(
                {
                    "create": hex(create),
                    "pop": hex(pop),
                    "ui": ui.row(),
                    "tasks": [t.row() for t in ts],
                }
            )
        )
        return 0
    print(f"{a.image}: task_create {create:#x}, UI loop queue pop at {pop:#x}")
    for t in ts:
        mark = "  <- UI task" if t is ui else ""
        f = lambda v: "?" if v is None else f"{v:#x}"  # noqa: E731
        print(
            f"  record {f(t.record)} entry {f(t.entry)} prio {t.priority} stack {f(t.base)}..{f(t.top)} "
            f"size {f(t.size)}  (call {t.site:#x} in {t.caller}){mark}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
