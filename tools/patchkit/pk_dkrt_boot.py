#!/usr/bin/env python3
"""Boot-time loading of run-time modules in the emulator (docs/findings/37-boot-load.md).

    uv run python -m tools.patchkit.pk_dkrt_boot proof [--image dt2-1.17|dt2-1.16|dn2-1.12|dn2-1.11] [--quick] [--keep DIR] [--only a,b]

The runtime loads the modules of the selection record (two sectors at the end of the card's store range) at boot, from a hook just
before the MainScreenView constructor. `proof` builds the images and cards and boots them in the coupled emulator (cold boots of the
patched firmware with the image's own DSP code; a restart is the emulator's RCM soft-reset model):

  equiv    a selection loaded at boot plays like the same module patched in: rows, DSP bytes, voice row, machine row, PCM
  restart  select through the mailbox, `dkrt_restart`, the next boot loads it; select nothing, restart, it is gone
  cut      the card states a power cut can leave between the two writes of a selection (and a torn copy)
  safe     a module that resets the machine in `init`: boots 1 and 2 raise the counter, boot 3 is safe mode; the safe-once flag
  watch    write-watch and flush check over every boot, and the card sectors the runtime wrote

Not covered **[O]**: the safe-mode key (the queue peek is untested on a device and the emulator delivers no key before the main
OS runs), `dkrt_restart` refusing during a card transfer (the emulator's eSDHC never reports one busy), panel edits surviving the
restart (they do not: finding 40; `DKRT_SAVE_MODE` selects the firmware's save sequence).

Python API for the scenario suite (lane C builds on it; everything below is importable, nothing runs at import)::

    from tools.patchkit import pk_dkrt_boot as B
    env = B.Env("dt2-1.17", work_dir)                  # the files of one image: folder, cards, binaries
    img = env.build_image("core/runtime,core/dynreg,core/dyndsp")   # a patched firmware for the static module set S -> Img
    env.pack("test/dyn-pluck", "pluck-d.dkm")           # a .dkm into the card folder (digikit/modules/)
    card = env.card(["modules/pluck-d.dkm"], counter=0, seq=1)      # a card image holding that selection record
    with env.boot(img, card, tag="a") as rig:           # a coupled daemon, cold-booted on that card: the boot hook has run
        rig.boot_info()        # dict: safe, reason, counter, seq, copy, count, loaded, results[], ui_reached, dsp_wait
        rig.voice_rows()       # [(type, pc)] of the DSP voice registry (type 0xffffffff: free)
        rig.machine_listed(18) # is the machine row of that type named
        rig.sel_records()      # the two record copies as the card holds them now: [dict|None, dict|None]
        rig.set_selection(["modules/x.dkm"])   # dkrt_selection_set through the mailbox (returns its result)
        rig.restart()          # dkrt_restart, then the emulator's soft reset; returns when the new boot is ready
        rig.select_type(18, 7) # the machine list on the panel (DT2); rig.note(wav, B.M.TRIG_FRAME) plays TRIG 1, returns the PCM
    B.make_record(paths, seq=1, counter=0, flags=0)     # the 512 bytes of one record copy (the layout of docs/design/digikit-store.md)
    B.write_record_sectors(card, copy_a, copy_b)        # raw sector images over the two record sectors of a card file

The record layout is in docs/design/digikit-store.md; the runtime's own view of the same bytes is `dkrt_sdk::sel`.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from tools.coldfire import arena_safety  # noqa: E402
from tools.control.digi_client import Emu, EmuError  # noqa: E402
from tools.firmware import config  # noqa: E402
from tools.patchkit import pk_dkrt_machine as M  # noqa: E402
from tools.patchkit import pk_harness as H  # noqa: E402
from tools.patchkit.pk_dkrt import (  # noqa: E402
    DEVICE_SYX,
    Tools,
    build_card,
    patch,
)

SEL_MAGIC, SEL_VERSION = 0x444B534C, 1
SEL_MAX, SEL_PATH = 8, 56
STORE_FIRST, STORE_SECTORS = 0x38000, 0x6000
SECTOR_A = STORE_FIRST + STORE_SECTORS - 2
SECTOR_B = STORE_FIRST + STORE_SECTORS - 1
CMD_LOAD, CMD_SELECT, CMD_RESTART, CMD_SELECT_ONE = (
    1,
    3,
    4,
    5,
)  # command 2 was CMD_UNLOAD (archived)
SAFE_NAMES = {0: "none", 1: "counter", 2: "key", 3: "flag"}
BI_FIELDS = [
    "magic",
    "version",
    "safe",
    "reason",
    "counter",
    "seq",
    "copy",
    "count",
    "loaded",
    "ui_reached",
]
SETTLE_MS = int(
    os.environ.get("DIGIKIT_SETTLE_MS", "1500")
)  # emulated ms the first UI-loop passes run before a scenario looks (wait_dsp then waits for a module's DSP part)
E_SAFE_MODE = -44
E_LOADING = 1
RT = "core/runtime,core/dynreg,core/dyndsp"
FREE_TYPE = M.FREE_TYPE


class Fail(RuntimeError):
    pass


def make_record(
    paths: list[str], seq: int = 1, counter: int = 0, flags: int = 0
) -> bytes:
    """One copy of the selection record (docs/design/digikit-store.md)."""
    if len(paths) > SEL_MAX or any(len(p) >= SEL_PATH or not p for p in paths):
        raise Fail("selection: at most 8 paths of 1 to 55 characters")
    b = bytearray(512)
    struct.pack_into(
        ">IIIIII", b, 0, SEL_MAGIC, SEL_VERSION, seq, counter, flags, len(paths)
    )
    for i, p in enumerate(paths):
        b[32 + i * SEL_PATH : 32 + i * SEL_PATH + len(p)] = p.encode()
    struct.pack_into(">I", b, 508, zlib.crc32(bytes(b[:508])))
    return bytes(b)


def parse_record(b: bytes) -> dict[str, Any] | None:
    """A valid record as a dict, or None (magic, version, CRC, count, NUL-terminated paths)."""
    if len(b) != 512:
        return None
    magic, ver, seq, counter, flags, count = struct.unpack_from(">IIIIII", b, 0)
    if (
        magic != SEL_MAGIC
        or ver != SEL_VERSION
        or struct.unpack_from(">I", b, 508)[0] != zlib.crc32(b[:508])
        or count > SEL_MAX
    ):
        return None
    paths = []
    for i in range(count):
        raw = b[32 + i * SEL_PATH : 32 + (i + 1) * SEL_PATH]
        if 0 not in raw or raw[0] == 0:
            return None
        paths.append(raw.split(b"\0")[0].decode())
    return {"seq": seq, "counter": counter, "flags": flags, "paths": paths}


def write_record_sectors(card: Path, a: bytes | None, b: bytes | None) -> None:
    """Write raw 512-byte sector images over the two record sectors of a card image file (None leaves one alone)."""
    with open(card, "r+b") as f:
        for sector, data in ((SECTOR_A, a), (SECTOR_B, b)):
            if data is not None:
                f.seek(sector * 512)
                f.write(data)


def current(recs: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The record a boot uses: the valid copy with the higher sequence number (A when equal)."""
    a, b = recs
    if a is None or (b is not None and b["seq"] > a["seq"]):
        return b
    return a


