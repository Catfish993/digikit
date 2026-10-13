#!/usr/bin/env python3
"""patchkit smoke harness: drive a (patched) firmware in the control daemon and judge it.

    uv run python tools/pk_harness.py run [--matrix FILE] [--cell DEV-FW:mod,mod ...] [--update]
                                          [--tests smoke|quick|all] [--seed N] [--stress-actions N]
    uv run python tools/pk_harness.py list                 # the cells of the matrix
    mise run patch:test [-- --matrix FILE]

A *cell* is device x firmware x module set (matrix TOML `[[cell]]`: `device`, `firmware`, `modules`, optional
`stall_ms`). For each cell the harness builds the patched image (`tools/patchkit.py patch`; stock when the set is
empty), spawns a daemon on the stock firmware's ready inputs, loads the image (`firmware.load`, DSP-only changes hot,
a MAIN change by the cached coupled cold boot), and runs *tours*: `tests/patchkit_smoke/tours/smoke.toml` (boot to the
readiness contract, track and pattern change, audition, play/stop with the sequencer running, machine menu open and
close, save project and reload, a seeded random-input stress) and the tests of the modules in the set
(`modules/*/*/tests/*.toml`). Every segment starts from `restart`, so segments do not depend on each other.

After every input and every segment the harness asserts: no `fault` (status.cf.error, GUEST_FAULT), no DSP halt, no
main-loop stall (status.heartbeat, the controller's `stall` events), and its own checks: `expect_frame` against the
golden SHA-256 (tests/patchkit_smoke/goldens.json, keyed `DEV-FW/<module set>/<segment>/<checkpoint>`; **only hashes are
committed**, the PNGs go to out/runs/patchkit/frames/), `expect_pcm_db` over the PCM tap's 100 ms windows.

Step format (TOML `[[segment.step]]` / `[[step]]`, exactly one action key each; names are the panel names of
`BUTTONS`, case-insensitive; numbers are raw codes):

    tap = "TRIG 1"                 press and release (hold_ms, default 20)
    tap = ["TRK", "TRIG 2"]        chord: the first key is held while the others are tapped
    hold = "PLAY"  ms = 500        press, run ms, release
    turn = "ENC A"  by = 2         encoder rotation (ENC A..H, LEVEL); negative = left
    wait_ms = 300                  run that many emulated milliseconds
    expect_frame = "name"          hash of the OLED frame against the golden (PNG to out/); `differs_from = "other"`
    expect_pcm_db = {min = -60, max = -3}   peak 100 ms-window RMS (dBFS) since the last input started
    expect_no_fault = true         assert the health now (it is also asserted after every input)
    expect_mem = {addr = 0x4265338c, mask = 0xff00, eq = 0x0700}   a ColdFire word (mem.cf.peek) under a mask
    expect_dsp = {addr = 0x255970, hex = "07"}                     DSP memory bytes (dsp.peek) from addr
    stress = {seed = 1, actions = 40}       seeded random inputs, health after each

`device = "dt2"` on a step limits it to one device. A module test file has `[test]` (`name`, `devices`, `quick`) and
`[[step]]`.

The daemon comes from `tools/native_emu.py --daemon --build-only` for the device's generated core (cached; shares the
machine through benchlock). Ready inputs must be fresh: SOURCE.json must say `intc_force: false` and name the base .syx.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import card_check  # noqa: E402
from digi_client import Emu, EmuError  # noqa: E402

MS = 132_000  # ColdFire instructions per emulated millisecond (the oracle clock)
OUT = ROOT / "out" / "runs" / "patchkit"
GOLDENS = ROOT / "tests" / "patchkit_smoke" / "goldens.json"
TOURS = ROOT / "tests" / "patchkit_smoke" / "tours"
DEFAULT_MATRIX = ROOT / "tests" / "patchkit_smoke" / "matrix.toml"
DEFAULT_STALL_MS = 1000

# firmware: (device, version) -> base .syx and the ready directory (override: DIGI_PK_READY_<DEV>_<VER>)
FIRMWARE: dict[tuple[str, str], dict[str, str]] = {
    ("dt2", "1.17"): {
        "syx": "Digitakt_II_OS1.17.syx",
        "ready": "out/ready-pk-dt2-117",
    },
    ("dn2", "1.12"): {
        "syx": "Digitone_II_OS1.12.syx",
        "ready": "out/ready-fix-dn2-112",
    },
    ("dt2", "1.16"): {
        "syx": "Digitakt_II_OS1.16.syx",
        "ready": "out/ready-pk-dt2-116",
    },
    ("dn2", "1.11"): {
        "syx": "Digitone_II_OS1.11.syx",
        "ready": "out/ready-pk-dn2-111",
    },
}

BUTTONS: dict[str, int] = {
    "PAGE TRIG": 1,
    "SRC": 2,
    "SYN": 2,
    "FLTR": 3,
    "AMP": 4,
    "FX": 5,
    "MOD": 6,
    "PRESET": 7,
    "SETTINGS": 8,
    "TEMPO": 9,
    "YES": 10,
    "UP": 11,
    "NO": 12,
    "LEFT": 13,
    "DOWN": 14,
    "RIGHT": 15,
    "TRK": 16,
    "FUNC": 17,
    "KEYBOARD": 18,
    "RECORD": 19,
    "PLAY": 20,
    "STOP": 21,
    "PAGE": 22,
    "PTN": 23,
    "SONG": 24,
    "LEVEL PUSH": 49,
    **{f"TRIG {n}": 24 + n for n in range(1, 17)},
    **{f"PUSH {c}": 41 + i for i, c in enumerate("ABCDEFGH")},
}
ENCODERS: dict[str, int] = {
    **{f"ENC {c}": 1 + i for i, c in enumerate("ABCDEFGH")},
    "LEVEL": 9,
}


def _norm(name: str) -> str:
    return re.sub(r"[\s_]+", " ", name.strip().upper())


def button(name: str | int) -> int:
    if isinstance(name, int):
        return name
    try:
        return BUTTONS[_norm(name)]
    except KeyError:
        raise HarnessError(f"unknown button {name!r}") from None


def encoder(name: str | int) -> int:
    if isinstance(name, int):
        return name
    try:
        return ENCODERS[_norm(name)]
    except KeyError:
        raise HarnessError(f"unknown encoder {name!r}") from None


class HarnessError(RuntimeError):
    """A setup problem (missing input, bad step): not a firmware verdict."""


class LoadFailure(Exception):
    """The image did not come up (cold boot fault, loader refusal): a verdict on the image."""


@dataclass
class Cell:
    device: str
    firmware: str
    modules: list[str] = field(default_factory=list)
    stall_ms: int = DEFAULT_STALL_MS

    @property
    def modset(self) -> str:
        return "+".join(sorted(self.modules)) or "stock"

    @property
    def name(self) -> str:
        return f"{self.device}-{self.firmware}:{self.modset}"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9.+-]+", "_", self.name)

    @property
    def golden_prefix(self) -> str:
        return f"{self.device}-{self.firmware}/{self.modset}"


def parse_cell(text: str) -> Cell:
    """`dt2-1.17:fx/master-gain,test/cf-demo` (no module part: stock)."""
    head, _, mods = text.partition(":")
    device, _, firmware = head.partition("-")
    return Cell(device, firmware, [m for m in mods.split(",") if m and m != "stock"])


def load_matrix(path: Path) -> list[Cell]:
    data = tomllib.loads(path.read_text())
    cells = []
    for c in data.get("cell", []):
        cells.append(
            Cell(
                c["device"],
                c["firmware"],
                list(c.get("modules", [])),
                int(c.get("stall_ms", DEFAULT_STALL_MS)),
            )
        )
    return cells


# ----- inputs ---------------------------------------------------------------------------------------------------


def firmware_inputs(cell: Cell) -> tuple[Path, Path]:
    """(base .syx, ready directory), checked: the ready state must be fresh and belong to the .syx."""
    try:
        spec = FIRMWARE[(cell.device, cell.firmware)]
    except KeyError:
        raise HarnessError(
            f"no firmware entry for {cell.device} {cell.firmware}"
        ) from None
    syx = ROOT / spec["syx"]
    env = os.environ.get(
        f"DIGI_PK_READY_{cell.device.upper()}_{cell.firmware.replace('.', '')}"
    )
    ready = Path(env) if env else ROOT / spec["ready"]
    if not syx.is_file():
        raise HarnessError(f"{syx} is missing")
    try:
        source = json.loads((ready / "SOURCE.json").read_text())
    except (OSError, ValueError):
        raise HarnessError(
            f"{ready}: no SOURCE.json; build it with `uv run python tools/ready.py {cell.device} "
            f"--syx {syx.name} --out {ready} --force`"
        ) from None
    if source.get("intc_force") is not False:
        raise HarnessError(
            f"{ready}: built before the INTC-force fix (no intc_force: false); rebuild with tools/ready.py --force"
        )
    if source.get("syx_sha256") != hashlib.sha256(syx.read_bytes()).hexdigest():
        raise HarnessError(f"{ready} was built from another .syx than {syx.name}")
    for name in (
        "digi-audio-dn2-image.bin",
        "digi-audio-m5.snap",
        "digi-audio-m5.snap.dsp",
    ):
        if not (ready / name).is_file():
            raise HarnessError(f"{ready}/{name} is missing")
    return syx, ready


_BINARIES: dict[str, Path] = {}


def daemon_binary(device: str, syx: Path) -> Path:
    """The `digi-emud` for the device's generated core (built when stale; the build holds benchlock)."""
    override = os.environ.get(f"DIGI_EMUD_{device.upper()}") or os.environ.get(
        "DIGI_EMUD"
    )
    if override:
        return Path(override)
    if device not in _BINARIES:
        r = subprocess.run(
            [
                sys.executable,
                str(HERE / "native_emu.py"),
                "--daemon",
                "--build-only",
                str(syx),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode or not lines or not Path(lines[-1]).is_file():
            raise HarnessError(
                f"cannot build digi-emud for {device}:\n{(r.stderr or r.stdout)[-1500:]}"
            )
        _BINARIES[device] = Path(lines[-1])
    return _BINARIES[device]


def patched_image(cell: Cell, syx: Path) -> Path:
    """The stock .syx, or the patch of the cell's modules (out/patch/pk-*.syx)."""
    if not cell.modules:
        return syx
    out = ROOT / "out" / "patch" / f"pk-{cell.slug}.syx"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            sys.executable,
            str(HERE / "patchkit.py"),
            "patch",
            "--firmware",
            str(syx),
            "--modules",
            ",".join(cell.modules),
            "-o",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise HarnessError(
            f"patch failed for {cell.name}:\n{(r.stderr or r.stdout)[-1500:]}"
        )
    return out


# ----- goldens --------------------------------------------------------------------------------------------------


class Goldens:
    """Frame hashes (sha256 hex of the 1024 raw OLED bytes) keyed `DEV-FW/modset/segment/checkpoint`."""

    def __init__(self, path: Path = GOLDENS):
        self.path = path
        try:
            self.data: dict[str, str] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True

    def save(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
            self.dirty = False


# ----- the session ----------------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class Failure(Exception):
    """The firmware misbehaved (fault, halt, stall): the segment stops."""


class Session:
    """One daemon on one cell: spawn, load the image, run segments from `restart`."""

    def __init__(
        self,
        cell: Cell,
        goldens: Goldens,
        update: bool = False,
        log: Path | None = None,
        run_name: str | None = None,
    ):
        self.cell, self.goldens, self.update = cell, goldens, update
        self.syx, self.ready = firmware_inputs(cell)
        self.log_path = log
        self.run_name = run_name or cell.slug
        self.emu: Emu | None = None
        self.events: list[dict[str, Any]] = []
        self.checks: list[Check] = []
        self.segment = ""
        self.pcm_mark = 0
        self.frames: dict[str, str] = {}
        self.load_report: dict[str, Any] = {}
        self.timings: dict[str, float] = {}
        self.card: str | None = None
        self.image: Path | None = None

    # -- lifecycle
    def log(self, **entry: Any) -> None:
        if self.log_path is not None:
            with self.log_path.open("a") as f:
                f.write(
                    json.dumps({"t": round(time.time(), 3), **entry}, default=str)
                    + "\n"
                )

    def open(self, image: Path | None = None) -> None:
        cell = self.cell
        started = time.monotonic()
        exe = daemon_binary(cell.device, self.syx)
        run_dir = OUT / f"run-{self.run_name}"
        self.emu = Emu.spawn_coupled(
            self.syx, self.ready, timeout=300, binary=exe, run_dir=run_dir
        )
        emu = self.emu
        if emu.status().get("audio") is None:
            raise HarnessError("this digi-emud has no coupled audio")
        emu.subscribe()
        if cell.device == "dt2":
            samples = OUT / "samples"
            card_check.make_samples(samples)
            self.card = emu.call("card.load", dir=str(samples))["card"]
        self.timings["spawn_s"] = time.monotonic() - started
        self.load(image or patched_image(cell, self.syx))

    def load(self, image: Path, settle: bool = False) -> dict[str, Any]:
        """`firmware.load` the image (hot DSP, or the cached coupled cold boot); LoadFailure when it does not
        come up. Segments start from `restart` themselves; SETTLE does one here too."""
        emu = self.emu
        assert emu is not None
        started = time.monotonic()
        params: dict[str, Any] = {
            "syx": str(image),
            "force": True,
            "auto_no": True,
            "start": "auto",
        }
        if self.card:
            params["card"] = self.card
        try:
            report = emu.call("firmware.load", **params)
        except EmuError as e:
            raise LoadFailure(f"firmware.load {image.name}: {e}") from e
        self.image = image
        self.load_report = report
        self.timings["load_s"] = time.monotonic() - started
        emu.watchdog(self.cell.stall_ms)
        if settle:
            self.reset()
        return report

    def close(self) -> None:
        if self.emu is not None:
            try:
                self.emu.__exit__(None, None, None)
            finally:
                self.emu = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- state and health
    def reset(self) -> None:
        """Back to the ready state, modal closed, PCM tap fresh: the start of every segment."""
        emu = self.emu
        assert emu is not None
        emu.restart(auto_no=True)
        emu.drain_events()
        self.run_ms(300)
        emu.pcm_start()
        self.pcm_mark = 0
        self.health("ready")
        status = emu.status()
        cf = status["cf"]
        ok = bool(cf["ready"]) and cf["phase"] == "ready" and cf["main_ui_reached"]
        ok = ok and cf.get("filesystem_verified") is not False
        self.check(
            "ready contract",
            ok,
            f"phase={cf['phase']} main_ui={cf['main_ui_reached']} fs={cf.get('filesystem_verified')}",
        )

    def run_ms(self, ms: float) -> None:
        emu = self.emu
        assert emu is not None
        n = int(ms * MS)
        while n > 0:
            chunk = min(n, 200_000_000)
            try:
                emu.step(chunk, timeout_wall_s=300)
            except EmuError as e:
                if e.code == 6:
                    self.events.extend(emu.drain_events())
                    raise Failure(f"fault: {e}") from e
                raise
            n -= chunk

    def health(self, where: str) -> None:
        """Fault, DSP halt and stall now? Raises Failure (the segment stops)."""
        emu = self.emu
        assert emu is not None
        status = emu.status()
        self.events.extend(e for e in emu.drain_events() if e["kind"] != "frame")
        cf, audio, beat = status["cf"], status.get("audio") or {}, status["heartbeat"]
        if cf.get("error") or cf["phase"] == "fault":
            raise Failure(f"fault at {where}: {cf.get('error')} pc={cf['pc']:#x}")
        if audio.get("halted"):
            raise Failure(f"DSP halt at {where}: {audio['halted']}")
        if beat["stalled"] or beat["stall_count"]:
            raise Failure(
                f"stall at {where}: mainloop_hits={beat['mainloop_hits']} stall_count={beat['stall_count']} "
                f"(threshold {beat['stall_ms']} ms)"
            )
        if any(e["kind"] == "lagged" for e in self.events):
            raise Failure(f"event queue lagged at {where}")

    def wait_event(self, kind: str, timeout_s: float = 3.0) -> bool:
        """Events are written by a socket thread and can follow their reply: poll `status` until one of KIND is
        in `events` (True) or TIMEOUT_S passes."""
        emu = self.emu
        assert emu is not None
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if any(e["kind"] == kind for e in self.events):
                return True
            emu.status()
            self.events.extend(e for e in emu.drain_events() if e["kind"] != "frame")
            time.sleep(0.05)
        return any(e["kind"] == kind for e in self.events)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(
            Check(f"{self.segment}/{name}" if self.segment else name, ok, detail)
        )
        self.log(check=name, segment=self.segment, ok=ok, detail=detail)

    # -- actions
    def mark_pcm(self) -> None:
        emu = self.emu
        assert emu is not None
        self.pcm_mark = len(emu.pcm_report().get("window_rms") or [])

    def do_tap(self, keys: str | int | list[Any], hold_ms: float = 20) -> None:
        emu = self.emu
        assert emu is not None
        self.mark_pcm()
        seq = keys if isinstance(keys, list) else [keys]
        held = [button(k) for k in seq[:-1]]
        for code in held:
            emu.button(code, True)
            self.run_ms(5)
        emu.tap(button(seq[-1]), hold=int(hold_ms * MS))
        self.run_ms(hold_ms + 5)
        for code in reversed(held):
            emu.button(code, False)
            self.run_ms(5)
        self.health(f"tap {keys}")

    def do_hold(self, key: str | int, ms: float) -> None:
        emu = self.emu
        assert emu is not None
        self.mark_pcm()
        code = button(key)
        emu.button(code, True)
        self.run_ms(ms)
        emu.button(code, False)
        self.run_ms(5)
        self.health(f"hold {key}")

    def do_turn(self, enc: str | int, by: int) -> None:
        emu = self.emu
        assert emu is not None
        self.mark_pcm()
        emu.turn(encoder(enc), by)
        self.run_ms(10)
        self.health(f"turn {enc} {by}")

    # -- parameter read-back and knob helpers (DT2: finding 02, "A machine is eight fixed engine slots")
    SOUND_PTRS = 0x8000470C  # 16 pointers to the tracks' Sound objects; the parameter array is at +0x14
    TX_FRAME = 0x80005348  # the ColdFire -> DSP frame; machine parameter words at 0xda + 0x60 * track + 2 * (idx - 25)

    def _u16(self, addr: int) -> int:
        assert self.emu is not None
        word = self.emu.peek32(addr & ~3) or 0
        return ((word >> 16) if addr % 4 == 0 else word) & 0xFFFF

    def mirror(self, idx: int, track: int = 0) -> int:
        """The s16 of parameter slot IDX (25..34: the machine's engine slots) of TRACK's Sound object, as unsigned."""
        assert self.emu is not None
        sound = self.emu.peek32(self.SOUND_PTRS + 4 * track) or 0
        return self._u16(sound + 0x14 + 2 * idx)

    def tx_word(self, idx: int, track: int = 0) -> int:
        """The word of engine slot IDX in the TX frame the DSP reads (valid after the next frame that follows a change)."""
        return self._u16(self.TX_FRAME + 0xDA + 0x60 * track + 2 * (idx - 25))

    def turn_burst(self, enc: str | int, detents: int, events: int, gap_ms: float = 2) -> None:
        """EVENTS encoder events of DETENTS each, GAP_MS apart. The encoder accelerates by the time between events and
        a value moves about one raw step per slow event (finding 03): a slow single event can change nothing."""
        emu = self.emu
        assert emu is not None
        self.mark_pcm()
        for _ in range(events):
            emu.turn(encoder(enc), detents)
            self.run_ms(gap_ms)
        self.run_ms(100)
        self.health(f"burst {enc} {detents}x{events}")

    def turn_to(
        self,
        enc: str | int,
        idx: int,
        target: int,
        tol: int = 0x300,
        track: int = 0,
        rounds: int = 80,
    ) -> int:
        """Turn ENC until slot IDX reads within TOL of TARGET (bursts of 3 events, sign by the error); returns the
        value reached. Raises Failure when ROUNDS bursts do not get there."""
        for _ in range(rounds):
            v = self.mirror(idx, track)
            err = target - v
            if abs(err) <= tol:
                return v
            self.turn_burst(enc, 4 if err > 0 else -4, 3 if abs(err) < 0x1000 else 8)
        v = self.mirror(idx, track)
        raise Failure(f"turn_to {enc}: slot {idx} is {v:#x}, wanted {target:#x}")

    def frame(self) -> tuple[str, bytes]:
        assert self.emu is not None
        _, raw = self.emu.frame()
        return hashlib.sha256(raw).hexdigest(), raw

    def expect_frame(self, name: str, differs_from: str | None = None) -> None:
        digest, raw = self.frame()
        key = f"{self.cell.golden_prefix}/{self.segment}/{name}"
        png_dir = OUT / "frames" / self.cell.slug
        png_dir.mkdir(parents=True, exist_ok=True)
        from digi_client import oled_png

        (png_dir / f"{self.segment}-{name}.png").write_bytes(oled_png(raw))
        self.frames[f"{self.segment}/{name}"] = digest
        if differs_from is not None:
            other = self.frames.get(f"{self.segment}/{differs_from}")
            self.check(
                f"{name} differs from {differs_from}",
                other is not None and other != digest,
            )
        if self.update:
            self.goldens.set(key, digest)
            self.check(f"frame {name}", True, f"golden recorded {digest[:12]}")
            return
        want = self.goldens.get(key)
        if want is None:
            self.check(
                f"frame {name}", False, f"no golden for {key} (run with --update)"
            )
        else:
            self.check(
                f"frame {name}", want == digest, f"got {digest[:12]} want {want[:12]}"
            )

    def expect_pcm_db(self, lo: float | None, hi: float | None) -> None:
        emu = self.emu
        assert emu is not None
        windows = (emu.pcm_report().get("window_rms") or [])[self.pcm_mark :]
        peak = max(windows, default=0.0)
        db = 20 * math.log10(peak) if peak > 0 else -math.inf
        ok = (lo is None or db >= lo) and (hi is None or db <= hi)
        self.check(
            "pcm",
            ok,
            f"peak window {db:.1f} dBFS over {len(windows)} windows (want {lo}..{hi})",
        )

    def expect_mem(self, want: dict[str, Any]) -> None:
        emu = self.emu
        assert emu is not None
        addr, mask = int(want["addr"]), int(want.get("mask", 0xFFFFFFFF))
        got = emu.peek32(addr)
        ok = got is not None and got & mask == int(want["eq"])
        self.check(
            f"mem {addr:#x}",
            ok,
            f"got {'unmapped' if got is None else f'{got & mask:#x}'} want {int(want['eq']):#x} (mask {mask:#x})",
        )

    def expect_dsp(self, want: dict[str, Any]) -> None:
        emu = self.emu
        assert emu is not None
        raw = bytes.fromhex(want["hex"])
        got = emu.dsp_peek(int(want["addr"]), len(raw))["hex"]
        self.check(
            f"dsp {int(want['addr']):#x}",
            got == raw.hex(),
            f"got {got} want {raw.hex()}",
        )

    def stress(self, seed: int, actions: int) -> None:
        rng = random.Random(seed)
        keys = (
            [c for c in range(1, 25)]
            + [25 + i for i in range(16)]
            + [41 + i for i in range(8)]
        )
        trace = []
        for i in range(actions):
            roll = rng.random()
            if roll < 0.55:
                code = rng.choice(keys)
                trace.append(f"tap {code}")
                self.do_tap(code, hold_ms=rng.choice([10, 20, 40]))
            elif roll < 0.8:
                enc, by = rng.randint(1, 9), rng.choice([-3, -2, -1, 1, 2, 3])
                trace.append(f"turn {enc} {by}")
                self.do_turn(enc, by)
            elif roll < 0.9:
                a, b = rng.choice([16, 17, 23, 22]), rng.choice(keys)
                trace.append(f"chord {a}+{b}")
                self.do_tap([a, b])
            else:
                ms = rng.choice([20, 60, 150])
                trace.append(f"wait {ms}")
                self.run_ms(ms)
            if i % 8 == 7:
                self.health(f"stress step {i} ({trace[-1]})")
        self.log(stress_seed=seed, trace=trace)
        self.check(f"stress {actions} inputs (seed {seed})", True)

    # -- steps
    def run_steps(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            devices = step.get("device")
            if devices and self.cell.device not in (
                devices if isinstance(devices, list) else [devices]
            ):
                continue
            self.log(step=step)
            if "tap" in step:
                self.do_tap(step["tap"], step.get("hold_ms", 20))
            elif "hold" in step:
                self.do_hold(step["hold"], step.get("ms", 500))
            elif "turn" in step:
                self.do_turn(step["turn"], step.get("by", 1))
            elif "wait_ms" in step:
                self.run_ms(step["wait_ms"])
                self.health(f"wait {step['wait_ms']} ms")
            elif "expect_frame" in step:
                self.expect_frame(step["expect_frame"], step.get("differs_from"))
            elif "expect_pcm_db" in step:
                want = step["expect_pcm_db"]
                self.expect_pcm_db(want.get("min"), want.get("max"))
            elif "expect_mem" in step:
                self.expect_mem(step["expect_mem"])
            elif "expect_dsp" in step:
                self.expect_dsp(step["expect_dsp"])
            elif "expect_no_fault" in step:
                self.health("expect_no_fault")
                self.check("no fault/halt/stall", True)
            elif (
                "_call" in step
            ):  # tests only: a callable(session) that breaks or inspects the guest
                step["_call"](self)
            elif "stress" in step:
                s = step["stress"]
                self.stress(int(s.get("seed", 1)), int(s.get("actions", 40)))
            else:
                raise HarnessError(f"step without an action: {step}")

    def run_segment(self, name: str, steps: list[dict[str, Any]]) -> bool:
        """One segment from a fresh `restart`; True when every check passed."""
        self.segment = name
        before = len(self.checks)
        started = time.monotonic()
        try:
            self.reset()
            self.run_steps(steps)
            self.health("end")
        except Failure as e:
            self.check("health", False, str(e))
            self.log(failure=str(e), events=self.events[-20:])
        except (EmuError, HarnessError) as e:
            self.check("error", False, str(e))
        self.log(segment=name, seconds=round(time.monotonic() - started, 2))
        self.segment = ""
        return all(c.ok for c in self.checks[before:])


# ----- tours ----------------------------------------------------------------------------------------------------


def smoke_segments(path: Path | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
    data = tomllib.loads((path or TOURS / "smoke.toml").read_text())
    return [(s["name"], s.get("step", [])) for s in data["segment"]]


def module_tests(
    modules: list[str], device: str, quick_only: bool
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    for mod in modules:
        for f in sorted((ROOT / "modules" / mod / "tests").glob("*.toml")):
            data = tomllib.loads(f.read_text())
            t = data.get("test", {})
            if device not in t.get("devices", ["dt2", "dn2"]):
                continue
            if quick_only and not t.get("quick", True):
                continue
            yield (
                f"{mod.replace('/', '.')}.{t.get('name', f.stem)}",
                data.get("step", []),
            )


def run_cell(
    cell: Cell,
    goldens: Goldens,
    update: bool = False,
    tests: str = "all",
    seed: int | None = None,
    stress_actions: int | None = None,
    log_dir: Path = OUT,
) -> tuple[bool, str, Session]:
    """Run a cell; (passed, one-line summary, session). The session's `checks` hold the detail."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = log_dir / f"{stamp}-{cell.slug}.jsonl"
    started = time.monotonic()
    session = Session(cell, goldens, update=update, log=log)
    failed: list[str] = []
    try:
        with session:
            session.open()
            segments: list[tuple[str, list[dict[str, Any]]]] = []
            if tests in ("all", "smoke"):
                for name, steps in smoke_segments():
                    steps = [dict(s) for s in steps]
                    for s in steps:
                        if "stress" in s:
                            s["stress"] = {
                                **s["stress"],
                                **({"seed": seed} if seed is not None else {}),
                                **(
                                    {"actions": stress_actions}
                                    if stress_actions
                                    else {}
                                ),
                            }
                    segments.append((name, steps))
            if tests in ("all", "quick"):
                segments.extend(
                    module_tests(cell.modules, cell.device, tests == "quick")
                )
            for name, steps in segments:
                if not session.run_segment(name, steps):
                    failed.append(name)
    except HarnessError as e:
        return False, f"FAIL {cell.name}: setup: {e}", session
    except LoadFailure as e:
        return (
            False,
            f"FAIL {cell.name}: load: {e} [log {log.relative_to(ROOT)}]",
            session,
        )
    finally:
        if update:
            goldens.save()
    secs = time.monotonic() - started
    n = len(session.checks)
    if failed:
        first = next(c for c in session.checks if not c.ok)
        return (
            False,
            f"FAIL {cell.name}: {len(failed)} segment(s) failed ({', '.join(failed)}); first: {first.name}: "
            f"{first.detail} [{secs:.0f} s, log {log.relative_to(ROOT)}]",
            session,
        )
    return (
        True,
        f"PASS {cell.name}: {n} checks in {secs:.0f} s (load {session.timings.get('load_s', 0):.1f} s, "
        f"{session.load_report.get('start_used', '?')}) [log {log.relative_to(ROOT)}]",
        session,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    r.add_argument(
        "--cell",
        action="append",
        default=[],
        help="DEV-FW:mod,mod (stock when no modules)",
    )
    r.add_argument(
        "--update",
        action="store_true",
        help="record the frame goldens instead of comparing",
    )
    r.add_argument("--tests", choices=("smoke", "quick", "all"), default="all")
    r.add_argument("--seed", type=int)
    r.add_argument("--stress-actions", type=int)
    sub.add_parser("list").add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    a = p.parse_args(argv)
    cells = [parse_cell(c) for c in getattr(a, "cell", [])] or load_matrix(a.matrix)
    if a.cmd == "list":
        for c in cells:
            print(c.name)
        return 0
    goldens = Goldens()
    rc = 0
    for cell in cells:
        ok, line, _ = run_cell(
            cell, goldens, a.update, a.tests, a.seed, a.stress_actions
        )
        print(line, flush=True)
        rc |= not ok
    return rc


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main(sys.argv[1:]))
