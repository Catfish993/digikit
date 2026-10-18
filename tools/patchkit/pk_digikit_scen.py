#!/usr/bin/env python3
"""Boot-time scenarios of the digikit runtime: data files and one runner (docs/findings/39-digikit-boot-tests.md).

    uv run python -m tools.patchkit.pk_digikit_scen list
    uv run python -m tools.patchkit.pk_digikit_scen run [NAME ...] [--image dt2-1.17] [--tier quick|full] [--keep DIR]

A scenario is a TOML file in `tests/digikit/scenarios/`:

    [scenario]
    name = "equiv-dyn-pluck"       # the file's stem
    kind = "equiv" | "roundtrip" | "safe" | "cut" | "menu" | "upload" | "card" | "debug" | "crash"
    devices = ["dt2"]              # the scenario runs on the images of these devices (`images` names them instead)
    tier = "quick" | "full"        # quick: part of the verify gate; full: only `--tier full` (merges to main)

and a table named like the kind with its data (module sets, expectations). The runner is built on `pk_dkrt_boot`
(`Env`, `BootRig`): the same cold boot of the patched firmware with the image's own DSP, the same mailbox, restart and
record views. Every scenario ends with the common checks: write-watch (no stray write into `cf.expansion`), flush check,
and the card sectors the guest wrote (in the store range only the two selection sectors; the rest is the firmware's own
project and working sectors).

Kinds:

  equiv      a selection loaded at boot gives the same machine, parameter, page and voice rows as the image patchkit builds
             with the static twin of the set patched in (compared by decoded content: names, ranges, defaults, slots, knobs
             by parameter name; the type numbers and the pointers differ by construction), and the same PCM for a note when
             the set has a sound (`play`). A set without a twin (`twin = ""`) must leave every table as a boot with no module.
  roundtrip  a project with the run-time machine on tracks, parameter values, p-locks and a sound lock is saved under set A,
             loaded and saved under set B (machine absent) and under A again: the project slot's sound records, the sequencer
             data and the sound locks are byte-identical to the first save; the track keeps its type while its machine is absent.
  safe       a module whose init resets the machine, twice: the third boot is safe mode (no module, reported). With `[safe] part = "late"`: a
             module that hangs the UI task some time into a session, before (early) or after (late) the stable period that clears the boot
             counter: early leads to safe mode at the third boot, late never does (finding 37).
  menu       a set ticked in SETTINGS > DIGIKIT > MODULES on a boot with no selection and applied with APPLY & RESTART (two presses) is
             loaded by the next boot and compares as `equiv` does (rows, machine in the list, PCM of a note equal to the patched-in set's).
  cut        the card states a power cut leaves between the two copies of the selection record.
  card       card safety (finding 46): the one card-write function refuses writes outside the store range; a blank card works as before, a card
             with foreign bytes in the range goes read-only and the runtime writes nothing, a card with our store keeps working.
  debug      the debug log: LOG and PEEK over SysEx, the DEBUG switch, INFO's sequence numbers, the periodic events.
  crash      a CPU exception in a module's slot: the runtime's hook records it in RAM that a soft reset keeps and resets; the next boot reports
             the record (right PC), two in a row lead to safe mode (before and after the stable period).
  upload     module upload over USB SysEx (docs/design/digikit-upload.md): `[upload] part` = ops | cut | stock | playback | quick, see
             tools/patchkit/pk_digikit_upload.py. The `menu` kind with `upload = true` is the through-the-menu proof.

Keys worth knowing: `[equiv] trig_frame` (the DSP frame the note starts at, after the setup), `[roundtrip] engine = "cf" | "coupled"` (the panel script on a
ColdFire-only daemon beside a coupled boot check of each card, or all on coupled daemons). Time: `DIGIKIT_PROFILE=1` prints a phase table, `--jobs N` runs
(scenario, image) pairs in N processes, `DIGIKIT_RUN_SALT=x` forces cold boots, `DIGIKIT_SETTLE_MS` the emulated ms after a boot (default 300).

Cards are cached by content in `out/cache/digikit/cards/` (they are read-only for the guest, its writes live in an overlay), and the
daemon's own boot cache is keyed by the card path, so a repeated scenario restores its boots instead of running them cold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from tools import python_env  # noqa: E402
from tools.patchkit import pk_device  # noqa: E402
from tools.patchkit import pk_dkrt_boot as B  # noqa: E402
from tools.patchkit import pk_dkrt_machine as M  # noqa: E402

SCENARIOS = ROOT / "tests" / "digikit" / "scenarios"
CACHE = ROOT / "out" / "cache" / "digikit"
# every device with a module store and the images of it that have a port (`port = true` in the device file); the quick tier runs the last
ALL_IMAGES = {d: pk_device.port_images(d) for d in pk_device.digikit_devices()}
QUICK_IMAGES = {d: imgs[-1] for d, imgs in ALL_IMAGES.items()}
RT = B.RT
# emulated ms a rig runs after its boot before the scenario looks: the first UI-loop passes (a module's DSP part is waited for by
# `wait_dsp`); the boot tool's own proof keeps 1500
SETTLE_MS = int(os.environ.get("DIGIKIT_SETTLE_MS", "300"))
FIRMWARE_FIRST = (
    pk_device.working_project_sector()
)  # the working project; the project slots follow (finding 02, storage records)


class Fail(RuntimeError):
    pass


# ---- the scenario files -------------------------------------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    kind: str
    tier: str
    devices: list[str]
    data: dict[str, Any]
    path: Path

    def images(self, tier: str) -> list[str]:
        """The images a run at TIER covers: the quick tier takes the newest image of each device."""
        explicit = self.data["scenario"].get("images")
        if explicit:
            return list(explicit) if tier == "full" else explicit[-1:]
        out: list[str] = []
        for dev in self.devices:
            out += list(ALL_IMAGES[dev]) if tier == "full" else [QUICK_IMAGES[dev]]
        return out


def load_scenarios(root: Path = SCENARIOS) -> list[Scenario]:
    out = []
    for f in sorted(root.glob("*.toml")):
        d = tomllib.loads(f.read_text())
        s = d["scenario"]
        if s["name"] != f.stem:
            raise Fail(f"{f.name}: scenario.name must be the file's stem")
        if s["kind"] not in KINDS:
            raise Fail(f"{f.name}: unknown kind {s['kind']!r}")
        if s["kind"] not in d:
            raise Fail(f"{f.name}: no [{s['kind']}] table")
        tier = s.get("tier", "full")
        if tier not in ("quick", "full"):
            raise Fail(f"{f.name}: tier is quick or full")
        out.append(Scenario(s["name"], s["kind"], tier, s.get("devices", []), d, f))
    return out


def selected(tier: str, names: list[str] | None = None) -> list[Scenario]:
    """The scenarios of a tier (full: all; quick: those marked quick), or the named ones."""
    out = []
    for s in load_scenarios():
        if names:
            if s.name in names:
                out.append(s)
        elif tier == "full" or s.tier == "quick":
            out.append(s)
    return out


# ---- the context of one scenario on one image ------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, scen: Scenario, image: str, work: Path):
        self.scen, self.image, self.work = scen, image, work
        self.device = image[:3]
        self.dt2 = self.device == "dt2"
        self.env = B.Env(image, work)
        # verify builds in the same target directory while its python tests run: a tool binary can be missing for a moment
        tools_run = self.env.tools.run

        def run_tool(*argv: object, **kw: Any) -> Any:
            for _ in range(12):
                try:
                    return tools_run(*argv, **kw)
                except FileNotFoundError:
                    time.sleep(15)
            return tools_run(*argv, **kw)

        self.env.tools.run = run_tool  # type: ignore[method-assign]
        self.checks: list[tuple[str, bool, str]] = []
        self.report: dict[str, Any] = {"scenario": scen.name, "image": image}
        self.imgs: dict[str, B.Img] = {}
        self.totals = {"emulators": 0, "write_events": 0, "flush_hits": 0}
        self.store_writes: set[int] = set()
        self.all_writes: set[int] = set()
        self.used_cards: set[str] = set()
        self.tier = "full"
        self.lock = threading.Lock()  # rigs of one scenario may run in threads
        self.t0 = time.monotonic()

    def parallel(self, *fns: Callable[[], None]) -> None:
        """Run independent parts of a scenario (each boots its own daemon) in threads; the first error is raised after all have ended."""
        errors: list[BaseException] = []

        def guarded(fn: Callable[[], None]) -> Callable[[], None]:
            def inner() -> None:
                try:
                    fn()
                except BaseException as e:  # noqa: BLE001  raised in the scenario's thread
                    errors.append(e)

            return inner

        threads = [threading.Thread(target=guarded(fn)) for fn in fns]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]

    def table(self) -> dict[str, Any]:
        """The scenario's data table of this kind, with the device's own sub-table laid over it."""
        d = dict(self.scen.data[self.scen.kind])
        d.update(d.pop(self.device, {}))
        for other in ALL_IMAGES:
            d.pop(other, None)
        return d

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}", flush=True)

    def image_for(self, static: str, name: str) -> B.Img:
        key = f"{name}:{static}"
        if key not in self.imgs:
            self.imgs[key] = self.env.build_image(static, name)
        return self.imgs[key]

    def pack(self, module: str, file: str) -> str:
        self.env.pack(module, file)
        return f"modules/{file}"

    def card(
        self,
        selection: list[str] | None,
        counter: int = 0,
        seq: int = 1,
        flags: int = 0,
        recs: tuple[bytes | None, bytes | None] | None = None,
        base: Path | None = None,
        stable_blocks: int = 0,
        foreign: dict[int, bytes] | None = None,
    ) -> Path:
        """A card image, cached by content: the store of the folder and the record copies (or SELECTION), optionally
        laid over a `base` card file (the card a scenario left behind). FOREIGN: sector images (absolute sector -> 512-byte multiple)
        written over the card, bytes that are not ours in the store range. Returns the cached file."""
        h = hashlib.sha256(self.image.encode())
        store_dir = self.env.folder / "digikit"
        for f in sorted(x for x in store_dir.rglob("*") if x.is_file()):
            h.update(str(f.relative_to(store_dir)).encode() + f.read_bytes())
        a, b = recs if recs else (None, None)
        if recs is None and selection is not None:
            a = b = B.make_record(selection, seq, counter, flags, stable_blocks)
        for x in (a, b):
            h.update(x or b"-")
        if base is not None:  # a card made by `card_after` is named by its content
            h.update(base.name.encode())
        for s, data in sorted((foreign or {}).items()):
            h.update(b"foreign:%d:" % s + data)
        key = h.hexdigest()[:20]
        dst = CACHE / "cards" / f"{self.image}-{key}.img"
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.work / f"card-{key}.img"
            if base is not None:
                from tools.emulator import card_overlay

                card_overlay.clone(base, tmp)
            else:
                tmp = self.env.card(None, tag=key)
            if a is not None or b is not None:
                B.write_record_sectors(tmp, a, b)
            if foreign:
                with open(tmp, "r+b") as f:
                    for s, data in sorted(foreign.items()):
                        f.seek(s * 512)
                        f.write(data)
            shutil.move(tmp, dst)
        return dst

    def rig(
        self,
        img: B.Img,
        card: Path,
        tag: str,
        extra_env: dict[str, str] | None = None,
        cf_only: bool = False,
        settle_ms: int = SETTLE_MS,
    ) -> B.BootRig:
        """A rig cold-booted on CARD (SETTLE_MS: emulated ms it runs after the boot; a panel script that saves a project needs the boot tool's 1500); CF_ONLY: on a daemon without DSP and audio (about 3 times faster to boot, idle time free)."""
        ck = hashlib.sha256(card.name.encode() + img.syx.read_bytes()).hexdigest()[:10]
        if salt := os.environ.get(
            "DIGIKIT_RUN_SALT"
        ):  # a fresh run directory: every cold boot runs again (timing of a cold start)
            ck = hashlib.sha256((salt + ck).encode()).hexdigest()[:10]
        if cf_only:
            ck = "cf" + ck
        if "-after-" in card.name or card.name in self.used_cards:
            # a card the firmware saved a project on never runs the DT2 filesystem check the readiness contract waits for
            extra_env = {"DIGI_COLD_RELAX_FS": "1", **(extra_env or {})}
        return self.env.boot(
            img,
            card,
            f"scn-{ck}",
            watch=True,
            extra_env=extra_env,
            cf_only=cf_only,
            settle_ms=settle_ms,
        )  # one run directory per (firmware, card): its boot cache is reused

    def done(self, rig: B.BootRig) -> None:
        """Collect the card writes and the watch summaries of a rig that is finished."""
        store: set[int] = set()
        every: set[int] = set()
        try:
            store = set(rig.card_store_writes())
            from tools.coldfire import arena_safety

            ov = rig.work / f"{rig.tag}.all-overlay.bin"
            every = set(arena_safety.parse_overlay(ov.read_bytes()))
        except Exception as e:  # noqa: BLE001
            print(f"card.overlay: {e}", file=sys.stderr)
        w = rig.watch_summaries()
        with self.lock:
            self.store_writes |= store
            self.all_writes |= every
            for k in self.totals:
                self.totals[k] += w[k]

    def common_checks(self) -> None:
        t = self.totals
        self.check(
            "write-watch on cf.expansion over every boot: no stray write",
            t["write_events"] == 0 and t["emulators"] > 0,
            str(t),
        )
        self.check("flush check over every boot: no hit", t["flush_hits"] == 0, str(t))
        stray = sorted(self.store_writes - {B.SECTOR_A, B.SECTOR_B})
        self.check(
            "card writes in the store range: only the two selection sectors",
            not stray,
            f"wrote {sorted(hex(s) for s in self.store_writes)}",
        )
        outside = sorted(
            x
            for x in self.all_writes
            if x < FIRMWARE_FIRST and x not in (B.SECTOR_A, B.SECTOR_B)
        )
        self.check(
            "card writes elsewhere: only the firmware's own working project and project slots (sectors from 0x50000)",
            not outside,
            f"{len(self.all_writes)} sectors, below 0x50000: {[hex(x) for x in outside[:6]]}",
        )

    def common_checks_upload(self, project_writes_ok: bool = True) -> None:
        """The checks of the `upload` scenarios: the same write-watch and flush check; the card writes are the store range (an upload
        writes data, tables and both header copies there) and, when PROJECT_WRITES_OK, the firmware's own sectors from 0x50000."""
        t = self.totals
        self.check(
            "write-watch on cf.expansion over every boot: no stray write",
            t["write_events"] == 0 and t["emulators"] > 0,
            str(t),
        )
        self.check("flush check over every boot: no hit", t["flush_hits"] == 0, str(t))
        lo, hi = B.STORE_FIRST, B.STORE_FIRST + B.STORE_SECTORS
        writes = self.all_writes | self.store_writes
        outside = sorted(
            x
            for x in writes
            if not (lo <= x < hi) and (x < FIRMWARE_FIRST or not project_writes_ok)
        )
        self.check(
            "card writes: the module store range"
            + (
                " and the firmware's own project sectors"
                if project_writes_ok
                else " only"
            ),
            not outside,
            f"{len(writes)} sectors, elsewhere: {[hex(x) for x in outside[:6]]}",
        )

    def result(self) -> dict[str, Any]:
        self.report["checks"] = [
            {"name": a, "ok": b, "detail": c} for a, b, c in self.checks
        ]
        self.report["pass"] = all(c[1] for c in self.checks) and bool(self.checks)
        self.report["seconds"] = round(time.monotonic() - self.t0, 1)
        return self.report


# ---- decoding the firmware's tables from a booted rig ---------------------------------------------------------------------------


def cstr(rig: B.BootRig, addr: int, limit: int = 40) -> str:
    """The NUL-terminated string at ADDR (read by words; the registry's strings are in RAM or rodata)."""
    if not addr:
        return ""
    out = bytearray()
    for k in range(0, limit, 4):
        out += rig.w(addr + k).to_bytes(4, "big")
        if 0 in out:
            break
    return bytes(out).split(b"\0")[0].decode("latin-1")


def decode_tables(rig: B.BootRig) -> dict[str, Any]:
    """The machine, parameter and page rows with a name, decoded without pointers or type numbers: parameters are named,
    knobs name their parameter (or keep a stock code), pages name their machine."""
    plan = rig.plan
    o = "core/machines"
    rows, count = M.sym(plan, o, "machines.rows"), M.sym(plan, o, "machines.count")
    first, size = M.sym(plan, o, "machines.first"), M.sym(plan, o, "machines.row_size")
    pf = M.sym(plan, "core/params", "pf_table")
    pfirst, pcount = (
        M.sym(plan, "core/params", "params.first"),
        M.sym(plan, "core/params", "params.count"),
    )
    pnames: dict[int, str] = {}
    params: dict[str, Any] = {}
    for k in range(pcount):
        code = pfirst + k
        row = pf + 60 * code
        name = cstr(rig, rig.w(row + 40))
        if not name:
            continue
        pnames[code] = name
        words = [rig.w(row + 4 * i) for i in range(15)]
        params[name] = {
            "short": cstr(rig, words[12]),
            "mirror": words[1],
            "min": words[2],
            "max": words[3],
            "default": words[4],
            "template_words": [words[i] for i in (0, 5, 6, 7, 8, 9, 11, 13, 14)],
        }
    mnames: dict[int, str] = {}
    machines: dict[str, Any] = {}
    for k in range(count):
        row = rows + size * k
        name = cstr(rig, rig.w(row + 56))
        if not name:
            continue
        mnames[first + k] = name
        f = [rig.w(row + 8 + 4 * i) for i in range(9)]
        machines[name] = {
            "short": cstr(rig, rig.w(row + 60)),
            "clone_of": rig.w(row + 44),
            "own_dsp": rig.w(row + 52),
            "knobs": [pnames.get(c, c) for c in f[:8]],
            "level": f[8],
        }
    pages: list[Any] = []
    try:
        prow, pn = M.sym(plan, o, "pages.rows"), M.sym(plan, o, "pages.count")
    except Exception:  # noqa: BLE001  the Digitakt II has no page rows
        prow, pn = 0, 0
    for k in range(pn):
        row = prow + 84 * k
        mach, name = rig.w(row + 44), cstr(rig, rig.w(row))
        if not name or mach not in mnames:
            continue
        pages.append(
            {
                "name": name,
                "title": cstr(rig, rig.w(row + 4)),
                "machine": mnames[mach],
                "knobs": [
                    pnames.get(c, c) for c in (rig.w(row + 8 + 4 * i) for i in range(8))
                ],
                "level": rig.w(row + 8 + 32),
                "slots": [rig.w(row + 48 + 4 * i) for i in range(9)],
            }
        )
    return {"machines": machines, "params": params, "pages": pages}


def diff_tables(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Differences between two decoded table sets (empty: equal)."""
    out = []
    for kind in ("machines", "params"):
        for name in sorted(set(a[kind]) | set(b[kind])):
            if a[kind].get(name) != b[kind].get(name):
                out.append(
                    f"{kind[:-1]} {name}: {a[kind].get(name)} != {b[kind].get(name)}"
                )
    ka = sorted(a["pages"], key=lambda p: (p["machine"], p["name"]))
    kb = sorted(b["pages"], key=lambda p: (p["machine"], p["name"]))
    if ka != kb:
        out.append(f"pages: {ka} != {kb}")
    return out


class PlainRig(B.BootRig):
    """A reference image without the runtime (no mailbox, no boot info): only the firmware and the patched-in set."""

    def start(self) -> None:
        self.s.open(self.syx)
        self.s.segment = "boot"
        self.s.reset()
        self.s.run_ms(1500)


# ---- the kinds ------------------------------------------------------------------------------------------------------------------


def run_equiv(c: Ctx) -> None:
    """The reference boot (the static twin patched in) and the run-time boot (the set loaded from the card) are independent daemons:
    they run in two threads and their results are compared afterwards. A set without a twin is compared with a boot of no module."""
    t = c.table()
    mods = [
        c.pack(m, f"m{i}.dkm") for i, m in enumerate(t["modules"])
    ]  # the run-time set, in selection order
    twin = t.get("twin", "")
    run_img = c.image_for(RT, "run")
    empty = c.card(None)
    play = t.get("play", "")
    trig = t.get("trig_frame", M.TRIG_FRAME if c.dt2 else M.TRIG_FRAME_DN2)
    env_ref, env_run = {}, {}
    if (
        play == "sine"
    ):  # finding 32: the frame copy's type is forced to the static or the run-time number
        env_ref = {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_STATIC_TYPE)}
        env_run = {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_DYN_TYPE)}
    plain = bool(
        t.get("ref_plain")
    )  # the patched-in set does not fit next to the runtime's DSP area (DN2 pluck)
    ref_img = (c.image_for(twin if plain else RT + "," + twin, "ref")) if twin else None
    card = c.card(mods)
    out: dict[str, Any] = {}

    def select(rig: Any, hint_key: str) -> None:
        if c.dt2:
            return
        if (
            play == "pluck"
        ):  # DN2: the machine list position (finding 33); the sound proves the type
            rig.select(t.get(hint_key, t["hint"]))

    def base_boot() -> (
        None
    ):  # the tables of a boot with no module (the reference of a set with no twin)
        with c.rig(run_img, empty, "none") as rig:
            out["ref"] = decode_tables(rig)
            c.report["boot_none"] = rig.boot_info()
            c.done(rig)

    def ref_boot() -> None:
        assert ref_img is not None
        ref_rig = (
            PlainRig(
                c.env,
                ref_img,
                "ref",
                empty,
                False,
                {"DIGI_WRITE_WATCH_SEQ": "1", **env_ref},
            )
            if plain
            else c.rig(ref_img, empty, "ref", env_ref)
        )
        with ref_rig as rig:
            out["ref"] = decode_tables(rig)
            if play:
                if c.dt2:
                    rig.select_type(t["static_type"], t.get("hint", 7))
                select(rig, "hint")
                pcm = rig.note(c.work / "ref.wav", trig)
                out["ref_pcm"] = pcm
                c.check(
                    "the reference note is audible",
                    float(abs(pcm).max()) > 0.01,
                    f"peak {float(abs(pcm).max()):.3f}",
                )
            if not plain:
                c.done(rig)

    def run_boot() -> None:
        with c.rig(run_img, card, "run", env_run) as rig:
            info = rig.boot_info()
            n = len(mods)
            c.check(
                "the selection loaded at boot: every module result is 0",
                info["count"] == n
                and info["results"] == [0] * n
                and info["loaded"] == n
                and info["safe"] == 0,
                f"{info['results']}",
            )
            c.report[
                "boot_ready"
            ] = {  # finding 37: the DSP part at the cold boot's ready point
                "dsp_blocks": rig.frames_at_ready,
                "ms": round(rig.frames_at_ready * 64 / 48.0, 1),
                "results": rig.at_ready["results"],
                "voice_rows": len(rig.voice_at_ready),
                "blocks_ready_to_dsp_done": rig.blocks_to_dsp,  # DSP blocks (1.33 ms) from the ready point to the voice row being in
            }
            out["got"] = decode_tables(rig)
            used = [r for r in rig.voice_rows() if r[0] != B.FREE_TYPE]
            want_voices = t.get("voices", 0)
            c.check(
                "voice rows claimed",
                len(used) == want_voices,
                f"{[(hex(a), hex(b)) for a, b in used]}, want {want_voices}",
            )
            if play:
                if c.dt2:
                    rig.select_type(
                        M.PLUCK_DYN_TYPE, t.get("hint_run", t.get("hint", 7))
                    )
                select(rig, "hint_run")
                out["run_pcm"] = rig.note(c.work / "run.wav", trig)
                out["run_rig_checks"] = rig  # pcm_check reports through a rig
            c.done(rig)

    c.parallel(run_boot, ref_boot if twin else base_boot)
    ref, got = out["ref"], out["got"]
    for name in t.get(
        "ignore_machines", []
    ):  # a static twin's machine with no run-time counterpart
        ref["machines"].pop(name, None)
    d = diff_tables(got, ref)
    c.check(
        "machine, parameter and page rows equal the "
        + ("static twin's" if twin else "no-module boot's"),
        not d,
        "; ".join(d[:3]),
    )
    if twin:
        c.check(
            "the run-time set added rows",
            bool(got["machines"]) or bool(got["params"]) or bool(got["pages"]),
        )
    if play:
        rr = out["run_rig_checks"]
        M.pcm_check(
            rr,
            out["ref_pcm"],
            out["run_pcm"],
            c.report,
            "machine" if c.dt2 else "voice",
        )
        c.checks += rr.checks
    c.common_checks()


def run_safe_late(c: Ctx) -> None:
    """A module that fails some time into a session (docs/findings/37-boot-load.md, "Safe mode after the first UI pass"). The runtime
    clears its boot counter only after a stable period of UI loop; here the period is shortened (the scenario pokes `DKRT_STABLE_BLOCKS`).
    EARLY: the module hangs the UI task before the period is over: each boot counts as bad, the third boot is safe mode. LATE: it hangs
    after the period: the counter was cleared, every boot loads the module and none is safe. A power cycle is a new rig on the card the
    last one left (the overlay of its writes laid over it)."""
    from tools.patchkit.pk_digikit_project import card_after

    t = c.table()
    mod = c.pack(t["fault"], "late.dkm")
    img = c.image_for(RT, "run")
    cfg = c.env.folder / "digikit" / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    cards: dict[str, Path] = {}
    for kind in ("early", "late"):
        (cfg / "fault-after.bin").write_bytes(
            int(t[f"{kind}_blocks"]).to_bytes(4, "big") + bytes([1])
        )  # hang
        cards[kind] = c.card([mod], stable_blocks=int(t["stable_blocks"]))
    # The coupled cold boot runs to its ready point before the rig is handed out: about 6,000 blocks (4 s) after the first UI-loop pass on
    # a fresh card (measured: the first pass is at block 5,972 of the firmware's counter, the ready point at 11,921), fewer on a card the
    # firmware has saved to. A failure earlier than the ready point would stop the boot from ever being ready, so the periods lie above it.
    # The rig then runs until the UI loop stands still (the hang) or, in safe mode, for the same time.

    def settle_hang(rig: Any, ticks_sym: int, limit_ms: int = 9000) -> bool:
        """Run in steps until the runtime's UI-loop count stops rising; False when it never does within LIMIT_MS."""
        for _ in range(limit_ms // 400):
            t0 = rig.w(ticks_sym)
            rig.s.run_ms(400)
            if rig.w(ticks_sym) == t0:
                return True
        return False

    def sequence(kind: str) -> Callable[[], None]:
        def run() -> None:
            base = cards[kind]
            seen: list[dict[str, Any]] = []
            ticks_sym = M.sym(img.plan, "core/runtime", "DKRT_STATE") + 8
            for boot in range(3):
                with c.rig(img, base, f"late-{kind}{boot}") as rig:
                    info = rig.boot_info()
                    hung = settle_hang(rig, ticks_sym)
                    after = rig.boot_info()
                    rec = B.current(rig.sel_records())
                    seen.append(
                        {"info": info, "after": after, "rec": rec, "hung": hung}
                    )
                    ov = rig.work / f"late-{kind}{boot}.ov.bin"
                    rig.emu.call("card.overlay", path=str(ov))
                    nxt = card_after(c, base, ov)
                    c.done(rig)
                base = nxt
            i0, i1, i2 = (s["info"] for s in seen)

            def m(s: dict[str, Any]) -> tuple[Any, ...]:
                return (
                    s["info"]["counter"],
                    s["after"]["stable"],
                    s["rec"] and s["rec"]["counter"],
                    s["hung"],
                )

            if kind == "early":
                c.check(
                    "early: boots 1 and 2 load the module and the UI task hangs in it",
                    all(
                        s["info"]["safe"] == 0
                        and s["info"]["loaded"] == 1
                        and s["hung"]
                        for s in seen[:2]
                    ),
                    str([m(s) for s in seen]),
                )
                c.check(
                    "early: the counter is raised at the start of each boot and not cleared (the failure came before the stable period)",
                    [i0["counter"], i1["counter"], i2["counter"]] == [0, 1, 2]
                    and all(
                        s["after"]["stable"] == 0
                        and s["rec"]["counter"] == s["info"]["counter"] + 1
                        for s in seen[:2]
                    ),
                    str([m(s) for s in seen]),
                )
                c.check(
                    "early: two bad boots in a row, the third boot is safe mode (counter): the module is not loaded and the UI keeps running",
                    i2["safe"] == 1
                    and i2["reason"] == 1
                    and i2["loaded"] == 0
                    and not seen[2]["hung"],
                    f"{i2['reason_name']} loaded {i2['loaded']} hung {seen[2]['hung']}",
                )
                c.check(
                    "early: the safe boot clears the counter at its first UI-loop pass (nothing of a module runs in it), selection kept",
                    seen[2]["after"]["stable"] == 1
                    and seen[2]["rec"]["counter"] == 0
                    and seen[2]["rec"]["paths"] == [mod],
                    str(seen[2]["rec"]),
                )
            else:
                c.check(
                    "late: every boot loads the module and the UI task hangs in it",
                    all(
                        s["info"]["safe"] == 0
                        and s["info"]["loaded"] == 1
                        and s["hung"]
                        for s in seen
                    ),
                    str([m(s) for s in seen]),
                )
                c.check(
                    "late: the counter was cleared once the stable period was over, before the failure: every boot starts at 0, no boot is safe",
                    all(
                        s["after"]["stable"] == 1 and s["rec"]["counter"] == 0
                        for s in seen
                    )
                    and [i0["counter"], i1["counter"], i2["counter"]] == [0, 0, 0],
                    str([m(s) for s in seen]),
                )

        return run

    c.parallel(sequence("early"), sequence("late"))
    c.common_checks()


def run_safe(c: Ctx) -> None:
    t = c.table()
    if t.get("part") == "late":
        run_safe_late(c)
        return
    mod = c.pack(t["fault"], "fault.dkm")
    other = [c.pack(m, f"m{i}.dkm") for i, m in enumerate(t.get("modules", []))]
    img = c.image_for(RT, "run")
    card_fault = c.card([mod])
    card_flag = c.card(other, flags=1) if other else None

    def fault() -> None:
        with c.rig(img, card_fault, "safe") as rig:
            info = rig.boot_info()
            c.check(
                "two boots that never reached the UI loop, the third boots in safe mode",
                info["safe"] == 1 and info["reason"] == 1 and info["counter"] == 2,
                f"{info['reason_name']} counter {info['counter']}",
            )
            c.check(
                "safe mode loaded no module and reported it",
                info["loaded"] == 0
                and info["all_results"][0] == B.E_SAFE_MODE
                and all(r[0] == B.FREE_TYPE for r in rig.voice_rows()),
                str(info["all_results"][:2]),
            )
            rec = B.current(rig.sel_records())
            c.check(
                "the UI loop ran: counter 0, selection kept",
                info["ui_reached"] == 1
                and rec is not None
                and rec["counter"] == 0
                and rec["paths"] == [mod],
                str(rec),
            )
            c.done(rig)

    def flag() -> None:
        assert card_flag is not None
        with c.rig(img, card_flag, "safe-flag") as rig:
            info = rig.boot_info()
            rec = B.current(rig.sel_records())
            c.check(
                "the safe-once flag: safe mode (flag), no module, flag cleared, selection kept",
                info["safe"] == 1
                and info["reason"] == 3
                and info["loaded"] == 0
                and rec is not None
                and rec["flags"] == 0
                and rec["paths"] == other,
                f"{info['reason_name']} {rec}",
            )
            c.done(rig)

    c.parallel(*([fault, flag] if other else [fault]))
    c.common_checks()


def run_cut(c: Ctx) -> None:
    t = c.table()
    mods = [c.pack(m, f"m{i}.dkm") for i, m in enumerate(t["modules"])]
    img = c.image_for(RT, "run")
    new, old = B.make_record(mods, seq=2), B.make_record([], seq=1)
    torn = bytearray(new)
    torn[100] ^= 0xFF
    named = {"new": new, "old": old, "torn": bytes(torn)}
    cases = t["case"]
    if c.tier == "quick":  # the first cases only
        cases = cases[: t.get("quick_cases", len(cases))]

    def one_case(case: dict[str, Any], k: int) -> Callable[[], None]:
        a, b = named[case["a"]], named[case["b"]]
        card = c.card(None, recs=(a, b))

        def run() -> None:
            with c.rig(img, card, f"cut{k}") as rig:
                info = rig.boot_info()
                want = case["want"]
                n = len(mods) if want["loaded"] == "all" else want["loaded"]
                c.check(
                    f"power cut, A {case['a']} / B {case['b']}: the boot uses the valid copy with the higher sequence number",
                    info["loaded"] == n
                    and info["seq"] == want["seq"]
                    and info["copy"] == want["copy"]
                    and info["safe"] == 0,
                    f"loaded {info['loaded']} seq {info['seq']} copy {info['copy']}",
                )
                c.done(rig)

        return run

    def runtime_cut() -> None:
        with c.rig(img, c.card(None), "cut-rt") as rig:
            r = rig.post(B.CMD_SELECT_ONE, "\0" + "\0".join(mods) + "\0\0")
            recs = rig.sel_records()
            c.check(
                "one copy written by the runtime: A new, B still empty",
                r == 0
                and recs[0] is not None
                and recs[0]["seq"] == 1
                and recs[1] is None,
                str(recs),
            )
            rig.restart()
            info = rig.boot_info()
            c.check(
                "the restart after the cut loads the new copy",
                info["loaded"] == len(mods) and info["copy"] == 0,
                str(info["results"]),
            )
            c.done(rig)

    # every boot is its own daemon on its own card: they run side by side
    c.parallel(
        *[one_case(case, k) for k, case in enumerate(cases)],
        *([runtime_cut] if t.get("runtime_cut", True) else []),
    )
    c.common_checks()


def run_roundtrip(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_project as P

    P.run_roundtrip(c)


def run_menu(c: Ctx) -> None:
    """Through the menu: boot with an empty selection, SETTINGS > DIGIKIT > MODULES, tick the set, APPLY & RESTART (two presses), and
    after the boot compare with the patched-in set as `equiv` does (rows, machine in the list, PCM of a note). The set may hold a
    code-only module next to the machine."""
    from tools.coldfire import digikit_menu_proof as D

    t = c.table()
    names = {m: m.split("/")[-1] for m in t["modules"]}
    files = {m: c.pack(m, f"{names[m]}.dkm") for m in t["modules"]}
    rows = sorted(names.values())  # the menu lists the store's files in name order
    upload = bool(t.get("upload"))
    blobs = {}
    if upload:  # the store is filled over USB SysEx, not by the card build: take the packed files out of the folder
        for path in files.values():
            f = c.env.folder / "digikit" / path
            blobs[path] = f.read_bytes()
            f.unlink()
    menu_img = c.image_for(f"{RT},core/digikit-menu", "menu")
    plan = D.Plan(menu_img.plan)
    empty = c.card(None)
    trig = t.get("trig_frame", M.TRIG_FRAME if c.dt2 else M.TRIG_FRAME_DN2)
    twin = t["twin"]
    plain = bool(t.get("ref_plain"))
    ref_img = c.image_for(twin if plain else RT + "," + twin, "ref")
    ref_rig = (
        PlainRig(c.env, ref_img, "ref", empty, False, {"DIGI_WRITE_WATCH_SEQ": "1"})
        if plain
        else c.rig(ref_img, empty, "ref")
    )
    with ref_rig as rig:
        ref = decode_tables(rig)
        if c.dt2:
            rig.select_type(t["static_type"], t.get("hint", 7))
        else:
            rig.select(t["hint"])
        ref_pcm = rig.note(c.work / "ref.wav", trig)
        c.check(
            "the reference note is audible",
            float(abs(ref_pcm).max()) > 0.01,
            f"peak {float(abs(ref_pcm).max()):.3f}",
        )
        if not plain:
            c.done(rig)

    def w(rig: B.BootRig, name: str, k: int = 0) -> int:
        return rig.w(plan.addr(name) + 4 * k)

    def tap(rig: B.BootRig, key: str, gap_ms: int = 350) -> None:
        rig.s.do_tap(key)
        rig.s.run_ms(gap_ms)

    with c.rig(menu_img, empty, "menu") as rig:
        b0 = rig.boot_info()
        c.check("the first boot has no module", b0["count"] == 0 and b0["loaded"] == 0)
        if upload:
            from tools.device import dkupload as DK
            from tools.patchkit import pk_digikit_upload as U

            up = DK.Uploader(U.transport(rig))
            for path, data in blobs.items():
                up.put(path, data)
            c.check(
                "LIST after the uploads names the modules",
                sorted(p for p, _, _ in up.ls()) == sorted(blobs),
                str(up.ls()),
            )
        tap(rig, "NO", 400)  # SETTINGS > DIGIKIT (the ninth row) > MODULES
        tap(rig, "SETTINGS", 600)
        for _ in range(8):
            tap(rig, "DOWN", 200)
        tap(rig, "YES", 700)
        tap(rig, "YES", 900)
        n = w(rig, "NENT")
        c.check("the store lists the packed modules", n == len(rows), f"{n} rows")
        c.check("APPLY not offered before a tick", w(rig, "DK_OFFER") == 0)
        want = set(names.values())
        for name in rows:  # tick the set, in list order
            if name in want:
                tap(rig, "YES", 500)
            tap(rig, "DOWN", 200)
        tick = [
            rig.w(plan.addr("TICK") + 0) >> (24 - 8 * k) & 0xFF
            for k in range(len(rows))
        ]
        c.check(
            "the pending selection is the whole set", tick == [1] * len(rows), str(tick)
        )
        c.check(
            "the card's selection is still empty", rig.sel_records() == [None, None]
        )
        c.check("APPLY offered", w(rig, "DK_OFFER") == 1)
        n0 = w(rig, "DK_POPUPS")
        tap(rig, "YES", 700)  # first press: the popup, nothing saved
        c.check(
            "first press: one popup, nothing saved",
            w(rig, "DK_POPUPS") == n0 + 1 and rig.sel_records() == [None, None],
        )
        tap(rig, "NO", 500)
        rig.emu.drain_events()
        tap(rig, "YES", 300)  # second press: save and restart
        c.check("dkrt_selection_set answered 0", w(rig, "DK_APPLY_SET") == 0)
        D._wait_restart(rig)
        info = rig.boot_info()
        k = len(rows)
        c.check(
            "the boot after APPLY & RESTART loaded the whole set",
            info["count"] == k
            and info["loaded"] == k
            and info["results"] == [0] * k
            and info["safe"] == 0,
            str(info["results"]),
        )
        rec = B.current(rig.sel_records())
        c.check(
            "the record in use holds the ticked modules",
            rec is not None and sorted(rec["paths"]) == sorted(files.values()),
            str(rec),
        )
        got = decode_tables(rig)
        d = diff_tables(got, ref)
        c.check(
            "the machine is in the list: machine, parameter and page rows equal the patched-in set's",
            not d and bool(got["machines"]),
            "; ".join(d[:3]),
        )
        used = [r for r in rig.voice_rows() if r[0] != B.FREE_TYPE]
        c.check("voice row claimed", len(used) == t.get("voices", 1), str(used))
        if c.dt2:
            rig.select_type(M.PLUCK_DYN_TYPE, t.get("hint_run", t.get("hint", 7)))
        else:
            rig.select(t.get("hint_run", t["hint"]))
        # the DSP block count runs on through the panel script and the restart: the note starts a little after the setup
        pcm = rig.note(c.work / "run.wav", max(trig, rig.frames() + 300))
        M.pcm_check(rig, ref_pcm, pcm, c.report, "machine" if c.dt2 else "voice")
        c.checks += rig.checks
        c.done(rig)
    c.common_checks_upload() if upload else c.common_checks()


def run_upload(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_upload as U

    U.run_upload(c)


def run_card(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_debug as DBG

    DBG.run_card(c)


def run_debug(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_debug as DBG

    DBG.run_debug(c)


def run_crash(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_debug as DBG

    DBG.run_crash(c)


KINDS = {
    "card": run_card,
    "debug": run_debug,
    "crash": run_crash,
    "equiv": run_equiv,
    "upload": run_upload,
    "menu": run_menu,
    "safe": run_safe,
    "cut": run_cut,
    "roundtrip": run_roundtrip,
}


def run_scenario(
    scen: Scenario, image: str, keep: Path | None = None, tier: str | None = None
) -> dict[str, Any]:
    work = keep or Path(tempfile.mkdtemp(prefix=f"digikit-{scen.name}-"))
    work.mkdir(parents=True, exist_ok=True)
    c = Ctx(scen, image, work)
    c.tier = tier or os.environ.get("DIGIKIT_TIER", "quick")
    print(f"--- {scen.name} on {image}", flush=True)
    prof = None
    if os.environ.get(
        "DIGIKIT_PROFILE"
    ):  # a per-phase wall-time table at the end (pk_digikit_prof)
        from tools.patchkit import pk_digikit_prof

        prof = pk_digikit_prof.install()
    try:
        KINDS[scen.kind](c)
    except Exception as e:  # noqa: BLE001  a scenario that cannot run is a failed scenario, with its reason
        import traceback

        traceback.print_exc()
        c.check(f"scenario error: {type(e).__name__}: {e}", False)
    rep = c.result()
    if prof is not None:
        print(prof.report(), flush=True)
    (work / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep


@dataclass
class Summary:
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r["pass"] for r in self.results)


def run_pairs(
    pairs: list[tuple[Scenario, str]],
    tier: str,
    jobs: int,
    keep: Path | None = None,
    quiet: bool = False,
) -> dict[tuple[str, str], dict[str, Any]]:
    """The (scenario, image) pairs in child processes, JOBS at a time, the long ones first. Returns the report of each pair (its
    `report.json`; a pair whose process died without one gets a failed report that names its log). One log per pair in KEEP (or a
    temporary directory)."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    base = keep or Path(tempfile.mkdtemp(prefix="digikit-jobs-"))
    base.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    rank = {
        "cut": 1,
        "upload": 1,
        "card": 1,
        "debug": 1,
        "roundtrip": 2,
        "safe": 3,
        "crash": 3,
        "equiv": 4,
    }
    order = sorted(
        pairs,
        key=lambda p: (
            -1
            if p[0].data.get(p[0].kind, {}).get("engine") == "coupled"
            else rank.get(p[0].kind, 9)
        ),
    )

    def one(pair: tuple[Scenario, str]) -> tuple[tuple[str, str], dict[str, Any]]:
        s, image = pair
        log = base / f"{s.name}-{image}.log"
        cmd = [
            sys.executable,
            "-m",
            "tools.patchkit.pk_digikit_scen",
            "run",
            s.name,
            "--image",
            image,
            "--tier",
            tier,
            "--keep",
            str(base),
        ]
        with log.open("w") as f:
            subprocess.run(
                cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=python_env()
            )
        report = base / f"{s.name}-{image}" / "report.json"
        try:
            rep = json.loads(report.read_text())
        except (OSError, ValueError):
            rep = {"scenario": s.name, "image": image, "pass": False, "seconds": 0,
                   "checks": [{"name": f"no result; see {log}", "ok": False, "detail": ""}]}  # fmt: skip
        if not quiet:
            bad = [c["name"] for c in rep["checks"] if not c["ok"]]
            print(
                f"{'PASS' if rep['pass'] else 'FAIL'} {s.name} {image} ({rep['seconds']} s)"
                + (f": {bad}" if bad else "")
                + f"   [{time.monotonic() - t0:.0f} s since start, log {log}]",
                flush=True,
            )
        return (s.name, image), rep

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return dict(pool.map(one, order))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["list", "run"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--image", action="append", default=[])
    ap.add_argument("--tier", choices=["quick", "full"], default="quick")
    ap.add_argument("--keep", type=Path)
    ap.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="run the (scenario, image) pairs in this many processes at once (each prints to its own log; the summary follows)",
    )
    a = ap.parse_args(argv)
    scens = selected(a.tier, a.names)
    if a.cmd == "list":
        for s in scens:
            print(f"{s.name:28s} {s.kind:10s} {s.tier:5s} {','.join(s.images(a.tier))}")
        return 0
    pairs = [
        (s, image)
        for s in scens
        for image in a.image or s.images(a.tier)
        if image[:3] in s.devices or s.data["scenario"].get("images")
    ]
    if a.jobs > 1 and len(pairs) > 1:
        reports = run_pairs(pairs, a.tier, a.jobs, a.keep)
        return 0 if all(r["pass"] for r in reports.values()) else 1
    summary = Summary()
    for s, image in pairs:
        keep = a.keep / f"{s.name}-{image}" if a.keep else None
        rep = run_scenario(s, image, keep, a.tier)
        summary.results.append(rep)
        bad = [c["name"] for c in rep["checks"] if not c["ok"]]
        print(
            f"{'PASS' if rep['pass'] else 'FAIL'} {s.name} {image} ({rep['seconds']} s)"
            + (f": {bad}" if bad else ""),
            flush=True,
        )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
