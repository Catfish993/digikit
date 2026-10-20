#!/usr/bin/env python3
"""Upload, list and delete the files of the digikit module store over USB MIDI SysEx (docs/design/digikit-upload.md).

    uv run python -m tools.device.dkupload --emu info                       # the running emulator (out/native/run/current.json)
    uv run python -m tools.device.dkupload --emu ls
    uv run python -m tools.device.dkupload --emu modules                   # the loaded modules: ranges, contracts, assigned types, guard faults
    uv run python -m tools.device.dkupload --emu put build/m.dkm modules/m.dkm
    uv run python -m tools.device.dkupload --emu rm modules/m.dkm
    uv run python -m tools.device.dkupload --emu clear
    uv run python -m tools.device.dkupload --emu --run-dir out/runs/patchkit/run-NAME put build/m.dkm   # a scenario's daemon, not the desktop app's
    uv run --with python-rtmidi python -m tools.device.dkupload --port Digitakt info    # a real device (not exercised in CI)

`put FILE [PATH]` stores FILE at PATH (default: `modules/<file name>`) in chunks of at most 32 KiB, two requests in flight, and
checks the file's sha256 on the device (END reads the staged file back from the card). It changes only the module store; an uploaded
module appears in DIGIKIT > MODULES and loads after APPLY & RESTART (or `tools.device.dkrun`, which also selects and restarts).

Python (the kept scenarios use it): ``Uploader(EmuTransport(emu))`` or ``Uploader(RtMidiTransport("Digitakt"))``. The framing is
`frame()` / `parse_reply()`; the 8-in-7 packing is `tools.firmware.build.encode_8in7` and `tools.device.midisniff.unpack_8in7`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.device import dkproto
from tools.device.dkproto import (
    CAP,
    CMD_ABORT,
    CMD_BEGIN,
    CMD_CLEAR,
    CMD_DATA,
    CMD_DELETE,
    CMD_END,
    CMD_INFO,
    CMD_LIST,
    CMD_LOG,
    CMD_LOGCFG,
    CMD_MODULES,
    CMD_PEEK,
    CMD_RESTART,
    CMD_SELECT,
    LOAD_STATUS,
    VERSION,
)
from tools.device.midisniff import unpack_8in7
from tools.firmware.build import encode_8in7

PREFIX = bytes((0xF0, 0x7D, 0x64, 0x6B))
CLEAR_MAGIC = 0x434C5221
MAX_CHUNK = 32768
DEPTH = 2

# status code -> name: the upload statuses, and the load and selection codes (SELECT answers them as the menu's APPLY gets them)
STATUS: dict[int, str] = {
    0: "ok",
    **{c: n for c, (n, _) in LOAD_STATUS.items()},
    **{c: n for c, (n, _) in dkproto.STATUS.items()},
}

# The runtimes before INFO carried `caps` had these features from this packed version on (0.0.3 LOG and PEEK, 0.0.4 SELECT and
# RESTART, 0.1.0 LOGCFG). MODULES is known by `caps` only.
CAP_SINCE: dict[str, int] = {
    "LOG": 0x000003,
    "PEEK": 0x000003,
    "SELECT": 0x000004,
    "RESTART": 0x000004,
    "LOGCFG": 0x000100,
}


def fmt_runtime(packed: int) -> str:
    """A packed runtime version (major << 16 | minor << 8 | patch) as "major.minor.patch"; the plain integers 1 to 5 of old runtimes read as 0.0.N."""
    return f"{packed >> 16}.{packed >> 8 & 255}.{packed & 255}"


def has_cap(info: dict[str, Any], name: str) -> bool:
    """Does the device have the feature `name` (a key of `CAP`)? From the INFO `caps` word; a runtime without one is judged by its version
    (`CAP_SINCE`), and a feature known by `caps` only (MODULES) is absent there."""
    if name not in CAP:
        raise KeyError(name)
    if info.get("caps") is not None:
        return bool(info["caps"] >> CAP[name] & 1)
    since = CAP_SINCE.get(name)
    return since is not None and info["runtime"] >= since


class UploadError(RuntimeError):
    def __init__(self, status: int, what: str, body: bytes = b""):
        super().__init__(f"{what}: {STATUS.get(status, status)}")
        self.status, self.body = status, body


def frame(cmd: int, seq: int, body: bytes = b"", version: int = VERSION) -> bytes:
    """One request message, F0 to F7."""
    return (
        PREFIX + bytes((version, cmd & 0x7F, seq & 0x7F)) + encode_8in7(body) + b"\xf7"
    )


def select_body(paths: list[str]) -> bytes:
    """The body of SELECT: `u8 n`, then each store path and a NUL."""
    if len(paths) > 8:
        raise ValueError("a selection holds at most 8 paths")
    return bytes((len(paths),)) + b"".join(p.encode() + b"\0" for p in paths)


def parse_reply(msg: bytes) -> tuple[int, int, int, bytes] | None:
    """(cmd, seq, status, body after the status) of one of our replies, or None for any other message."""
    if len(msg) < 9 or msg[:4] != PREFIX or msg[-1] != 0xF7 or not msg[5] & 0x40:
        return None
    body = unpack_8in7(msg[7:-1])
    if not body:
        return None
    status = body[0] - 256 if body[0] >= 128 else body[0]
    return msg[5] & 0x3F, msg[6], status, body[1:]


def split_messages(stream: bytearray) -> list[bytes]:
    """Take the complete F0..F7 messages off the front of a byte stream (other bytes are dropped)."""
    out = []
    while True:
        a = stream.find(b"\xf0")
        if a < 0:
            stream.clear()
            return out
        b = stream.find(b"\xf7", a)
        if b < 0:
            del stream[:a]
            return out
        out.append(bytes(stream[a : b + 1]))
        del stream[: b + 1]


class EmuTransport:
    """The emulator's USB MIDI path: `midi.usb.in` hands a message to the firmware's receive callback, `midi.usb.out` returns what
    the firmware sent. Time passes only when stepped, so `recv` steps the emulator until a reply is there."""

    def __init__(
        self,
        emu: Any,
        step: int = 400_000,
        limit: int = 4_000_000_000,
        chunk: int | None = None,
        period: int | None = None,
    ):
        # CHUNK and PERIOD: the delivery model of `midi.usb.in` (default 64 bytes per 132,000 instructions, a full-speed frame; the
        # high-speed model is 384 and 16,500)
        self.emu, self.step, self.limit = emu, step, limit
        self.chunk, self.period = chunk, period
        self.buf = bytearray()
        self.ready: list[bytes] = []  # complete replies not yet handed out
        self.stepped = 0  # instructions stepped by recv (the host-side cost)

    def send(self, msg: bytes) -> None:
        self.emu.usb_in(msg, self.chunk, self.period)

    def recv(self, timeout_s: float = 0.0) -> bytes:
        spent = 0
        while True:
            self.buf += self.emu.usb_out()
            self.ready += [m for m in split_messages(self.buf) if parse_reply(m)]
            if self.ready:
                return self.ready.pop(0)
            if spent >= self.limit:
                raise TimeoutError("no reply from the emulated device")
            self.emu.step(self.step)
            spent += self.step
            self.stepped += self.step


class RtMidiTransport:
    """A real device through python-rtmidi (`pip` package `python-rtmidi`, not a repository dependency)."""

    def __init__(self, port_substring: str):
        import rtmidi  # type: ignore[import-not-found]

        self.mi, self.mo = rtmidi.MidiIn(), rtmidi.MidiOut()
        names_in, names_out = self.mi.get_ports(), self.mo.get_ports()
        pi = next(
            (i for i, n in enumerate(names_in) if port_substring.lower() in n.lower()),
            None,
        )
        po = next(
            (i for i, n in enumerate(names_out) if port_substring.lower() in n.lower()),
            None,
        )
        if pi is None or po is None:
            raise SystemExit(f"no MIDI port matching {port_substring!r}: {names_in}")
        self.mi.open_port(pi)
        self.mo.open_port(po)
        self.mi.ignore_types(sysex=False, timing=True, active_sense=True)

    def close(self) -> None:
        for p in (self.mi, self.mo):
            with contextlib.suppress(Exception):  # the device may be gone already
                p.close_port()

    def send(self, msg: bytes) -> None:
        self.mo.send_message(list(msg))

    def recv(self, timeout_s: float = 5.0) -> bytes:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            m = self.mi.get_message()
            if m:
                data = bytes(m[0])
                if parse_reply(data) is not None:
                    return data
            else:
                time.sleep(0.001)
        raise TimeoutError("no reply from the device")


class Uploader:
    """The host side of docs/design/digikit-upload.md over a transport with `send(bytes)` and `recv() -> bytes`."""

    def __init__(self, transport: Any, chunk: int = MAX_CHUNK, depth: int = DEPTH):
        if chunk <= 0 or chunk > MAX_CHUNK or chunk % 512:
            raise ValueError("chunk is a multiple of 512 up to 32768")
        self.t, self.chunk, self.depth = transport, chunk, depth
        self.seq = 0
        self.log: list[tuple[str, int]] = []

    # -- one request, one reply
    def _next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0x7F
        return self.seq

    def post(self, cmd: int, body: bytes = b"", version: int = VERSION) -> int:
        seq = self._next_seq()
        self.t.send(frame(cmd, seq, body, version))
        return seq

    def wait(self, cmd: int, seq: int) -> tuple[int, bytes]:
        r = parse_reply(self.t.recv())
        assert r is not None
        rcmd, rseq, status, body = r
        if rcmd != cmd or rseq != seq:
            raise UploadError(
                status, f"reply for cmd {rcmd:#x} seq {rseq}, wanted {cmd:#x} seq {seq}"
            )
        self.log.append((f"{cmd:#x}", status))
        return status, body

    def request(
        self, cmd: int, body: bytes = b"", version: int = VERSION
    ) -> tuple[int, bytes]:
        """(status, body after the status) without raising on a non-zero status."""
        return self.wait(cmd, self.post(cmd, body, version))

    def _ok(self, cmd: int, what: str, body: bytes = b"") -> bytes:
        status, rb = self.request(cmd, body)
        if status != 0:
            raise UploadError(status, what, rb)
        return rb

    # -- commands
    def info(self) -> dict[str, Any]:
        b = self._ok(CMD_INFO, "info")
        proto, runtime = struct.unpack_from(">HH", b, 0)
        end = b.index(0, 4)
        port = b[4:end].decode()
        free, max_chunk, flags = struct.unpack_from(">IIB", b, end + 1)
        # runtime 3 appends the sequence numbers of the oldest and newest log event
        oldest, newest = (
            struct.unpack_from(">II", b, end + 10) if len(b) >= end + 18 else (0, 0)
        )
        # appended after the log range: the packed runtime version (u32) and the capability bits (u32)
        caps = None
        if len(b) >= end + 26:
            runtime, caps = struct.unpack_from(">II", b, end + 18)
        return {
            "protocol": proto,
            "runtime": runtime,
            "caps": caps,
            "port": port,
            "free_bytes": free,
            "max_chunk": max_chunk,
            "safe": bool(flags & 1),
            "upload_open": bool(flags & 2),
            "card_read_only": bool(flags & 4),
            "debug_on": bool(flags & 8),
            "crash_found": bool(flags & 16),
            "log_oldest": oldest,
            "log_newest": newest,
        }

    def ls(self) -> list[tuple[str, int, str]]:
        """Every file of the store: (path, size, sha256 hex), in the store's order."""
        out: list[tuple[str, int, str]] = []
        first = 0
        while True:
            b = self._ok(CMD_LIST, "ls", struct.pack(">I", first))
            total, n = struct.unpack_from(">IB", b, 0)
            at = 5
            for _ in range(n):
                end = b.index(0, at)
                path = b[at:end].decode()
                size = struct.unpack_from(">I", b, end + 1)[0]
                out.append((path, size, b[end + 5 : end + 37].hex()))
                at = end + 37
            first += n
            if first >= total or n == 0:
                return out

    def modules(self) -> dict[str, Any]:
        """MODULES (runtime 0.2.0, capability MODULES): the arena and DSP-area figures and the loaded modules, every page. Returns
        `{arena_free, arena_largest, dsp_free, dsp_largest, total, modules: [...]}`; a module is `{index, name, version, contracts
        ((major, minor) of api, machine, fx, service), cf_base, cf_size, dsp_off, dsp_len, rows (voice-row mask), types (assigned machine
        types), faults (the guard's count for those types), guard_off (the guard switched one of them off)}`."""
        out: dict[str, Any] = {"modules": []}
        start = 0
        while True:
            b = self._ok(CMD_MODULES, "modules", bytes((start,)))
            af, al, df, dl = struct.unpack_from(">IIII", b, 0)
            total, n = b[16], b[17]
            out.update(
                arena_free=af, arena_largest=al, dsp_free=df, dsp_largest=dl, total=total
            )
            at = 18
            for _ in range(n):
                idx = b[at]
                name = b[at + 1 : at + 33].split(b"\0")[0].decode()
                version = struct.unpack_from(">I", b, at + 33)[0]
                cts = [(b[at + 37 + 2 * k], b[at + 38 + 2 * k]) for k in range(4)]
                cf_base, cf_size, dsp_off, dsp_len, rows = struct.unpack_from(
                    ">IIIII", b, at + 45
                )
                nt = b[at + 65]
                types = list(b[at + 66 : at + 66 + nt])
                faults = struct.unpack_from(">I", b, at + 66 + nt)[0]
                flags = b[at + 70 + nt]
                out["modules"].append(
                    {
                        "index": idx,
                        "name": name,
                        "version": version,
                        "contracts": cts,
                        "cf_base": cf_base,
                        "cf_size": cf_size,
                        "dsp_off": dsp_off,
                        "dsp_len": dsp_len,
                        "rows": rows,
                        "types": types,
                        "faults": faults,
                        "guard_off": bool(flags & 1),
                    }
                )
                at += 71 + nt
                start = idx + 1
            if len(out["modules"]) >= total or n == 0:
                return out

    def log_page(
        self, from_seq: int
    ) -> tuple[int, int, list[tuple[int, int, int, int, tuple[int, ...]]]]:
        """LOG (runtime 3): (oldest, newest, events) from `from_seq`; an event is (seq, time, kind, flags, (a0..a4)). Empty when the host has
        read everything."""
        b = self._ok(CMD_LOG, "log", struct.pack(">I", from_seq))
        oldest, newest, n = struct.unpack_from(">IIB", b, 0)
        events = []
        for i in range(n):
            seq, t, kind, flags, *a = struct.unpack_from(">IIHH5I", b, 9 + 32 * i)
            events.append((seq, t, kind, flags, tuple(a)))
        return oldest, newest, events

    def peek(self, addr: int, length: int) -> bytes:
        """PEEK (runtime 3): `length` (1 to 512) bytes of RAM at `addr`, inside the ranges the device file lists (`[digikit] peek`)."""
        b = self._ok(CMD_PEEK, "peek", struct.pack(">IH", addr, length))
        got, n = struct.unpack_from(">IH", b, 0)
        if got != addr or n != length or len(b) < 6 + n:
            raise UploadError(-50, "peek: malformed reply")
        return b[6 : 6 + n]

    def logcfg(
        self, default: int | None = None, modules: dict[int, int | None] | None = None
    ) -> dict[str, Any]:
        """LOGCFG: the log levels. With no argument, read them; else set `default` and/or the levels of module records (`modules`: record index ->
        level, None for the default). Returns {"default": int, "modules": [8 levels, 0xff = the default], "debug": bool}."""

        def parse(b: bytes) -> dict[str, Any]:
            if len(b) < 10:
                raise UploadError(-50, "logcfg: malformed reply")
            return {"default": b[0], "modules": list(b[1:9]), "debug": bool(b[9])}

        cur = parse(self._ok(CMD_LOGCFG, "logcfg", b"\x00"))
        if default is None and not modules:
            return cur
        lv = list(cur["modules"])
        for i, v in (modules or {}).items():
            lv[i] = 0xFF if v is None else v
        d = cur["default"] if default is None else default
        return parse(self._ok(CMD_LOGCFG, "logcfg", bytes([1, d, *lv])))

    def select(self, paths: list[str]) -> None:
        """SELECT (runtime 4): save the selection record (what the next boot loads); the menu's code path, so its card rules apply."""
        self._ok(CMD_SELECT, "select", select_body(paths))

    def restart(self) -> None:
        """RESTART (runtime 4): the reply comes first, then the device soft-resets (as APPLY & RESTART)."""
        self._ok(CMD_RESTART, "restart")

    def begin(self, path: str, size: int, sha256: bytes) -> None:
        self._ok(
            CMD_BEGIN,
            "begin",
            path.encode() + b"\0" + struct.pack(">I", size) + sha256,
        )

    def end(self) -> None:
        self._ok(CMD_END, "end")

    def abort(self) -> None:
        self._ok(CMD_ABORT, "abort")

    def rm(self, path: str) -> None:
        self._ok(CMD_DELETE, "rm", path.encode() + b"\0")

    def clear(self) -> None:
        self._ok(CMD_CLEAR, "clear", struct.pack(">I", CLEAR_MAGIC))

    def put(
        self,
        path: str,
        data: bytes,
        progress: Callable[[int, int], None] | None = None,
        sha256: bytes | None = None,
    ) -> None:
        """Store `data` at `path`: BEGIN, DATA chunks with up to `depth` in flight, END. Raises `UploadError` at the first bad
        status (an open upload is then left for the caller to ABORT)."""
        self.begin(path, len(data), sha256 or hashlib.sha256(data).digest())
        inflight: list[int] = []
        sent = 0
        offsets = list(range(0, len(data), self.chunk))
        done = 0
        while done < len(offsets):
            while sent < len(offsets) and len(inflight) < self.depth:
                off = offsets[sent]
                body = struct.pack(">I", off) + data[off : off + self.chunk]
                inflight.append(self.post(CMD_DATA, body))
                sent += 1
            seq = inflight.pop(0)
            status, rb = self.wait(CMD_DATA, seq)
            if status != 0:
                raise UploadError(status, f"data at {offsets[done]}", rb)
            done += 1
            if progress:
                progress(min(len(data), offsets[done - 1] + self.chunk), len(data))
        self.end()


