#!/usr/bin/env python3
"""Boot-time scenarios of the digikit runtime: data files and one runner (docs/findings/39-digikit-boot-tests.md).

    uv run python -m tools.patchkit.pk_digikit_scen list
    uv run python -m tools.patchkit.pk_digikit_scen run [NAME ...] [--image dt2-1.17] [--tier quick|full] [--keep DIR]

A scenario is a TOML file in `tests/digikit/scenarios/`:

    [scenario]
    name = "equiv-dyn-pluck"       # the file's stem
    kind = "equiv" | "roundtrip" | "safe" | "cut" | "menu" | "upload" | "card" | "debug" | "crash" | "rungc" | "pages" | "fx" | "fxsend" | "fxroute" | "fxroutes" | "settings"
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
             `pcm_sha256` (with `play`) replaces the PCM compare when no twin has the sound: the SHA-256 of the run-time note from its onset over
             `pcm_samples` (default 24000) interleaved float32 samples; "record" fails and prints the hash. `ignore_run_machines` drops a run-time
             machine (with its parameters and pages) that the twin has no counterpart of.
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
             `[debug] part = "sticky"`: sticky machine type numbers (assigned at the boot load, kept in the selection record's map, eviction); the plain part also checks MODULES.
             `part = "guard"` (both images): the runtime guard on a NaN fixture.
  crash      a CPU exception in a module's slot: the runtime's hook records it in RAM that a soft reset keeps and resets; the next boot reports
             the record (right PC), two in a row lead to safe mode (before and after the stable period).
  rungc      the rehearsal of rung C on a Digitakt II (docs/PATCHING.md): the rung C image, a card with samples and a blank store, `dkupload put`,
             the menu, APPLY & RESTART, the machine on a track, a note equal to the patched-in pluck, the DSP's counters; screenshots to
             out/runs/lanes/b-rungC/ (tools/patchkit/pk_digikit_rungc.py).
  pages      the parameter pages of a machine chosen on track 1 (SRC, SRC again, encoder turns): the run-time module's frames equal the patched-in twin's, an
             added machine never draws the sample view, stock sample machines still do (finding 50; tools/patchkit/pk_digikit_pages.py).
  fx         effects loaded at run time (DSP section version 4, `core/dynfx`; Digitakt II and Digitone II): the sine and `test/dyn-fx-gain` selected at boot play as the
             dry sine times the gain, the chain's row, MODULES and CAPACITY say so, deselecting the effect and restarting gives the dry PCM again, and
             one effect more than the device has slots is refused (`E_DSP_ROWS`, and by the fit rule); tools/patchkit/pk_digikit_fx.py.
  fxsend     a run-time effect on the send route of `core/fx` (Digitakt II): the row's route and amount words are written with `dsp.patch`; no amount, or an amount on a silent track, leaves the PCM
             dry bit for bit; an amount on the playing track adds the effect's return (the amount squared, as the stock sends); the effect runs once a block; tools/patchkit/pk_digikit_fxsend.py.
  fxroutes   SETTINGS > DIGIKIT > FX ROUTES and the manifest's route fields (`route`, `routes` of `[[fx]]`; Digitakt II): a send-default effect starts on the send route, the page lists each
             effect as `<TAB>  SEND` or `<TAB>  MASTER`, YES switches the pending route (MASTER ONLY, SEND ONLY and NO SEND SLOT are messages), APPLY & RESTART writes the settings and restarts and the
             route is in effect after it (row word, FX key page, PCM, `patchkit store dump`), the FX view lists send effects first; tools/patchkit/pk_digikit_fxroutes.py.
  settings   the settings store of the runtime (docs/design/digikit-store.md, "The settings record"): a key put through the mailbox is written 2 s after the last change to the copy that is not
             current (A, then B, then A), never to anything else in the store range; a soft restart reads it back; the SETTINGS command and CAPACITY kind SETTINGS report it; `patchkit store dump`
             decodes the card the guest left; a blank card is claimed first; a torn newer copy leaves the older one; tools/patchkit/pk_digikit_settings.py.
  fxpage     the FX view pages and knobs of run-time effects (`core/fxpages`; Digitakt II and Digitone II): with no effect loaded the three stock pages draw and play as on an image without
             the pages; with `test/dyn-fx-gain` the view has a fourth page (tab GAN, title Test Gain, the GAIN knob and MIX), turning the knobs changes the PCM as
             declared (gain, MIX 0 = dry bit for bit, MIX 127 = wet, the linear blend between), and deselecting the effect and restarting brings the three pages and
             the dry PCM back; screenshots with `DIGIKIT_SHOTS=DIR`; tools/patchkit/pk_digikit_fxpage.py.
  fxpersist  the values of run-time effects are saved with the kit (`core/fxpages` hooks on the kit loader, saver and initialiser; Digitakt II and Digitone II): Save Kit and Load Kit restore them to the
             same effect (kit array, DSP row, PCM, pages) and give another effect, an empty row or a stock kit the defaults, never a zero on the way to the DSP; a project; another
             pattern's kit; tools/patchkit/pk_digikit_fxpersist.py.
  fxshift    the frequency shifter `digikit/freqshift` (Digitakt II and Digitone II; the manifest says send, MIX 127): its page (tab FSH, title Freq Shifter, SHIFT bipolar, MIX default 127), on the master route (stored `master` on
             the Digitakt II, the fallback on the Digitone II, which has no send cells) MIX 0 leaves the PCM bit for bit as it was, MIX 127 and SHIFT 0 keeps the sine, turning SHIFT moves the sine by the shift of the routine's
             mapping in either direction with the image 40 dB down, Save Kit and Load Kit give SHIFT back; on the Digitakt II also the send route (the default): an amount on track 1 gives a return of amount^2 x MIX x the shifted
             track (compared with the float32 model), an amount on another track adds nothing; tools/patchkit/pk_digikit_fxshift.py.
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
from tools.patchkit import pk_harness as H  # noqa: E402

SCENARIOS = ROOT / "tests" / "digikit" / "scenarios"
CACHE = ROOT / "out" / "cache" / "digikit"
# every device with a module store and its newest image that has a port (`port = true` in the device file)
QUICK_IMAGES = {d: pk_device.port_images(d)[-1] for d in pk_device.digikit_devices()}
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
        """The images a run covers: the newest image of each device the scenario targets."""
        explicit = self.data["scenario"].get("images")
        if explicit:
            return [i for i in explicit if i in QUICK_IMAGES.values()]
        return [QUICK_IMAGES[dev] for dev in self.devices]


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
        self.store = pk_device.store_of(
            self.env.device
        )  # the store area of this device
        self.live_card: str | None = (
            None  # the card file of the last `live_boot` (the DT2's)
        )
        self.store_writes: set[int] = set()
        self.all_writes: set[int] = set()
        self.used_cards: set[str] = set()
        self.tier = "full"
        self.lock = threading.Lock()  # rigs of one scenario may run in threads
        self.t0 = time.monotonic()

    def parallel(self, *fns: Callable[[], None]) -> None:
        """Run independent parts of a scenario (each boots its own daemon) in threads, at most
        pk_harness.part_jobs(parts) at once (DIGIKIT_PART_JOBS); the first error is raised after all have ended."""
        errors: list[BaseException] = []
        width = threading.Semaphore(H.part_jobs(len(fns)))

        def guarded(fn: Callable[[], None]) -> Callable[[], None]:
            def inner() -> None:
                with width:
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
        for other in QUICK_IMAGES:
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

    def pack_synth(self, spec: str, file: str) -> str:
        """A synthetic module (`patchkit synth-dkm`) in the store folder; returns its store path."""
        self.env.synth(spec, file)
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
        # the store format and area are part of the key: a card built for another area or version is not this card
        h.update(b"store-v3.1" + repr(self.store).encode())
        store_dir = self.env.folder / "digikit"
        for f in sorted(store_dir.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(store_dir)).encode() + f.read_bytes())
            elif f.is_dir() and not any(
                f.iterdir()
            ):  # an empty folder is a folder entry of the store
                h.update(b"dir:" + str(f.relative_to(store_dir)).encode())
        a, b = recs if recs else (None, None)
        if recs is None and selection is not None:
            # a card laid over a base keeps the base's sticky machine types, as the menu's SELECT does (the new record is not a fresh card's)
            tmap = None
            if base is not None:
                cur = B.current(B.records_in(base, b"", self.env.device))
                tmap = cur["map"] if cur else None
            a = b = B.make_record(selection, seq, counter, flags, stable_blocks, tmap)
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
                B.write_record_sectors(tmp, a, b, self.env.device)
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

    def common_checks(
        self,
        firmware_areas: tuple[tuple[int, int], ...] = (),
        store_extra: frozenset[int] = frozenset(),
    ) -> None:
        """The checks every scenario ends with. FIRMWARE_AREAS: sector ranges (first, end) below 0x50000 that the scenario makes the firmware write on purpose (the kit library of a Save Kit).
        STORE_EXTRA: store-range sectors besides the two selection sectors that the scenario writes on purpose (the settings copies)."""
        t = self.totals
        self.check(
            "write-watch on cf.expansion over every boot: no stray write",
            t["write_events"] == 0 and t["emulators"] > 0,
            str(t),
        )
        self.check("flush check over every boot: no hit", t["flush_hits"] == 0, str(t))
        stray = sorted(
            self.store_writes - {self.store.sel_a, self.store.sel_b} - store_extra
        )
        self.check(
            "card writes in the store range: only the two selection sectors"
            + (
                " and the sectors the scenario writes on purpose" if store_extra else ""
            ),
            not stray,
            f"wrote {sorted(hex(s) for s in self.store_writes)}",
        )
        outside = sorted(
            x
            for x in self.all_writes
            if x < FIRMWARE_FIRST
            and x not in (self.store.sel_a, self.store.sel_b)
            and not any(lo <= x < hi for lo, hi in firmware_areas)
        )
        self.check(
            "card writes elsewhere: only the firmware's own working project and project slots (sectors from 0x50000)"
            + (
                " and the areas the scenario writes on purpose"
                if firmware_areas
                else ""
            ),
            not outside,
            f"{len(self.all_writes)} sectors, below 0x50000: {[hex(x) for x in outside[:6]]}"
            + "".join(
                f"; in {hex(lo)}..{hex(hi)}: {len(inside)} sectors {hex(min(inside))}..{hex(max(inside))}"
                for lo, hi in firmware_areas
                if (inside := [x for x in self.all_writes if lo <= x < hi])
            ),
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
        lo, hi = self.store.first, self.store.end
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
    env_ref: dict[str, str] = {}
    env_run: dict[str, str] = {}
    pin = t.get(
        "pcm_sha256", ""
    )  # the run-time note is compared with this hash, not with a twin's note
    plain = bool(
        t.get("ref_plain")
    )  # the patched-in set does not fit next to the runtime's DSP area (DN2 pluck)
    ref_img = (c.image_for(twin if plain else RT + "," + twin, "ref")) if twin else None
    card = c.card(mods)
    out: dict[str, Any] = {}

    def select(rig: Any, hint_key: str) -> None:
        if c.dt2:
            return
        if play in (
            "pluck",
            "sine",
        ):  # DN2: the machine list position (finding 33); the sound proves the type
            rig.select(t[hint_key] if hint_key in t else t["hint"])

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
            if play and not pin:
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
                        rig.dyn_type(M.MODULE, "pluck"),
                        t.get("hint_run", t.get("hint", 7)),
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
    for name in t.get(
        "ignore_run_machines", []
    ):  # a run-time module's machine with no twin: its machine row, its parameters and its pages
        gone = got["machines"].pop(name, None) or {}
        mine = [p for p in got["pages"] if p["machine"] == name]
        for k in list(gone.get("knobs", [])) + [k for p in mine for k in p["knobs"]]:
            got["params"].pop(k, None)
        got["pages"] = [p for p in got["pages"] if p["machine"] != name]
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
    if play and pin:
        rr = out["run_rig_checks"]
        M.pcm_pin(
            rr,
            out["run_pcm"],
            pin,
            c.report,
            "voice",
            t.get("pcm_samples", M.PIN_SAMPLES),
        )
        c.checks += rr.checks
    elif play:
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


def run_dkrun(
    c: Ctx, rig: B.BootRig, D: Any, blobs: dict[str, bytes], files: dict[str, str]
) -> None:
    """`tools.device.dkrun.run` against the emulator on a boot with no module: upload, SELECT, RESTART, the new boot's MOD_LOAD and MOD_DSP."""
    from tools.device import dkrun as R
    from tools.device import dkupload as DK
    from tools.patchkit import pk_digikit_upload as U

    (module,) = c.table()["modules"]
    (path,) = blobs
    dkm = c.work / "dkrun" / f"{module.split('/')[-1]}.dkm"
    dkm.parent.mkdir(exist_ok=True)
    dkm.write_bytes(blobs[path])
    up = DK.Uploader(U.transport(rig))
    steps = {"n": 0}

    def sleep(s: float) -> None:
        rig.emu.step(int(s * R.EMU_IPS))
        steps["n"] += int(s * R.EMU_IPS)

    out = R.run(
        up,
        module,
        wait_boot=lambda: D._wait_restart(rig),
        sleep=sleep,
        now=lambda: steps["n"] / R.EMU_IPS,
        seconds=0,
        dkm=dkm,
    )
    c.check(
        "dkrun: runtime 4 or newer",
        out["info"]["runtime"] >= R.MIN_RUNTIME,
        str(out["info"]),
    )
    c.check("dkrun: the module was uploaded", out["uploaded"] and out["path"] == path)
    res = {(d["kind"], d["args"][0]): d["args"][1] for d in out["results"]}
    c.check(
        "dkrun: after the reboot MOD_LOAD result=1 (LOADING) and MOD_DSP result=0",
        res.get(("MOD_LOAD", 0)) == 1 and res.get(("MOD_DSP", 0)) == 0,
        str(res),
    )
    rec = B.current(rig.sel_records())
    c.check(
        "dkrun: SELECT wrote the record: the module and nothing else",
        rec is not None and rec["paths"] == [path],
        str(rec),
    )
    # a second run finds the file on the device (the same sha256) and uploads nothing (it stops after SELECT: one boot is enough here)
    out2 = R.run(
        DK.Uploader(U.transport(rig)),
        module,
        sleep=sleep,
        now=lambda: steps["n"] / R.EMU_IPS,
        seconds=0,
        dkm=dkm,
        restart=False,
    )
    c.check("dkrun: a second run uploads nothing (same sha256)", not out2["uploaded"])