def records_in(card_file: Path, overlay: bytes) -> list[dict[str, Any] | None]:
    """The two record copies of a card image with an overlay (the bytes the guest wrote) laid over it."""
    rec = 8 + 64 + 512
    secs: dict[int, bytearray] = {}
    with open(card_file, "rb") as f:
        for sector in (SECTOR_A, SECTOR_B):
            f.seek(sector * 512)
            secs[sector] = bytearray(f.read(512))
    for i in range(0, len(overlay), rec):
        sector = struct.unpack_from("<Q", overlay, i)[0]
        if sector in secs:
            mask, body = overlay[i + 8 : i + 72], overlay[i + 72 : i + rec]
            for j in range(512):
                if mask[j // 8] & (1 << (j % 8)):
                    secs[sector][j] = body[j]
    return [parse_record(bytes(secs[SECTOR_A])), parse_record(bytes(secs[SECTOR_B]))]


@dataclass
class Img:
    """A patched firmware and its plan."""

    syx: Path
    plan: dict
    modules: str


class Env:
    """The files of one device image: the card folder, the patched firmware, cards."""

    def __init__(self, image: str, work: Path, tools: Tools | None = None):
        self.image, self.work = image, work
        self.device = image[:3]
        self.tools = tools or Tools()
        work.mkdir(parents=True, exist_ok=True)
        self.folder = work / "plus_drive"
        (self.folder / "digikit" / "modules").mkdir(parents=True, exist_ok=True)
        self.stock = config.stock(DEVICE_SYX[image])
        self.cards = 0

    def build_image(self, modules: str, name: str = "img") -> Img:
        syx = self.work / f"{self.image}-{name}.syx"
        return Img(syx, patch(self.tools, self.stock, syx, modules), modules)

    def pack(self, module: str, file: str) -> Path:
        out = self.folder / "digikit" / "modules" / file
        self.tools.patchkit_run("pack", module, "--device", self.device, "-o", out)
        return out

    def card(
        self,
        selection: list[str] | None,
        counter: int = 0,
        seq: int = 1,
        flags: int = 0,
        tag: str | None = None,
    ) -> Path:
        """A card image with the folder's store and, when SELECTION is not None, both copies of the record."""
        self.cards += 1
        path = self.work / f"card-{tag or self.cards}.img"
        path.unlink(missing_ok=True)
        build_card(self.tools, self.folder, path, self.stock, self.device)
        if selection is not None:
            rec = make_record(selection, seq, counter, flags)
            write_record_sectors(path, rec, rec)
        return path

    def boot(
        self,
        img: Img,
        card: Path,
        tag: str = "boot",
        watch: bool = False,
        extra_env: dict[str, str] | None = None,
        cf_only: bool = False,
        settle_ms: int | None = None,
    ) -> BootRig:
        # SETTLE_MS: emulated ms to run after the boot before the rig is handed out (default SETTLE_MS)
        # CF_ONLY: a daemon without DSP and audio (`CfBootRig`)
        # (the cold boot runs the image's own DSP code from the start, as a device does: a boot-time DSP load needs it)
        # DIGI_WRITE_WATCH_SEQ: one watch summary per emulator (the cold boot, each reset boot, the installed runtime)
        rig = (CfBootRig if cf_only else BootRig)(
            self,
            img,
            tag,
            card,
            watch,
            {
                "DIGI_WRITE_WATCH_SEQ": "1",
                **(extra_env or {}),
            },
        )
        if settle_ms is not None:
            rig.settle_ms = settle_ms
        return rig


class BootRig(M.Rig):
    """A coupled daemon cold-booted on a card; `Rig` of pk_dkrt_machine plus the boot-time views."""

    def __init__(
        self,
        env: Env,
        img: Img,
        tag: str,
        card: Path,
        watch: bool,
        extra_env: dict[str, str] | None,
    ):
        super().__init__(
            tag,
            env.image,
            img.modules,
            img.plan,
            img.syx,
            env.folder,
            env.work,
            watch,
            card,
            extra_env,
        )
        self.env, self.card_path = env, card
        self._lock: Any = None
        self.settle_ms = SETTLE_MS
        self.boot_sym = (
            M.sym(img.plan, "core/runtime", "DKRT_BOOT") if M.has_rt(img.plan) else 0
        )

    def __enter__(self) -> BootRig:
        # two rigs on one run directory (one firmware on one card) would share a socket and a boot cache: the second waits
        # (scenarios run side by side in threads and in processes)
        import fcntl

        H.OUT.mkdir(parents=True, exist_ok=True)
        self._lock = (H.OUT / f"run-{self.s.run_name}.lock").open("w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.start()
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        self.s.close()
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.close()  # closing the file drops the flock

    def __exit__(self, *exc: object) -> None:
        self._release()

    def start(self) -> None:  # the cold boot has run the hook; no mailbox traffic yet
        self.s.open(self.syx)
        self.s.segment = "boot"
        # the state at the ready point of the cold boot, before anything else runs: is the DSP part of the selection done?
        self.at_ready = self.boot_info()
        # when the cold boot is ready: DSP blocks since the boot (a block is 64 samples at 48 kHz) and whether the voice row is in
        self.frames_at_ready = self.frames()
        self.voice_at_ready = [r for r in self.voice_rows() if r[0] != FREE_TYPE]
        # DSP blocks from the ready point (the cold boot is ready, the UI loop runs) to the DSP part being done: a saved track plays as
        # type 0 until then. None: no DSP part (or already done at the ready point: 0).
        self.blocks_to_dsp: int | None = (
            0 if E_LOADING not in self.at_ready["results"] else None
        )
        n = 0
        while self.blocks_to_dsp is None and n < 3000:
            self.emu.step(88_000)
            n += 1
            if E_LOADING not in self.boot_info()["results"]:
                self.blocks_to_dsp = self.frames() - self.frames_at_ready
        self.s.reset()
        self.s.run_ms(self.settle_ms)
        self.wait_dsp()

    def wait_dsp(self, ms: int = 5000) -> None:
        """Run until no selected module is still `LOADING` (its DSP part follows the CF part over the first UI-loop passes)."""
        for _ in range(ms // 100):
            if E_LOADING not in self.boot_info()["results"]:
                return
            self.s.run_ms(100)

    def boot_info(self) -> dict[str, Any]:
        d = {k: self.w(self.boot_sym + 4 * i) for i, k in enumerate(BI_FIELDS)}
        res = []
        for i in range(SEL_MAX):
            v = self.w(self.boot_sym + 4 * (len(BI_FIELDS) + i))
            res.append(v - (1 << 32) if v >= 1 << 31 else v)
        d["results"] = res[: d["count"]] if d["count"] else res[:0]
        d["all_results"] = res
        d["dsp_wait"] = self.w(self.boot_sym + 4 * (len(BI_FIELDS) + SEL_MAX))
        d["reason_name"] = SAFE_NAMES.get(d["reason"], "?")
        return d

    def voice_rows(self) -> list[tuple[int, int]]:
        return [self.voice_row(k) for k in range(8)]

    def sel_records(self) -> list[dict[str, Any] | None]:
        """The two copies as the card holds them now (the overlay of the running daemon over the image)."""
        ov = self.work / f"{self.tag}.sel-overlay.bin"
        self.emu.call("card.overlay", path=str(ov))
        return records_in(self.card_path, ov.read_bytes())

    def card_store_writes(self) -> list[int]:
        """The sectors in the store range that the guest has written."""
        ov = self.work / f"{self.tag}.all-overlay.bin"
        self.emu.call("card.overlay", path=str(ov))
        return arena_safety.card_store_hits(ov)

    def set_selection(self, paths: list[str]) -> int:
        return self.post(CMD_SELECT, "\0".join(paths) + "\0\0")

    def restart(self, timeout_s: float = 900.0) -> None:
        """`dkrt_restart` (mailbox command), then the emulator's RCM soft-reset model: the controller cold-boots the same patched
        firmware on the card as the guest left it. Returns when the new boot is ready (the daemon is the same)."""
        self.emu.drain_events()
        self.emu.poke32(self.mb + M.MB_CMD, CMD_RESTART)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            try:
                self.emu.step(400_000)
            except Exception:  # noqa: BLE001  a cold boot runs on its worker: steps are refused until it is done
                time.sleep(0.5)
            for ev in self.emu.drain_events():
                if ev.get("kind") == "cpu.reset" and ev.get("restart") == "unsupported":
                    raise Fail("the emulator cannot restart this build")
                if ev.get("kind") == "firmware.loaded":
                    if not ev.get("ok"):
                        raise Fail(f"restart: the cold boot failed {ev}")
                    self.s.emu.watchdog(self.s.cell.stall_ms)
                    self.s.run_ms(1500)
                    self.wait_dsp()
                    return
        raise Fail(f"restart: no new boot after {timeout_s:.0f} s")

    def watch_summaries(self) -> dict[str, Any]:
        """Close the daemon and add up the write-watch summaries of every emulator it ran (cold boots included)."""
        self.s.close()
        events = flush = cpushl = n = 0
        for f in sorted(self.work.glob(f"{self.tag}.writewatch-*.summary.json")):
            d = json.loads(f.read_text())
            events += d["events"]
            flush += len(d["flush_hits"])
            cpushl += d["cpushl"]
            n += 1
        return {
            "emulators": n,
            "write_events": events,
            "flush_hits": flush,
            "cpushl": cpushl,
        }


class CfFolderSession(M.FolderSession):
    """A daemon without DSP and audio: the ColdFire alone. Its cold boot to the main panel takes about 6 s (the coupled one 10 to 20 s)
    and a second of emulated idle time costs almost nothing (the coupled daemon runs at about a quarter of real time). What the
    firmware does on the ColdFire is the same; what needs the DSP (a module's DSP part, voice rows, PCM) is not there."""

    def open(self, image: Path | None = None) -> None:
        exe = H.daemon_binary(self.cell.device, self.syx)
        self.emu = Emu.spawn(
            binary=exe,
            timeout=300,
            run_dir=H.OUT / f"run-{self.run_name}",
            env=self.env,
        )
        self.emu.subscribe()
        self.card = str(self.card_img)
        assert image is not None
        self.load(image)

    def load(self, image: Path, settle: bool = False) -> dict[str, Any]:
        emu = self.emu
        assert emu is not None and self.card is not None
        started = time.monotonic()
        try:
            report = emu.call(
                "firmware.load",
                syx=str(image),
                force=True,
                auto_no=True,
                start="auto",
                card=self.card,
            )
        except EmuError as e:
            raise H.LoadFailure(f"firmware.load {image.name}: {e}") from e
        self.image, self.load_report = image, report
        emu.watchdog(self.cell.stall_ms)
        # the boot job runs when the daemon is stepped; the panel is up when the main UI has been reached (a used card never runs
        # the DT2 filesystem check the coupled readiness contract waits for)
        for _ in range(400):
            emu.step(20_000_000)
            cf = emu.status()["cf"]
            if cf.get("error") or cf["phase"] == "fault":
                raise H.LoadFailure(f"cold boot: {cf.get('error')} pc={cf['pc']:#x}")
            if cf["main_ui_reached"]:
                break
        else:
            raise H.LoadFailure("cold boot: the main panel was not reached")
        emu.drain_events()
        self.timings["load_s"] = time.monotonic() - started
        return report

    def reset(self) -> None:
        raise Fail("a ColdFire-only daemon has no reset to the ready state")

    def mark_pcm(self) -> None:  # no PCM tap
        pass


class CfBootRig(BootRig):
    """`BootRig` on a ColdFire-only daemon (`CfFolderSession`): panel scripts, RAM views and the card; no DSP, no PCM, no restart.
    `boot_info()['results']` of a module with a DSP part still reads `LOADING`."""

    session_cls = CfFolderSession

    def start(self) -> None:
        self.s.open(self.syx)
        self.s.segment = "boot"
        self.at_ready = self.boot_info()
        self.s.run_ms(
            self.settle_ms
        )  # the first UI-loop passes, as the coupled rig settles

    def wait_dsp(self, ms: int = 5000) -> None:
        pass

    def voice_rows(self) -> list[tuple[int, int]]:
        raise Fail("no DSP in a ColdFire-only rig")

    def restart(self, timeout_s: float = 900.0) -> None:
        raise Fail("the soft-reset model is coupled only")


# ---- the proof ----------------------------------------------------------------------------------------------------------------


class Proof:
    def __init__(self, image: str, quick: bool, work: Path, only: set[str]):
        self.image, self.quick, self.work, self.only = image, quick, work, only
        self.env = Env(image, work)
        self.checks: list[tuple[str, bool, str]] = []
        self.report: dict[str, Any] = {"image": image, "work": str(work)}
        self.dt2 = image.startswith("dt2")
        self.watch_totals = {"emulators": 0, "write_events": 0, "flush_hits": 0}
        self.store_writes: set[int] = set()
        self.static = ""
        self.mods: list[str] = []
        self.run_img: Img | None = None

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}", flush=True)

    def finish(self, rig: BootRig) -> None:
        """Collect the card-store writes and the watch summaries of a rig that is done."""
        boot = rig.s.load_report.get(
            "boot"
        )  # the cold boot's ColdFire instruction count (absent when the ready pair was cached)
        if boot:
            self.report.setdefault("boot_icount", {})[rig.tag] = boot["icount"]
        try:
            self.store_writes |= set(rig.card_store_writes())
        except Exception as e:  # noqa: BLE001
            print(f"card.overlay: {e}", file=sys.stderr)
        w = rig.watch_summaries()
        for k in self.watch_totals:
            self.watch_totals[k] += w[k]

    def setup(self) -> None:
        e = self.env
        if self.dt2:
            e.pack("test/dyn-pluck", "m1.dkm")
            self.static = RT + ",machines/pluck"
            self.mods = ["modules/m1.dkm"]
        else:
            e.pack("test/dyn-dn2", "m0.dkm")
            e.pack("test/dyn-sine-dn2", "m1.dkm")
            self.static = M.MODULES_STATIC_DN2 + ",test/dn2-sine"
            self.mods = ["modules/m0.dkm", "modules/m1.dkm"]
        e.pack("test/dyn-fault", "fault.dkm")
        self.run_img = e.build_image(RT, "run")

    def rig(
        self,
        img: Img | None,
        card: Path,
        tag: str,
        extra_env: dict[str, str] | None = None,
    ) -> BootRig:
        assert img is not None
        return self.env.boot(img, card, tag, watch=True, extra_env=extra_env)

    # -- scenarios
    def equiv(self) -> None:
        e = self.env
        trig = M.TRIG_FRAME if self.dt2 else M.TRIG_FRAME_DN2
        run_env = (
            {}
            if self.dt2
            else {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_DYN_TYPE)}
        )
        ref_env = (
            {}
            if self.dt2
            else {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_STATIC_TYPE)}
        )
        ref_img = e.build_image(self.static, "ref")
        with self.rig(ref_img, e.card(None, tag="ref"), "ref", ref_env) as ref:
            if self.dt2:
                ref.select_type(M.PLUCK_STATIC_TYPE, 7)
            ref_pcm = ref.note(self.work / "ref.wav", trig)
            peak = float(abs(ref_pcm).max())
            self.check("the reference note is audible", peak > 0.01, f"peak {peak:.3f}")
            self.finish(ref)
        card = e.card(self.mods, tag="equiv")
        with self.rig(self.run_img, card, "equiv", run_env) as run:
            n = len(self.mods)
            info = run.boot_info()
            self.report["boot_info"] = info
            self.report["at_ready"] = run.at_ready
            self.check(
                "the selection loaded at boot: every module result is 0",
                info["count"] == n
                and info["results"] == [0] * n
                and info["loaded"] == n
                and info["safe"] == 0,
                f"{info['results']} dsp_wait {info['dsp_wait']}",
            )
            rec = current(run.sel_records())
            self.check(
                "the UI loop ran and the counter is back to 0",
                info["ui_reached"] == 1 and rec is not None and rec["counter"] == 0,
                str(rec),
            )
            used = [r for r in run.voice_rows() if r[0] != FREE_TYPE]
            self.check(
                "the voice row of the DSP module is claimed",
                len(used) == 1 and used[0][0] == 18,
                str([(hex(a), hex(b)) for a, b in used]),
            )
            idx = self.mods.index("modules/m1.dkm")
            sec = M.parse_dsp_section(
                (e.folder / "digikit/modules/m1.dkm").read_bytes()
            )
            assert self.run_img is not None
            base = run.info_word(3) + run.w(run.dsp + M.D_MOD_OFF + 4 * idx)
            want = M.relocate(sec, base, M.run_imports(run, self.run_img.plan))
            got = run.dsp_bytes(base, len(want))
            self.check(
                "the DSP area holds the relocated image byte for byte",
                got == want,
                f"{len(want)} bytes at {base:#x}",
            )
            self.check(
                "the machine row is named", run.machine_listed(18 if self.dt2 else 16)
            )
            if self.dt2:
                downs = run.select_type(M.PLUCK_DYN_TYPE, 7)
                self.check(
                    "the stock machine list shows it: the track takes type 18",
                    run.type_now() == M.PLUCK_DYN_TYPE,
                    f"downs {downs}",
                )
            pcm = run.note(self.work / "run.wav", trig)
            M.pcm_check(
                run, ref_pcm, pcm, self.report, "machine" if self.dt2 else "voice"
            )
            self.finish(run)

    def restart(self) -> None:
        e = self.env
        n = len(self.mods)
        with self.rig(self.run_img, e.card(None, tag="restart"), "restart") as rig:
            b0 = rig.boot_info()
            self.check(
                "a card with no record boots with no module",
                b0["count"] == 0
                and b0["loaded"] == 0
                and b0["safe"] == 0
                and b0["seq"] == 0,
            )
            self.check(
                "the mailbox selection answers 0", rig.set_selection(self.mods) == 0
            )
            recs = rig.sel_records()
            self.check(
                "both copies hold the selection, the same sequence number",
                recs[0] == recs[1]
                and recs[0] is not None
                and recs[0]["paths"] == self.mods
                and recs[0]["seq"] == 1,
                str(recs),
            )
            rig.restart()
            b1 = rig.boot_info()
            self.report["restart_boot"] = b1
            self.check(
                "the next boot loads it",
                b1["loaded"] == n and b1["results"] == [0] * n and b1["seq"] == 1,
                str(b1["results"]),
            )
            rows = [r for r in rig.voice_rows() if r[0] != FREE_TYPE]
            self.check("its voice row is claimed", len(rows) == 1, str(rows))
            rec = current(rig.sel_records())
            self.check(
                "the UI loop of the new boot cleared the counter",
                rec is not None and rec["counter"] == 0,
            )
            if not self.quick:
                self.check("select nothing", rig.set_selection([]) == 0)
                rig.restart()
                b2 = rig.boot_info()
                self.check(
                    "the boot after that loads nothing and the voice rows are free",
                    b2["count"] == 0
                    and all(r[0] == FREE_TYPE for r in rig.voice_rows()),
                )
            self.finish(rig)

    def cut(self) -> None:
        e = self.env
        m = self.mods
        rec_new = make_record(m, seq=2)
        rec_old = make_record([], seq=1)
        torn = bytearray(rec_new)
        torn[100] ^= 0xFF  # a torn write: the CRC fails
        cases = [
            (
                "A new, B old (cut after the first write)",
                rec_new,
                rec_old,
                len(m),
                2,
                0,
            ),
            (
                "A torn, B old (cut inside the first write)",
                bytes(torn),
                rec_old,
                0,
                1,
                1,
            ),
            ("A old, B new", rec_old, rec_new, len(m), 2, 1),
            ("both copies invalid", bytes(torn), bytes(torn), 0, 0, 0),
        ]
        if self.quick:
            cases = cases[:2]
        for name, a, b, want_loaded, want_seq, want_copy in cases:
            card = e.card(None, tag="cut")
            write_record_sectors(card, a, b)
            with self.rig(self.run_img, card, "cut") as rig:
                info = rig.boot_info()
                self.check(
                    f"power cut, {name}: the boot uses the valid copy with the higher sequence number",
                    info["loaded"] == want_loaded
                    and info["seq"] == want_seq
                    and info["copy"] == want_copy
                    and info["safe"] == 0,
                    f"loaded {info['loaded']} seq {info['seq']} copy {info['copy']}",
                )
                self.finish(rig)
        # the state made by the runtime itself: one copy written (CMD_SELECT_ONE), then the restart
        with self.rig(self.run_img, e.card(None, tag="cut2"), "cut2") as rig:
            r = rig.post(CMD_SELECT_ONE, "\0" + "\0".join(m) + "\0\0")
            recs = rig.sel_records()
            self.check(
                "one copy written by the runtime: A new, B still empty",
                r == 0
                and recs[0] is not None
                and recs[0]["seq"] == 1
                and recs[1] is None,
                str(recs),
            )
            rig.restart()
            info = rig.boot_info()
            self.check(
                "the restart after the cut loads the new copy",
                info["loaded"] == len(m) and info["copy"] == 0,
                str(info["results"]),
            )
            self.finish(rig)

    def safe(self) -> None:
        e = self.env
        with self.rig(
            self.run_img, e.card(["modules/fault.dkm"], tag="safe"), "safe"
        ) as rig:
            info = rig.boot_info()
            self.report["safe_boot"] = info
            self.check(
                "two boots that never reached the UI loop, the third boots in safe mode",
                info["safe"] == 1 and info["reason"] == 1 and info["counter"] == 2,
                f"{info['reason_name']} counter {info['counter']}",
            )
            self.check(
                "safe mode loaded no module",
                info["loaded"] == 0
                and info["all_results"][0] == E_SAFE_MODE
                and all(r[0] == FREE_TYPE for r in rig.voice_rows()),
                str(info["all_results"][:2]),
            )
            rec = current(rig.sel_records())
            self.check(
                "the UI loop ran: the counter is back to 0 and the selection is kept",
                info["ui_reached"] == 1
                and rec is not None
                and rec["counter"] == 0
                and rec["paths"] == ["modules/fault.dkm"],
                str(rec),
            )
            self.finish(rig)
        # safe once: the record asks for it; the boot that honours it clears the flag
        with self.rig(
            self.run_img, e.card(self.mods, flags=1, tag="safe2"), "safe2"
        ) as rig:
            info = rig.boot_info()
            rec = current(rig.sel_records())
            self.check(
                "the safe-once flag: safe mode (flag), no module, flag cleared, selection kept",
                info["safe"] == 1
                and info["reason"] == 3
                and info["loaded"] == 0
                and rec is not None
                and rec["flags"] == 0
                and rec["paths"] == self.mods,
                f"{info['reason_name']} {rec}",
            )
            self.finish(rig)

    def run(self) -> dict[str, Any]:
        t0 = time.monotonic()
        self.setup()
        scenarios = {
            "equiv": self.equiv,
            "restart": self.restart,
            "cut": self.cut,
            "safe": self.safe,
        }
        for name, fn in scenarios.items():
            if self.only and name not in self.only:
                continue
            print(f"--- {name}", flush=True)
            fn()
        if not self.only or "watch" in self.only:
            wt = self.watch_totals
            self.check(
                "write-watch on cf.expansion over every boot: no stray write",
                wt["write_events"] == 0 and wt["emulators"] > 0,
                str(wt),
            )
            self.check(
                "flush check over every boot: no hit", wt["flush_hits"] == 0, str(wt)
            )
            stray = sorted(self.store_writes - {SECTOR_A, SECTOR_B})
            self.check(
                "the only store-range sectors the guest wrote are the two record sectors",
                not stray,
                f"wrote {sorted(hex(s) for s in self.store_writes)}",
            )
        self.report["checks"] = [
            {"name": a, "ok": b, "detail": c} for a, b, c in self.checks
        ]
        self.report["pass"] = all(c[1] for c in self.checks)
        self.report["seconds"] = round(time.monotonic() - t0, 1)
        (self.work / "report.json").write_text(
            json.dumps(self.report, indent=1, default=str)
        )
        return self.report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["proof"])
    ap.add_argument("--image", default="dt2-1.17")
    ap.add_argument(
        "--quick", action="store_true", help="fewer cases in restart and cut"
    )
    ap.add_argument("--keep", type=Path)
    ap.add_argument(
        "--only", default="", help="comma separated: equiv,restart,cut,safe,watch"
    )
    a = ap.parse_args(argv)
    work = a.keep or Path(tempfile.mkdtemp(prefix="pk-dkrt-boot-"))
    proof = Proof(a.image, a.quick, work, {x for x in a.only.split(",") if x})
    try:
        rep = proof.run()
    except (Fail, M.Fail) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {k: v for k, v in rep.items() if k != "checks"}, indent=1, default=str
        )
    )
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