def modules_lines(m: dict[str, Any]) -> list[str]:
    """The `modules` subcommand's output: a header with the free space, then one line per loaded module."""
    out = [
        f"arena free {m['arena_free']} (largest {m['arena_largest']}), dsp area free {m['dsp_free']} (largest {m['dsp_largest']}), {m['total']} module(s)"
    ]
    for x in m["modules"]:
        cts = " ".join(f"{a}.{b}" for a, b in x["contracts"])
        v = x["version"]
        out.append(
            f"{x['index']}  {x['name']}  v{v >> 16}.{v >> 8 & 255}.{v & 255}  contracts {cts}  "
            f"cf {x['cf_base']:#x}+{x['cf_size']:#x}  dsp {x['dsp_off']:#x}+{x['dsp_len']:#x}  rows {x['rows']:#x}  "
            f"types {','.join(map(str, x['types'])) or '-'}  faults {x['faults']}{'  GUARD OFF' if x['guard_off'] else ''}"
        )
    return out


def _emu(run_dir: Path | None = None) -> Any:
    from tools.control.digi_client import Emu

    return Emu.connect(run_dir=run_dir)


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
        help="with --emu: the run directory of a daemon (a scenario's, out/runs/patchkit/run-*), not the one in out/native/run/current.json",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("ls")
    sub.add_parser("modules")
    p = sub.add_parser("put")
    p.add_argument("file", type=Path)
    p.add_argument("path", nargs="?")
    p = sub.add_parser("rm")
    p.add_argument("path")
    sub.add_parser("clear")
    args = ap.parse_args(argv)
    t: Any = (
        EmuTransport(_emu(args.run_dir)) if args.emu else RtMidiTransport(args.port)
    )
    up = Uploader(t)
    try:
        if args.cmd == "info":
            for k, v in up.info().items():
                print(f"{k}: {v}")
        elif args.cmd == "ls":
            for path, size, sha in up.ls():
                print(f"{path}\t{size}\t{sha}")
        elif args.cmd == "modules":
            for line in modules_lines(up.modules()):
                print(line)
        elif args.cmd == "put":
            data = args.file.read_bytes()
            path = args.path or f"modules/{args.file.name}"
            t0 = time.monotonic()
            up.put(path, data)
            print(
                f"{path}: {len(data)} bytes in {time.monotonic() - t0:.1f} s (sha256 checked on the device)"
            )
        elif args.cmd == "rm":
            up.rm(args.path)
        elif args.cmd == "clear":
            up.clear()
    except UploadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