def run_transfer(
    c: Ctx, rig: B.BootRig, D: Any, blobs: dict[str, bytes], files: dict[str, str]
) -> None:
    """`tools.device.dktransfer.install` against the emulator on a boot with no module: build only what changed (nothing: the committed
    objects are current), pack, upload, SELECT, RESTART, wait for MODULES. A second install builds and uploads nothing."""
    from tools.device import dkrun as R
    from tools.device import dktransfer as TR
    from tools.device import dkupload as DK
    from tools.patchkit import pk_digikit_upload as U

    (module,) = c.table()["modules"]
    (path,) = blobs
    steps = {"n": 0}

    def sleep(s: float) -> None:
        rig.emu.step(int(s * R.EMU_IPS))
        steps["n"] += int(s * R.EMU_IPS)

    def now() -> float:
        return steps["n"] / R.EMU_IPS

    up = DK.Uploader(U.transport(rig))
    rows = TR.repo_rows(up.info()["port"])
    before = TR.list_view(up, rows)
    row = next(r for r in before["modules"] if r["id"] == module)
    c.check(
        "transfer list: the module is listed, in a group, not on the card, needs no build",
        row["group"] == "Machines"
        and row["on_card"] == "no"
        and not row["loaded"]
        and row["build"] == "unchanged"
        and before["card_only"] == [],
        str(row),
    )
    events: list[dict[str, Any]] = []
    out = TR.install(up, [module], emit=events.append, rows=rows, select=True)
    c.check(
        "transfer install (no restart): nothing built (unchanged), uploaded, verified by the device, selected",
        out["built"] == []
        and out["uploaded"] == [module]
        and out["selected"] == [path]
        and not out["restarted"]
        and [e["ok"] for e in events if e["event"] == "verify"] == [True],
        str(events),
    )
    c.check(
        "transfer install (no restart): the module is not loaded yet",
        module not in [m["name"] for m in up.modules()["modules"]],
    )
    res = TR.restart_device(
        up,
        [module],
        emit=events.append,
        wait_boot=lambda: D._wait_restart(rig),
        sleep=sleep,
        now=now,
        paths=[path],
    )
    c.check(
        "transfer restart: MODULES lists the module after the boot",
        res["loaded"] == [module] and res["missing"] == [],
        str(res),
    )
    rec = B.current(rig.sel_records())
    c.check(
        "transfer install: SELECT wrote the module and nothing else",
        rec is not None and rec["paths"] == [path],
        str(rec),
    )
    up = DK.Uploader(U.transport(rig))
    after = TR.list_view(up, rows)
    row = next(r for r in after["modules"] if r["id"] == module)
    c.check(
        "transfer list after the install: on the card, loaded, fits",
        row["on_card"] == "yes" and row["loaded"] and row["fits"] is True,
        str(row),
    )

    plan = TR.plan_install(TR.repo_rows(up.info()["port"], [module]), [module], up.ls())
    c.check(
        "transfer install, second time: nothing to build or upload",
        plan["build"] == [] and plan["upload"] == [] and plan["select"] == [path],
        str(plan),
    )
    hdr = up.header(path)
    c.check(
        "READ: the file on the card has the .dkm header the repo packed",
        hdr == Path(row["dkm"]).read_bytes()[: DK.HEADER_BYTES],
    )


