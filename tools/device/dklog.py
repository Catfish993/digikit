#!/usr/bin/env python3
"""Read the digikit runtime's debug log over USB MIDI SysEx (docs/design/digikit-upload.md commands LOG and PEEK; docs/findings/46-digikit-debug.md).

    uv run python -m tools.device.dklog --emu follow                        # the running emulator (out/native/run/current.json)
    uv run --with python-rtmidi python -m tools.device.dklog --port Digitakt follow
    uv run --with python-rtmidi python -m tools.device.dklog --port Digitakt crash
    uv run --with python-rtmidi python -m tools.device.dklog --port Digitakt peek 0x47e8b000 64

`follow` polls LOG (the device sends nothing unless asked) and writes every new event to the terminal as a readable line and to a file
(default `out/runs/device-logs/<date>.log`): a readable line starting `# ` and, right after it, the same event as one JSON object on a line of
its own. `grep -v '^#' FILE` is a JSON-lines file. It notes a gap (events the ring overwrote before they were read) and a device restart
(the sequence numbers start again). `--once` reads what is there and stops; Ctrl-C stops a follow.

`crash` prints the last crash record the device found at its boot, decoded: the program counter, the vector, the registers, the stack, the
module, and the events before the crash. `peek ADDR LEN` hex-dumps RAM inside the ranges the device file lists (`[digikit] peek`).

The log is a ring of 512 events: a host that polls once a second loses nothing in normal use (about 1 to 3 events a second with DEBUG on).
The transports are those of `tools.device.dkupload`: `--emu` for the emulator, `--port` for a device through python-rtmidi (not a repository
dependency).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.device import dkupload as D

ROOT = Path(__file__).resolve().parents[2]
BLOCKS_PER_SECOND = 1500

# kind -> (name, argument names). The argument words are as `modules/core/runtime/src/rs/src` writes them; docs/findings/46-digikit-debug.md.
KINDS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: (
        "BOOT_START",
        ("runtime", "device", "stable_blocks", "record_seq", "boot_counter"),
    ),
    2: ("CARD_CHECK", ("state", "first_nonblank", "hash", "read_errors", "-")),
    3: ("SAFE", ("reason", "boot_counter", "record_seq", "-", "-")),
    4: ("BOOT_RECORD", ("seq", "copy", "boot_counter", "selected", "flags")),
    5: ("MOD_LOAD", ("index", "result", "name0", "name1", "name2")),
    6: ("MOD_DSP", ("index", "result", "-", "-", "-")),
    7: ("UPLOAD", ("cmd", "status", "card_writes", "-", "-")),
    8: ("MENU", ("action", "arg", "-", "-", "-")),
    9: (
        "HEALTH",
        ("flags_cpu", "dsp_avg_x10", "dsp_max_x10", "arena_free", "heap_free_kb"),
    ),
    10: ("DSP_DIAG", ("count_chunk", "w0", "w1", "w2", "w3")),
    11: ("DSP_STOPPED", ("heartbeat", "w0", "w1", "w2", "w3")),
    12: ("CRASH_FOUND", ("pc", "vector_format", "sr", "a7", "time")),
    13: ("CARD_REFUSED", ("sector", "bytes", "reason", "card_state", "-")),
    14: ("MOD_LOG", ("msg0", "msg1", "msg2", "msg3", "value")),
    15: ("UI_FIRST", ("blocks", "safe", "loaded", "-", "-")),
    16: ("STABLE", ("blocks", "floor", "-", "-", "-")),
    17: ("DEBUG_SET", ("on", "-", "-", "-", "-")),
    18: ("RAM_KEEP", ("kept", "generation", "-", "-", "-")),
    19: ("CRASH_DETAIL", ("-", "-", "-", "-", "-")),
    20: ("DSP_ABSENT", ("-", "-", "-", "-", "-")),
    21: ("MOD_LIST", ("index", "slots", "name0", "name1", "name2")),
    22: ("CRASH_MOD", ("module", "entered_stable", "name0", "name1", "name2")),
    23: ("CRASH_REG", ("first", "r0", "r1", "r2", "r3")),
    24: ("CRASH_STACK", ("first", "w0", "w1", "w2", "w3")),
    25: ("CRASH_TAIL", ("a", "b", "c", "d", "e")),
}
K = {name: kid for kid, (name, _) in KINDS.items()}
CARD_STATES = {0: "unknown", 1: "ours", 2: "blank", 3: "foreign", 4: "unreadable"}
SAFE_REASONS = {1: "counter", 2: "key", 3: "flag"}
REFUSAL = {1: "outside the store range", 2: "card read-only", 3: "claim failed"}
REGS = [f"d{i}" for i in range(8)] + [f"a{i}" for i in range(8)]
VECTORS = {
    2: "access error",
    3: "address error",
    4: "illegal instruction",
    5: "divide by zero",
    8: "privilege violation",
    9: "trace",
    10: "line A",
    11: "line F",
    14: "format error",
}
EVENT_FLAG_ERROR = 1


def ascii_of(*words: int) -> str:
    raw = b"".join(w.to_bytes(4, "big") for w in words).split(b"\0")[0]
    return "".join(chr(c) if 32 <= c < 127 else "?" for c in raw)


def sx(v: int) -> int:
    return v - (1 << 32) if v >= 1 << 31 else v


def fields_of(kind: int, a: tuple[int, ...]) -> dict[str, Any]:
    """The argument words of an event by name, with the readable forms (names, signed results) the kind has."""
    name, names = KINDS.get(kind, (f"KIND{kind}", ("a0", "a1", "a2", "a3", "a4")))
    f: dict[str, Any] = {n: v for n, v in zip(names, a, strict=True) if n != "-"}
    if name == "CARD_CHECK":
        f["state"] = CARD_STATES.get(a[0], a[0])
        f["first_nonblank"] = None if a[1] == 0xFFFFFFFF else a[1]
        f["hash"] = f"{a[2]:08x}"
    elif name in ("MOD_LOAD", "MOD_LIST", "CRASH_MOD") and kind != 22:
        f = {k: v for k, v in f.items() if not k.startswith("name")}
        f["name"] = ascii_of(a[2], a[3], a[4])
        if name == "MOD_LOAD":
            f["result"] = sx(a[1])
    elif name == "CRASH_MOD":
        if a[0] == 0x100:
            f = {
                "frame_word": f"{a[1]:08x}",
                "faults": a[2],
                "name_tail": ascii_of(a[3]),
            }
        else:
            f = {
                "module": None if a[0] == 0xFFFFFFFF else a[0],
                "slots_entered": a[1] & 0xFFFF,
                "boot_was_stable": bool(a[1] >> 16),
                "name": ascii_of(a[2], a[3], a[4]),
            }
    elif name == "MOD_DSP":
        f["result"] = sx(a[1])
    elif name == "SAFE":
        f["reason"] = SAFE_REASONS.get(a[0], a[0])
    elif name == "UPLOAD":
        f["status"] = sx(a[1])
        f["cmd"] = f"{a[0]:#x}"
    elif name == "CARD_REFUSED":
        f["reason"] = REFUSAL.get(a[2], a[2])
        f["card_state"] = CARD_STATES.get(a[3], a[3])
    elif name == "MOD_LOG":
        f = {"msg": ascii_of(a[0], a[1], a[2], a[3]), "value": a[4]}
    elif name == "CRASH_FOUND":
        vec = a[1] & 0xFF
        f = {
            "pc": f"{a[0]:08x}",
            "vector": vec,
            "vector_name": VECTORS.get(vec, "?"),
            "format": a[1] >> 8,
            "sr": f"{a[2] & 0xFFFF:04x}",
            "a7": f"{a[3]:08x}",
            "crash_time_blocks": a[4],
        }
    elif name == "HEALTH":
        flags, cpu = a[0] >> 16, a[0] & 0xFFFF
        f = {
            "health_flags": flags,
            "cpu_pct": cpu / 10 if flags & 2 else None,
            "dsp_avg_pct": a[1] / 10 if flags & 4 else None,
            "dsp_max_pct": a[2] / 10 if flags & 4 else None,
            "arena_free": a[3],
            "heap_free_kb": None if a[4] == 0xFFFFFFFF else a[4],
        }
    elif name == "DSP_DIAG":
        f = {
            "count": a[0] >> 8,
            "chunk": a[0] & 0xFF,
            "words": [f"{w:08x}" for w in a[1:]],
        }
    elif name == "CRASH_REG":
        f = {REGS[a[0] + i]: f"{w:08x}" for i, w in enumerate(a[1:]) if a[0] + i < 16}
    elif name == "CRASH_STACK":
        f = {"first_word": a[0], "words": [f"{w:08x}" for w in a[1:]]}
    return f


def event_dict(ev: tuple[int, int, int, int, tuple[int, ...]]) -> dict[str, Any]:
    seq, t, kind, flags, a = ev
    name = KINDS.get(kind, (f"KIND{kind}", ()))[0]
    return {
        "seq": seq,
        "blocks": t,
        "t": round(t / BLOCKS_PER_SECOND, 3),
        "kind": name,
        "kind_id": kind,
        "error": bool(flags & EVENT_FLAG_ERROR),
        "args": list(a),
        "fields": fields_of(kind, a),
    }


def text_of(d: dict[str, Any]) -> str:
    parts = []
    for k, v in d["fields"].items():
        if isinstance(v, list):
            v = ",".join(map(str, v))
        parts.append(f"{k}={v}")
    flag = "!" if d["error"] else " "
    return f"{d['seq']:6d} {d['t']:10.3f}s {flag}{d['kind']:<13s} {' '.join(parts)}"


def read_all(
    up: D.Uploader, first: int = 0
) -> tuple[int, int, list[tuple[int, int, int, int, tuple[int, ...]]]]:
    """Poll LOG from `first` until the host has everything the device has: (oldest, newest, events)."""
    out: list[tuple[int, int, int, int, tuple[int, ...]]] = []
    want = first
    oldest = newest = 0
    while True:
        oldest, newest, evs = up.log_page(want)
        if not evs:
            return oldest, newest, out
        out += evs
        want = evs[-1][0] + 1
        if want > newest:
            return oldest, newest, out


class Follower:
    """The state of a `follow`: the next sequence number to ask for, and what was lost."""

    def __init__(
        self,
        up: D.Uploader,
        emit: Callable[[str, dict[str, Any] | None], None],
        start: int = 0,
    ):
        self.up, self.emit, self.next = up, emit, start
        self.seen_newest = 0

    def poll(self) -> int:
        """One round of polling; returns the number of events written."""
        n = 0
        while True:
            oldest, newest, evs = self.up.log_page(self.next)
            if newest and newest < self.seen_newest:
                self.emit(
                    f"the device restarted: the log starts again (was at {self.seen_newest}, now {newest})",
                    None,
                )
                self.next = 0
                self.seen_newest = 0
                continue
            if not evs:
                break
            if evs[0][0] > self.next and self.next:
                self.emit(
                    f"{evs[0][0] - self.next} events lost (the ring held from {oldest})",
                    None,
                )
            for ev in evs:
                d = event_dict(ev)
                self.emit(text_of(d), d)
                n += 1
            self.next = evs[-1][0] + 1
            self.seen_newest = max(self.seen_newest, newest)
            if self.next > newest:
                break
        return n


def follow(
    up: D.Uploader,
    out_path: Path | None,
    interval: float = 1.0,
    once: bool = False,
    start: int = 0,
    echo: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll the log and write it; `once` reads what is there and returns. Returns the number of events written."""
    f = None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        f = out_path.open("a")
        f.write(
            f"# dklog follow, started {datetime.datetime.now().isoformat(timespec='seconds')}\n"
        )

    def emit(text: str, d: dict[str, Any] | None) -> None:
        if echo:
            print(text if d is not None else f"# {text}", flush=True)
        if f is not None:
            f.write(f"# {text}\n")
            if d is not None:
                f.write(
                    json.dumps(
                        {
                            **d,
                            "host_time": datetime.datetime.now().isoformat(
                                timespec="milliseconds"
                            ),
                        }
                    )
                    + "\n"
                )
            f.flush()

    fol = Follower(up, emit, start)
    total = 0
    try:
        while True:
            total += fol.poll()
            if once:
                return total
            sleep(interval)
    except KeyboardInterrupt:
        return total
    finally:
        if f is not None:
            f.close()