def memory_rows(cap: dict[str, dict[str, int]]) -> list[str]:
    """The rows the MEMORY page shows for a CAPACITY reply (modules/core/digikit-menu `fill_mem`): label, ten-character bar, percent or used/limit."""
    out = []
    for kind, label, pct in (
        ("DSP_AREA", "DSP   ", True),
        ("ARENA", "CPU   ", True),
        ("MODULES", "MODS  ", False),
        ("MACHINE_ROWS", "MACH  ", False),
        ("VOICE_ROWS", "VOICE ", False),
        ("PARAM_ROWS", "PARAM ", False),
        ("PAGE_ROWS", "PAGE  ", False),
    ):
        e = cap.get(kind)
        if e is None or e["limit"] == 0:
            continue
        used = min(e["used"], e["limit"])
        full = used * 10 // e["limit"]
        bar = "|" * full + "." * (10 - full)
        tail = f"{used * 100 // e['limit']}%" if pct else f"{e['used']}/{e['limit']}"
        out.append(f"{label}{bar} {tail}")
    return out


def check_memory_page(c: Ctx, rig: B.BootRig, plan: Any) -> None:
    """SETTINGS > DIGIKIT > MEMORY on a boot that loaded a machine: the rows the menu kept in `DK_MEM` equal the rows computed from the
    CAPACITY reply of the same boot (so the DSP percent is the same figure rounded the same way), and MACH and PARAM show the machine (a page row only where the device has page rows: the equality covers it)."""
    from tools.device import dkupload as DK
    from tools.patchkit import pk_digikit_upload as U

    def tap(key: str, gap_ms: int = 350) -> None:
        rig.s.do_tap(key)
        rig.s.run_ms(gap_ms)

    tap("NO", 400)  # SETTINGS > DIGIKIT (the ninth row) > MEMORY (the fifth row)
    tap("SETTINGS", 600)
    for _ in range(8):
        tap("DOWN", 200)
    tap("YES", 700)
    for _ in range(4):
        tap("DOWN", 200)
    tap("YES", 700)
    base, n = plan.addr("DK_MEM"), rig.w(plan.addr("DK_MEM_N"))
    got = []
    for i in range(n):
        raw = b"".join(
            rig.w(base + 24 * i + 4 * k).to_bytes(4, "big") for k in range(6)
        )
        got.append(raw.split(b"\0")[0].decode("latin-1"))
    from tools.control.digi_client import oled_png

    (c.work / "memory-page.png").write_bytes(
        oled_png(rig.s.frame()[1])
    )  # with `--keep`: to look at the labels
    cap = DK.Uploader(U.transport(rig)).capacity()
    want = memory_rows(cap)
    c.check(
        "MEMORY page: the rows equal the CAPACITY reply of the same boot, DSP row included",
        got == want and any(r.startswith("DSP   ") for r in got),
        f"page {got}; want {want}",
    )
    used = {k: cap[k]["used"] for k in ("MACHINE_ROWS", "PARAM_ROWS")}
    c.check(
        "MEMORY page: MACH and PARAM show the loaded machine",
        all(v >= 1 for v in used.values())
        and all(
            any(r.startswith(f"{k} ") and f" {cap[kk]['used']}/" in r for r in got)
            for k, kk in (("MACH", "MACHINE_ROWS"), ("PARAM", "PARAM_ROWS"))
        ),
        f"{used}; {got}",
    )


def run_menu(c: Ctx) -> None:
    """Through the menu: boot with an empty selection, SETTINGS > DIGIKIT > MODULES, tick the set, APPLY & RESTART (two presses), and
    after the boot compare with the patched-in set as `equiv` does (rows, machine in the list, PCM of a note). The set may hold a
    code-only module next to the machine."""
    from tools.coldfire import digikit_menu_proof as D

    t = c.table()
    names = {m: m.split("/")[-1] for m in t["modules"]}
    files = {m: c.pack(m, f"{names[m]}.dkm") for m in t["modules"]}
    rows = sorted(names.values())  # the menu lists the store's files in name order
    dkrun = bool(
        t.get("dkrun")
    )  # the selection and the restart come from `tools.device.dkrun` (SELECT, RESTART), not from the menu
    transfer = bool(t.get("transfer"))  # dktransfer.install does what dkrun does
    upload = bool(t.get("upload")) or dkrun or transfer
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
        if upload and not dkrun and not transfer:
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
        if transfer:
            run_transfer(c, rig, D, blobs, files)
        elif dkrun:
            run_dkrun(c, rig, D, blobs, files)
        else:
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
                "the pending selection is the whole set",
                tick == [1] * len(rows),
                str(tick),
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
            rig.select_type(
                rig.dyn_type(M.MODULE, "pluck"), t.get("hint_run", t.get("hint", 7))
            )
        else:
            rig.select(t.get("hint_run", t["hint"]))
        # the DSP block count runs on through the panel script and the restart: the note starts a little after the setup
        pcm = rig.note(c.work / "run.wav", max(trig, rig.frames() + 300))
        M.pcm_check(rig, ref_pcm, pcm, c.report, "machine" if c.dt2 else "voice")
        check_memory_page(c, rig, plan)
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