# ---- the crash record ---------------------------------------------------------------------------------------------------------


def crash_record(
    events: list[tuple[int, int, int, int, tuple[int, ...]]],
) -> dict[str, Any] | None:
    """The last crash record in a list of events, decoded: the events from the last CRASH_FOUND on that belong to it."""
    starts = [
        i for i, e in enumerate(events) if e[2] == K["CRASH_FOUND"] and e[4][4] != 0xBAD
    ]
    bad = [e for e in events if e[2] == K["CRASH_FOUND"] and e[4][4] == 0xBAD]
    if not starts:
        return {"corrupt": True, "seq": bad[-1][0]} if bad else None
    i = starts[-1]
    pc, vf, sr, a7, tcrash = events[i][4]
    rec: dict[str, Any] = {
        "seq": events[i][0],
        "boot_blocks": events[i][1],
        "pc": pc,
        "vector": vf & 0xFF,
        "vector_name": VECTORS.get(vf & 0xFF, "?"),
        "format": vf >> 8,
        "sr": sr & 0xFFFF,
        "a7": a7,
        "crash_blocks": tcrash,
        "module": None,
        "regs": {},
        "stack": {},
        "tail": [],
    }
    pending: tuple[int, ...] | None = None
    for _seq, _t, kind, _fl, a in events[i + 1 :]:
        if kind == K["CRASH_MOD"]:
            if a[0] == 0x100:
                rec["frame_word"] = a[1]
                rec["faults_in_handler"] = a[2]
            else:
                rec["module"] = {
                    "index": None if a[0] == 0xFFFFFFFF else a[0],
                    "name": ascii_of(a[2], a[3], a[4]),
                    "slots_entered": a[1] & 0xFFFF,
                    "boot_was_stable": bool(a[1] >> 16),
                }
        elif kind == K["CRASH_REG"]:
            for j, w in enumerate(a[1:]):
                if a[0] + j < 16:
                    rec["regs"][REGS[a[0] + j]] = w
        elif kind == K["CRASH_STACK"]:
            for j, w in enumerate(a[1:]):
                rec["stack"][a[0] + j] = w
        elif kind == K["CRASH_TAIL"]:
            # two events per logged event: (seq, time, kind << 16 | flags, arg0, arg1), then (seq, arg2, arg3, arg4, 1)
            if pending is None:
                pending = a
            else:
                rec["tail"].append(
                    (
                        pending[0],
                        pending[1],
                        pending[2] >> 16,
                        pending[2] & 0xFFFF,
                        (pending[3], pending[4], a[1], a[2], a[3]),
                    )
                )
                pending = None
        else:
            break
    return rec


def crash_text(rec: dict[str, Any]) -> str:
    if rec.get("corrupt"):
        return f"a crash record was found at the boot (event {rec['seq']}) but it failed its CRC"
    m = rec["module"]
    lines = [
        f"crash found at boot (log event {rec['seq']}), crashed {rec['crash_blocks'] / BLOCKS_PER_SECOND:.1f} s after the boot before started",
        f"  vector {rec['vector']} ({rec['vector_name']}), format {rec['format']}, sr {rec['sr']:04x}",
        f"  pc {rec['pc']:08x}   a7 {rec['a7']:08x}",
    ]
    if m is not None:
        where = (
            f"module {m['name']!r} (index {m['index']})"
            if m["index"] is not None
            else f"outside any module, {m['name']!r}"
        )
        lines.append(
            f"  in {where}; slots entered mask {m['slots_entered']:#x}; the boot had {'already' if m['boot_was_stable'] else 'not yet'} reached its stable period"
        )
    if rec["regs"]:
        for i in range(0, 16, 4):
            lines.append(
                "  "
                + "  ".join(f"{r} {rec['regs'].get(r, 0):08x}" for r in REGS[i : i + 4])
            )
    if rec["stack"]:
        words = [rec["stack"].get(i, 0) for i in range(max(rec["stack"]) + 1)]
        lines.append("  stack from a7:")
        for i in range(0, len(words), 8):
            lines.append(
                f"    +{4 * i:03x}  " + " ".join(f"{w:08x}" for w in words[i : i + 8])
            )
    if rec["tail"]:
        lines.append("  events before the crash (oldest first):")
        for seq, t, kind, flags, a in rec["tail"]:
            d = event_dict((seq, t, kind, flags, a))
            lines.append("    " + text_of(d))
    return "\n".join(lines)