def run_rungc(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_rungc as RC

    RC.run_rungc(c)


def run_pages(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_pages as PG

    PG.run_pages(c)


def run_fx(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fx as FX

    FX.run_fx(c)


def run_fxsend(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxsend as FXS

    FXS.run_fxsend(c)


def run_fxroute(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxroute as FR

    FR.run_fxroute(c)


def run_fxroutes(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxroutes as FRS

    FRS.run_fxroutes(c)


def run_settings(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_settings as SET

    SET.run_settings(c)


def run_fxpage(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxpage as FP

    FP.run_fxpage(c)


def run_fxpersist(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxpersist as FPS

    FPS.run_fxpersist(c)


def run_fxshift(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_fxshift as FSH

    FSH.run_fxshift(c)


def live_boot(c: Ctx, tag: str = "", patch_set: str | None = None) -> tuple[Any, Path]:
    """A coupled daemon free-running on a stock image with the runtime patched in (the DT2 on a card built from a folder, the DN2 on none),
    started with `--mirror-dir`; returns it and the mirror folder. The caller closes it."""
    from tools.control.digi_client import Emu
    from tools.firmware import config

    dev, ver = c.image.split("-")
    spec = H.FIRMWARE[(dev, ver)]
    syx = config.stock(spec["syx"])
    mirror = (
        c.work / "mirror" / dev
    )  # the plus_drive/<device>/ folder the module store is mirrored to
    emu = Emu.spawn_coupled(
        syx,
        H.ROOT / spec["ready"],
        "--mirror-dir",
        str(mirror),
        timeout=300,
        binary=H.daemon_binary(dev, syx),
        run_dir=c.work / f"live-run{tag}",
    )
    try:
        if dev == "dt2":  # the DT2 boots on a card built from a folder; the DN2 on none
            from tools.emulator import card_check

            samples = H.OUT / "samples"
            card_check.make_samples(samples)
            card = emu.call("card.load", dir=str(samples))["card"]
            c.live_card = card
            emu.load(syx, runtime=True, start="auto", card=card, patch_set=patch_set)
        else:
            emu.load(syx, runtime=True, start="auto", patch_set=patch_set)
        emu.call("run.mode", mode="free")
        time.sleep(float(c.table().get("settle_s", 12)))
        c.check("the daemon runs free", emu.status().get("mode") == "free")
    except BaseException:
        emu.__exit__(None, None, None)
        raise
    return emu, mirror


def live_tool(emu: Any) -> Callable[..., tuple[int, list[dict[str, Any]], float]]:
    """`tools.device.dktransfer` as a command line against the daemon: (return code, JSON events, seconds)."""
    import subprocess

    from tools import python_env

    def tool(*args: str) -> tuple[int, list[dict[str, Any]], float]:
        t0 = time.monotonic()
        r = subprocess.run(
            [sys.executable, "-m", "tools.device.dktransfer", "--emu"]
            + ["--socket", emu.path, "--json", *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=python_env(),
            timeout=300,
        )
        ev = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        return r.returncode, ev, time.monotonic() - t0

    return tool


def run_live_svc(c: Ctx) -> None:
    """A module that registers SysEx commands (Api.register_cmds): `test/svc-echo` is installed and loaded by the runtime, then its commands
    answer over SysEx (an echo, a status of its own), the refusals it provoked at init are the ones the runtime must give, an unregistered
    command is E_PROTO, and INFO says the runtime has the capability."""
    from tools.device import dkproto
    from tools.device import dktransfer as TR
    from tools.device import dkupload as DK

    (module,) = c.table()["modules"]
    emu, _mirror = live_boot(c)
    try:
        tool = live_tool(emu)
        rc, ev, _ = tool("install", module, "--restart")
        res = next((e for e in ev if e["event"] == "result"), {})
        c.check(
            "install --restart: the module that registers commands loaded",
            rc == 0 and module in res.get("loaded", []),
            f"rc {rc} {ev[-1:] if ev else ''}",
        )
        up = DK.Uploader(TR.LiveTransport(emu))
        info = up.info()
        c.check(
            "INFO: the capability for service commands",
            DK.has_cap(info, "SERVICE_CMDS"),
            str(info["caps"]),
        )
        lo, hi = dkproto.RANGES["remote_state"]
        c.check(
            "the remote state range is reserved",
            (lo, hi) == (0x3E, 0x3F),
            str((lo, hi)),
        )
        echo, refusals = 0x3E, 0x3F
        status, body = up.request(echo, b"hello \x80\xff")
        c.check(
            "an echo command: the reply body is the request body (8-bit bytes too)",
            status == 0 and body == b"hello \x80\xff",
            f"{status} {body!r}",
        )
        status, body = up.request(echo, b"")
        c.check("an echo of nothing", status == 0 and body == b"", f"{status} {body!r}")
        status, body = up.request(echo, b"\xee")
        c.check(
            "a status of the module's own, no body",
            status == -77 and body == b"",
            f"{status} {body!r}",
        )
        status, body = up.request(echo, bytes(range(150)))
        c.check(
            "a 150-byte body", status == 0 and body == bytes(range(150)), f"{status}"
        )
        status, body = up.request(echo, bytes(range(250)))
        c.check(
            "a 250-byte body", status == 0 and body == bytes(range(250)), f"{status}"
        )
        status, body = up.request(echo, bytes(300))
        c.check(
            "a body over 256 bytes is refused (E_MSG)",
            status == -50,
            f"{status}",
        )
        status, body = up.request(refusals)
        e_range = 0xC0  # E_CMD_RANGE = -64, its low byte
        c.check(
            "the runtime refused what it had to: outside a reserved range, over a registered one, an empty range",
            status == 0 and list(body) == [e_range] * 3,
            f"{status} {list(body)}",
        )
        status, _ = up.request(0x3D)
        c.check("a command nobody registered is E_PROTO", status == -60, str(status))
    finally:
        emu.__exit__(None, None, None)


def run_live_samples(c: Ctx) -> None:
    """A sample sent to the +Drive over the stock MidiRpc file commands by the TypeScript client (tools/devtools/samples_live.mjs, which also
    converts a WAV with dksample.wasm and checks the firmware's content hash against the host's), on a free-running Digitakt II. Then the card:
    the guest's writes laid over the card, read by `plusdrive fs`, hold the file the card builder makes of the same WAV, the host's bytes plus the
    firmware's 16-byte trailer, with the hash the firmware reported in the record."""
    import subprocess

    from tools.emulator import card_overlay

    out = c.work / "samples-out"
    emu, _mirror = live_boot(c, "-samples")
    try:
        r = subprocess.run(
            ["node", "tools/devtools/samples_live.mjs", emu.path, "--out", str(out)],
            capture_output=True, text=True, cwd=ROOT, timeout=600,
        )  # fmt: skip
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        for ln in lines:
            if "check" in ln:
                c.check(f"client: {ln['check']}", ln["ok"], ln["detail"])
        info = next((ln for ln in lines if "hash" in ln), {})
        c.check(
            "the client ran to the end and every check held",
            r.returncode == 0 and bool(info),
            r.stderr[-300:],
        )
        c.report["firmware_hash"] = info.get("hash")
        c.report["compared"] = info.get("compared")
        ov = c.work / "samples.ov.bin"
        emu.call("card.overlay", path=str(ov))
    finally:
        emu.__exit__(None, None, None)
    tools = c.env.tools
    after = c.work / "samples-after.img"
    card_overlay.apply(Path(c.live_card or ""), ov, after)

    def cat(
        image: Path, path: str
    ) -> Any:  # the bytes of a file: `tools.run` returns text
        return subprocess.run(
            [str(tools.plusdrive), "fs", "cat", str(image), path],
            capture_output=True, cwd=ROOT, env=python_env(),
        )  # fmt: skip

    got = cat(after, "/live/test")
    sent = (out / "sent-test.bin").read_bytes()
    c.check(
        "the card holds the bytes sent plus a 16-byte zero trailer",
        got.returncode == 0 and got.stdout == sent + bytes(16),
        f"{len(got.stdout)} bytes, sent {len(sent)}",
    )
    folder = c.work / "built" / "plus_drive" / "dt2"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out / "test.wav", folder / "test.wav")
    built = c.work / "built.img"
    b = tools.run(
        tools.plusdrive, "build", folder, "-o", built, "--device", "dt2", "--no-project"
    )
    want = cat(built, "/test")
    c.check(
        "the card equals the file the +Drive build makes of the same WAV",
        b.returncode == 0
        and want.returncode == 0
        and got.stdout == want.stdout
        and len(want.stdout) > 0x50,
        f"{len(got.stdout)} vs {len(want.stdout)} bytes {b.stderr[-200:]}",
    )
    ls = tools.run(tools.plusdrive, "fs", "ls", after)
    row = next(
        (
            ln.split("\t")
            for ln in ls.stdout.splitlines()
            if ln.startswith("live/test\t")
        ),
        [],
    )
    c.check(
        "the record's hash|1 and size (without the trailer) are the ones the client saw",
        len(row) == 3
        and int(row[2], 16) == info.get("hash")
        and int(row[1]) == len(sent) + 16,
        str(row),
    )
    c.check(
        "the card keeps no deleted file (renamed.txt) and the text file is gone",
        not any(
            ln.startswith("live/notes.txt") or ln.startswith("live/renamed.txt")
            for ln in ls.stdout.splitlines()
        ),
    )


def run_live_samples_store(c: Ctx) -> None:
    """A sample sent into the `samples/` folder of the digikit store by the TypeScript samples API (tools/devtools/samples_store_live.mjs, which also
    converts a WAV with dksample.wasm, checks VERIFY against the host's sha256, a delete, and a RESTART), on a free-running emulator whose profile
    says `[storage] samples = "digikit"`. Then the card mirror: the folder the emulator keeps of the store holds the file the client sent."""
    import subprocess

    out = c.work / "samples-store-out"
    storage = pk_device.load(c.env.device)["storage"]["samples"]
    c.check(
        'the device profile says [storage] samples = "digikit"',
        storage == "digikit",
        storage,
    )
    emu, mirror = live_boot(c, "-samples-store")
    try:
        r = subprocess.run(
            ["node", "tools/devtools/samples_store_live.mjs", emu.path, "--out", str(out),
             "--storage", storage, "--total-kib", str(c.store.limit // 2)],
            capture_output=True, text=True, cwd=ROOT, timeout=900,
        )  # fmt: skip
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        for ln in lines:
            if "check" in ln:
                c.check(f"client: {ln['check']}", ln["ok"], ln["detail"])
        res = next((ln for ln in lines if "sha256" in ln), {})
        c.check(
            "the client ran to the end and every check held",
            r.returncode == 0 and bool(res),
            r.stderr[-300:],
        )
        c.report["compared"] = res.get("compared")
        synced = emu.call("card.sync")
        mirrored = mirror / "digikit" / "samples" / "drums" / "test"
        sent = (
            (out / "stored-test.bin").read_bytes()
            if (out / "stored-test.bin").is_file()
            else b""
        )
        c.check(
            "the card mirror holds the file the client sent (samples/drums/test)",
            bool(sent) and mirrored.is_file() and mirrored.read_bytes() == sent,
            f"{mirrored} {synced.get('files')}",
        )
    finally:
        emu.__exit__(None, None, None)


def run_live_input(c: Ctx) -> None:
    """core/input against the emulator's own input path. The same presses are made on two free-running daemons with the same boot history (the probe installed, one restart):
    on the first with the emulator's `input.button` / `input.turn` (the panel UART path), on the second through `test/input-probe` (SysEx
    commands 0x38 key and 0x39 turn that call core/input; installed and loaded first). The screens must be the same after each step: the
    firmware's decoders saw the same presses. A turn shows on the screen only some seconds later (the firmware flushes it on a tick), and the
    emulator runs slower than real time, so each step waits `settle` seconds. The codes and the encoder come from the scenario file, the layout
    of the codes from the device file."""
    from tools.device import dktransfer as TR
    from tools.device import dkupload as DK

    t = c.table()
    (module,) = t["modules"]
    key_a, key_b, enc, detents = t["key_a"], t["key_b"], t["encoder"], t["detents"]

    def sequence(
        emu: Any, key: Callable[[int], None], turn: Callable[[int], None]
    ) -> list[str]:
        out = []
        for act in (lambda: key(key_a), lambda: turn(detents), lambda: key(key_b)):
            act()
            time.sleep(float(t.get("settle", 10)))
            out.append(str(emu.frame_hash()))
        return out

    def boot_with_probe(tag: str) -> Any:
        """A daemon with the probe installed and the device restarted once: both runs have the same boot history (the firmware's first boot and a soft
        reset boot are not the same state, so a screen after the same keys differs between them)."""
        emu, _ = live_boot(c, tag)
        try:
            rc, ev, _ = live_tool(emu)("install", module, "--restart")
            res = next((e for e in ev if e["event"] == "result"), {})
            c.check(
                f"install --restart ({tag[1:]}): the probe loaded (it imports input_key and input_turn: core/input is in the image)",
                rc == 0 and module in res.get("loaded", []),
                f"rc {rc} {ev[-1:] if ev else ''}",
            )
            time.sleep(float(t.get("settle", 10)))
        except BaseException:
            emu.__exit__(None, None, None)
            raise
        return emu

    emu = boot_with_probe("-stock")
    try:

        def stock_key(code: int) -> None:
            emu.button(code, True)
            time.sleep(0.3)
            emu.button(code, False)

        stock = sequence(emu, stock_key, lambda n: emu.turn(enc, n))
    finally:
        emu.__exit__(None, None, None)
    c.check(
        "the emulator's path: each step changes the screen",
        len(set(stock)) == 3,
        " ".join(h[:8] for h in stock),
    )

    emu = boot_with_probe("-probe")
    try:
        up = DK.Uploader(TR.LiveTransport(emu))
        answers: list[int] = []

        def probe_key(code: int) -> None:
            answers.append(up.request(0x38, bytes((code, 1)))[0])
            time.sleep(0.3)
            answers.append(up.request(0x38, bytes((code, 0)))[0])

        def probe_turn(n: int) -> None:
            answers.append(up.request(0x39, bytes((enc, n & 0xFF)))[0])

        probe = sequence(emu, probe_key, probe_turn)
        c.check("every probe command answered ok", set(answers) == {0}, str(answers))
        for name, a, b in zip(
            ("a key", "a turn", "a second key"), probe, stock, strict=True
        ):
            c.check(
                f"{name} through core/input gives the screen the panel path gave",
                a == b,
                f"{a[:8]} {b[:8]}",
            )
        # refusals: a code that is not a key, an encoder that does not exist
        st, _ = up.request(0x38, bytes((200, 1)))
        c.check("a code that is no key is refused (-1)", st == -1, str(st))
        st, _ = up.request(0x39, bytes((99, 1)))
        c.check("an encoder that does not exist is refused (-1)", st == -1, str(st))
    finally:
        emu.__exit__(None, None, None)


def run_live_remote(c: Ctx) -> None:
    """digikit/remote and the REMOTE switch, on a daemon booted with the `device` patch set (core/input in it, core/remote-open not): the switch is off
    at the boot. The module is installed and loaded; STATE answers (it is never guarded) and says off; KEY, TURN and RELEASE are refused with
    E_DISABLED (-65). The switch is turned on in the menu (SETTINGS > DIGIKIT > REMOTE, with the emulator's own button input): STATE says on and the commands work, TURN
    takes at most 30 detents and the rest after the module's pacing time, a malformed body is refused; the menu turns the switch off again."""
    from tools.device import dktransfer as TR
    from tools.device import dkupload as DK

    t = c.table()
    (module,) = t["modules"]
    emu, _ = live_boot(c, "-remote", patch_set=t.get("patch_set"))
    try:
        rc, ev, _ = live_tool(emu)("install", module, "--restart")
        res = next((e for e in ev if e["event"] == "result"), {})
        c.check(
            "install --restart: digikit/remote loaded (it imports input_key and input_turn: core/input is in the image)",
            rc == 0 and module in res.get("loaded", []),
            f"rc {rc} {ev[-1:] if ev else ''}",
        )
        time.sleep(float(t.get("settle", 10)))
        up = DK.Uploader(TR.LiveTransport(emu))
        enc, key = t["encoder"], t["key"]

        def state() -> list[int]:
            st, body = up.request(0x3E)
            return [st, *body]

        def cmd(n: int, body: bytes = b"") -> tuple[int, bytes]:
            st, b = up.request(n, body)
            return st, bytes(b)

        c.check(
            "STATE with the switch off: enabled 0, format 1, turn limit 30",
            state() == [0, 0, 1, 30],
            str(state()),
        )
        refused = [
            cmd(0x38, bytes((key, 1)))[0],
            cmd(0x39, bytes((enc, 1)))[0],
            cmd(0x3A)[0],
        ]
        c.check(
            "KEY, TURN and RELEASE are refused with E_DISABLED (-65) while the switch is off",
            refused == [-65] * 3,
            str(refused),
        )

        def tap(name: str, wait: float = 1.2) -> None:
            code = H.button(name)
            emu.button(code, True)
            time.sleep(0.4)
            emu.button(code, False)
            time.sleep(wait)

        def menu_remote() -> None:
            """SETTINGS > DIGIKIT (the ninth row) > REMOTE (the seventh row of the DIGIKIT page), one press: the switch toggles."""
            tap("NO", 2)
            tap("SETTINGS", 3)
            for _ in range(8):
                tap("DOWN")
            tap("YES", 3)
            for _ in range(6):
                tap("DOWN")
            tap("YES", 2)
            tap("NO", 2)

        menu_remote()
        c.check(
            "the menu's REMOTE row turned the switch on: STATE says 1",
            state() == [0, 1, 1, 30],
            str(state()),
        )
        r = [cmd(0x38, bytes((key, 1))), cmd(0x38, bytes((key, 0))), cmd(0x3A)]
        c.check(
            "KEY down, KEY up and RELEASE answer ok",
            [x[0] for x in r] == [0, 0, 0],
            str(r),
        )
        c.check(
            "a malformed KEY and an encoder that does not exist are refused (E_MSG -50)",
            [
                cmd(0x38, b"\x01")[0],
                cmd(0x38, bytes((key, 2)))[0],
                cmd(0x39, bytes((0, 1)))[0],
            ]
            == [-50] * 3,
        )
        c.check(
            "a key that is no key is refused by the adapter (-1)",
            cmd(0x38, bytes((127, 1)))[0] == -1,
        )
        st1, b1 = cmd(0x39, bytes((enc, 100)))
        st2, b2 = cmd(0x39, bytes((enc, 100)))
        c.check(
            "TURN 100: the module takes 30, and 0 straight after (the firmware's clamp is respected)",
            (st1, b1, st2, b2) == (0, bytes((30,)), 0, bytes((0,))),
            f"{(st1, b1, st2, b2)}",
        )
        time.sleep(float(t.get("pace_wait", 3)))
        st3, b3 = cmd(0x39, bytes((enc, 0x100 - 100)))
        c.check(
            "after the pacing time a turn back is taken (negative, within 30)",
            st3 == 0 and len(b3) == 1 and 1 <= 256 - b3[0] <= 30,
            f"{(st3, b3)}",
        )
        menu_remote()
        c.check(
            "the menu's REMOTE row turned the switch off again: STATE says 0, KEY is refused",
            state() == [0, 0, 1, 30] and cmd(0x38, bytes((key, 1)))[0] == -65,
            str(state()),
        )
    finally:
        emu.__exit__(None, None, None)


def run_live(c: Ctx) -> None:
    """The desktop app's setup: a coupled daemon free-running on a stock image with the runtime patched in and NO card file, driven
    by the `dktransfer` command line in real time (nothing steps it). An upload and a RESTART must survive the soft reset: the
    guest's card writes are laid over a default blank card when the firmware was loaded without one."""
    from tools.device import dklog as L
    from tools.device import dktransfer as TR
    from tools.device import dkupload as DK

    if c.table().get("part") == "svc":
        return run_live_svc(c)
    if c.table().get("part") == "samples":
        return run_live_samples(c)
    if c.table().get("part") == "samples-store":
        return run_live_samples_store(c)
    if c.table().get("part") == "input":
        return run_live_input(c)
    if c.table().get("part") == "remote":
        return run_live_remote(c)
    (module,) = c.table()["modules"]
    dev, _ = c.image.split("-")
    emu, mirror = live_boot(c)
    try:
        epoch0 = emu.status()["epoch"]
        tool = live_tool(emu)

        rc, ev, secs = tool("install", module, "--restart")
        res = next((e for e in ev if e["event"] == "result"), {})
        c.check(
            "dktransfer install: the restart kept the upload and the selection, MODULES lists the module",
            rc == 0 and module in res.get("loaded", []),
            f"rc {rc} {ev[-1:] if ev else ''}",
        )
        c.report["install_s"] = round(secs, 1)
        c.check("the emulator booted again (epoch)", emu.status()["epoch"] > epoch0)
        up = DK.Uploader(TR.LiveTransport(emu))
        c.check(
            "the file is on the card after the soft reset",
            [p for p, _, _ in up.ls()] == [f"modules/{module.split('/')[-1]}.dkm"],
            str(up.ls()),
        )
        # the module store follows into the folder about 2 s after the guest's last card write, with the same sha256
        path = f"modules/{module.split('/')[-1]}.dkm"
        want = dict((p, h) for p, _, h in up.ls())[path]
        mirrored = mirror / "digikit" / path

        def sha_of(f: Path) -> str | None:
            return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None

        end = time.monotonic() + 30
        while time.monotonic() < end and sha_of(mirrored) != want:
            time.sleep(0.5)
        c.check(
            "the folder holds the module with the card's sha256 (the emulator mirrors the store after its quiet period)",
            sha_of(mirrored) == want,
            str(mirrored),
        )
        fold = emu.call("card.folder")
        c.check(
            "card.folder lists it with size and sha256",
            fold["folder"] == str(mirror)
            and fold["continuous"]
            and [(f["path"], f["sha256"]) for f in fold["files"]] == [(path, want)],
            str(fold),
        )
        # delete the selected file: DELETE does not touch the selection, so the next boot finds a selected path whose file is gone and
        # skips it with an error line (MOD_LOAD result -1) instead of failing the boot
        # (the boot after `install --restart` is the second boot with a selection that has not run the stable period, 30 s of DSP blocks:
        # one more such boot would be a safe boot, so the scenario restarts once more only)
        rc3, ev3, _ = tool("delete", path)
        c.check(
            "dktransfer delete: the file is gone from the card",
            rc3 == 0 and any(e["event"] == "delete" for e in ev3) and up.ls() == [],
            f"rc {rc3} {ev3[-1:] if ev3 else ''}",
        )
        # ... and the folder follows the card: the file is removed (card.sync does not wait for the quiet period)
        synced = emu.call("card.sync")
        c.check(
            "card.sync removes the deleted file from the folder",
            not mirrored.exists() and synced["files"] == [],
            str(synced),
        )
        rc4, ev4, _ = tool("restart")
        up = DK.Uploader(TR.LiveTransport(emu))
        _, _, evs = L.read_all(up)
        c.check(
            "the boot after the delete skips the selected path (MOD_LOAD E_NOT_FOUND) and reaches the UI",
            rc4 == 0
            and any(e[2] == L.K["MOD_LOAD"] and e[4][1] == 0xFFFFFFFF for e in evs)
            and any(e[2] == L.K["UI_FIRST"] for e in evs)
            and [m["name"] for m in up.modules()["modules"]] == [],
            f"rc {rc4} {ev4[-2:]} "
            + " | ".join(
                L.text_of(L.event_dict(e)).strip()
                for e in evs
                if e[2] != L.K["DSP_DIAG"]
            ),
        )
    finally:
        emu.__exit__(None, None, None)


KINDS = {
    "live": run_live,
    "pages": run_pages,
    "fx": run_fx,
    "fxsend": run_fxsend,
    "fxroute": run_fxroute,
    "fxroutes": run_fxroutes,
    "settings": run_settings,
    "fxpage": run_fxpage,
    "fxpersist": run_fxpersist,
    "fxshift": run_fxshift,
    "card": run_card,
    "debug": run_debug,
    "crash": run_crash,
    "rungc": run_rungc,
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