# ---- command line --------------------------------------------------------------------------------------------------------------


def hexdump(addr: int, data: bytes) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{addr + i:08x}  {hexs:<47s}  {asc}")
    return "\n".join(lines)


def peek_range(up: D.Uploader, addr: int, length: int) -> bytes:
    out = b""
    while length > 0:
        n = min(512, length)
        out += up.peek(addr, n)
        addr += n
        length -= n
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--emu",
        action="store_true",
        help="the running emulator (the desktop app or `mise run emu:serve`)",
    )
    where.add_argument(
        "--port", help="a MIDI port name substring (a real device; python-rtmidi)"
    )
    ap.add_argument(
        "--run-dir",
        type=Path,
        help="with --emu: the run directory of the emulator to talk to (default: discover)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("follow")
    p.add_argument(
        "--out", type=Path, help="the file (default out/runs/device-logs/<date>.log)"
    )
    p.add_argument(
        "--interval", type=float, default=1.0, help="seconds between polls (default 1)"
    )
    p.add_argument(
        "--once", action="store_true", help="read what the device has and stop"
    )
    p.add_argument(
        "--from",
        dest="start",
        type=int,
        default=0,
        help="first sequence number to read (default: the oldest)",
    )
    sub.add_parser("crash")
    p = sub.add_parser("peek")
    p.add_argument("addr", type=lambda s: int(s, 0))
    p.add_argument("length", type=lambda s: int(s, 0))
    sub.add_parser("info")
    args = ap.parse_args(argv)
    t: Any = (
        D.EmuTransport(D._emu(args.run_dir))
        if args.emu
        else D.RtMidiTransport(args.port)
    )
    up = D.Uploader(t)
    try:
        if args.cmd == "follow":
            out = (
                args.out
                or ROOT
                / "out"
                / "runs"
                / "device-logs"
                / f"{datetime.date.today().isoformat()}.log"
            )
            print(f"# writing {out}", file=sys.stderr)
            follow(up, out, args.interval, args.once, args.start)
        elif args.cmd == "crash":
            _, _, evs = read_all(up)
            rec = crash_record(evs)
            print(
                crash_text(rec)
                if rec
                else "no crash record in the log (the last boot did not follow a crash, or the ring has overwritten it)"
            )
        elif args.cmd == "peek":
            print(hexdump(args.addr, peek_range(up, args.addr, args.length)))
        elif args.cmd == "info":
            for k, v in up.info().items():
                print(f"{k}: {v}")
    except D.UploadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
