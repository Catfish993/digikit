#!/usr/bin/env python3
"""Rehearsal of rung C on the Digitakt II, in the coupled emulator, with the images and commands Em uses on the device
(docs/PATCHING.md "Rung C"; docs/findings/48-dsp-breadcrumbs.md). Run by `tools/patchkit/pk_digikit_scen.py` as the `rungc` kind.

The rung C image (`core/runtime,core/digikit-menu,core/health,core/dynreg,core/dyndsp`) boots from a card with a sample filesystem (a few
WAV files) and a blank module store. Then, in this order, what Em does:

  1. `dkupload info` and `dkupload put out/patch/pluck-dt2.dkm` (the command line of tools/device/dkupload.py, with `--emu --run-dir`
     where Em types `--port Digitakt`); the sectors the upload wrote are all inside the store range;
  2. SETTINGS > DIGIKIT > MODULES, tick the module, APPLY & RESTART (two presses);
  3. after the restart: ABOUT and the log say the module loaded, RAM was kept, HEALTH is sane;
  4. FUNC + SRC, choose the run-time pluck machine on track 1, hold TRIG 1: the note equals the patched-in `test/patched-pluck` sample for
     sample; the DSP's counters say that every dispatch to type 18 returned; no DSP_STOPPED.

Screenshots of the OLED and the log files go to `out/runs/lanes/b-rungC/` (never committed).
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import struct
import wave
from pathlib import Path
from typing import Any

from tools.device import dklog as L
from tools.device import dkupload as DK
from tools.patchkit import pk_digikit_debug as DBG
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit import pk_harness as H

ROOT = Path(__file__).resolve().parents[2]
RUNG_C = "core/runtime,core/digikit-menu,core/health,core/dynreg,core/dyndsp"
DKM = ROOT / "out" / "patch" / "pluck-dt2.dkm"
PLUCK_HINT = 7


def out_dir() -> Path:
    d = Path(
        os.environ.get("DIGIKIT_RUNGC_OUT", ROOT / "out" / "runs" / "lanes" / "b-rungC")
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_wav(path: Path, hz: float, seconds: float, noise: bool = False) -> None:
    """A mono 16-bit 48 kHz WAV: a decaying sine (or noise burst), enough for the sample filesystem to have a few files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n, x = int(48000 * seconds), 0x2545F491
    frames = bytearray()
    for i in range(n):
        env = math.exp(-5.0 * i / n)
        if noise:
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= x >> 17
            x ^= (x << 5) & 0xFFFFFFFF
            v = ((x & 0xFFFF) - 32768) / 32768
        else:
            v = math.sin(2 * math.pi * hz * i / 48000)
        frames += struct.pack("<h", int(0.5 * env * v * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(bytes(frames))


def sectors_written(rig: Any) -> set[int]:
    """Every card sector the guest has written so far (the overlay of the card)."""
    from tools.coldfire import arena_safety

    ov = rig.work / f"{rig.tag}.rungc-overlay.bin"
    rig.emu.call("card.overlay", path=str(ov))
    return set(arena_safety.parse_overlay(ov.read_bytes()))


class Reporter:
    """`pcm_check` reports through an object with `check` and `checks`."""

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))


@contextlib.contextmanager
def _env(name: str, value: str | None):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def run_rungc(c: Any) -> None:
    """The scenario. `[rungc] uninit = "HEXSEED"` runs every emulator with SHARC_UNINIT (finding 49): SHARC memory that nothing loaded or
    wrote reads as a seeded non-zero pattern instead of zeros. `[rungc] variant = "MODULE"` with `defines = ["NAME"]` runs it with that module
    built with those options (tools/patchkit/pk_variant.py; the bisect of finding 49, docs/PATCHING.md "Rung C2"): the machine's DSP routine is
    never called and the checks of step 4 expect the stock path for the added type (breadcrumb stage 3) instead of dispatches; with
    `routine = true` the variant keeps the registered path (the C3 variants of finding 49 section 9) and the checks are those of the plain rung C.
    `[rungc] irq_probe = "tick"|"sft3"|"both"|"HEXMASK"` (with `irq_every = N`) runs the main image with SHARC_IRQ_PROBE over the wrapper's code:
    the core takes the kernel's interrupts between any two instructions of the hook (finding 49 section 9; no patched-in twin, no PCM comparison)."""
    d = c.scen.data["rungc"]
    seed, variant = d.get("uninit"), d.get("variant")
    root = None
    if variant:
        from tools.patchkit import pk_variant

        root = str(pk_variant.make(variant, list(d["defines"]), c.scen.name))
        c.report["variant"] = {"module": variant, "defines": d["defines"], "root": root}
    if seed:
        c.report["sharc_uninit"] = str(seed)
    with _env("SHARC_UNINIT", str(seed) if seed else None), _env("DIGI_PK_ROOT", root):
        return _run_rungc(
            c,
            bool(variant) and not d.get("routine"),
            d.get("irq_probe"),
            int(d.get("irq_every", 1)),
        )


PROBE_MASKS = {"tick": 0x400000, "sft3": 0x80000000, "both": 0x80400000}


def _run_rungc(
    c: Any, bisect: bool = False, probe: str | None = None, every: int = 1
) -> None:
    from tools.coldfire import digikit_menu_proof as D

    shots = out_dir()
    for f in shots.glob("*"):
        if f.is_file():
            f.unlink()
    pngs: dict[str, str] = {}

    # ---- the files: the image, the module, the card with samples ------------------------------------------------------------
    DKM.parent.mkdir(parents=True, exist_ok=True)
    c.env.tools.patchkit_run(
        "pack", "digikit/pluck", "--device", "dt2", "-o", DKM
    )  # always: a kept file from an older packer has other type fields
    img = c.image_for(RUNG_C, "rungC")
    plan = D.Plan(img.plan)
    folder = c.env.folder
    write_wav(folder / "KICK.wav", 60.0, 0.4)
    write_wav(folder / "drums" / "SNARE.wav", 200.0, 0.3, noise=True)
    write_wav(folder / "loops" / "PAD.wav", 220.0, 1.5)
    card = c.env.card(
        None, tag="rungc"
    )  # built here, not cached by c.card (its key does not see the WAV files); the store range is blank
    c.report["rungc_image_sha256"] = (
        __import__("hashlib").sha256(img.syx.read_bytes()).hexdigest()
    )

    # ---- the twin: test/patched-pluck patched in, the PCM the run-time machine has to equal ----------------------------------------
    ref_img = (
        None
        if bisect or probe
        else c.image_for(M.MODULES_STATIC + ",test/patched-pluck", "ref")
    )
    ref: dict[str, Any] = {}

    def ref_boot() -> None:
        with c.rig(ref_img, card, "ref") as rig:
            rig.select_type(M.PLUCK_STATIC_TYPE, PLUCK_HINT)
            ref["pcm"] = rig.note(c.work / "ref.wav", M.TRIG_FRAME)
            c.done(rig)

    def snap(rig: Any, name: str) -> None:
        p = shots / f"{name}.png"
        p.write_bytes(rig.emu.frame_png())
        pngs[name] = str(p)

    def tap(rig: Any, key: str, gap_ms: int = 350) -> None:
        rig.s.do_tap(key)
        rig.s.run_ms(gap_ms)

    def rig_run_dir(rig: Any) -> Path:
        return H.OUT / f"run-{rig.s.run_name}"

    def cli(fn: Any, rig: Any, *args: str) -> tuple[int, str]:
        """The tool's command line against this daemon: what Em types with `--port Digitakt`, here with `--emu --run-dir`."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn(["--emu", "--run-dir", str(rig_run_dir(rig)), *args])
        return rc, out.getvalue()

    def to_digikit(rig: Any) -> None:
        tap(rig, "NO", 400)
        tap(rig, "SETTINGS", 600)
        for _ in range(8):
            tap(rig, "DOWN", 200)
        tap(rig, "YES", 700)

    def log_to(rig: Any, name: str) -> list[Any]:
        f = shots / name
        rc, _ = cli(L.main, rig, "follow", "--once", "--out", str(f))
        assert rc == 0
        return DBG.events(rig)

    run_dirs: list[Path] = []

    def main_run() -> None:
        with c.rig(img, card, "rungc") as rig:
            run_dirs.append(rig_run_dir(rig))
            b0 = rig.boot_info()
            snap(rig, "00-main-screen")
            evs = log_to(rig, "boot1.log")
            chk = [e for e in evs if e[2] == L.K["CARD_CHECK"]]
            c.check(
                "rung C image, first boot, card with samples: no module, no safe mode, the store range is blank (CARD_CHECK blank)",
                b0["count"] == 0
                and b0["safe"] == 0
                and len(chk) == 1
                and chk[0][4][0] == 2,
                f"{b0['count']} modules, check {chk and chk[0][4]}",
            )
            before = sectors_written(rig)
            # ---- 1. the upload, as typed on the device --------------------------------------------------------------------------
            rc, text = cli(DK.main, rig, "info")
            c.check(
                "dkupload info answers and shows a free store",
                rc == 0 and "free_bytes" in text,
                text.replace("\n", "; ")[:160],
            )
            rc, text = cli(DK.main, rig, "put", str(DKM))
            c.check(
                "dkupload put out/patch/pluck-dt2.dkm: the sha256 is checked on the device",
                rc == 0 and "sha256 checked on the device" in text,
                text.strip(),
            )
            rc, text = cli(DK.main, rig, "ls")
            c.check(
                "dkupload ls lists the module",
                rc == 0 and "pluck-dt2.dkm" in text,
                text.strip(),
            )
            after = sectors_written(rig)
            new = sorted(after - before)
            outside = [s for s in new if not c.store.first <= s < c.store.end]
            c.check(
                "the upload's card writes are store sectors only",
                bool(new) and not outside,
                f"{len(new)} sectors written, {len(outside)} outside the store range ({hex(c.store.first)}..{hex(c.store.end)})",
            )
            # ---- 2. the menu ----------------------------------------------------------------------------------------------------
            to_digikit(rig)
            snap(rig, "01-digikit-menu")
            tap(rig, "YES", 900)  # MODULES
            snap(rig, "02-modules-before-tick")
            tick0 = bytes(rig.w(plan.addr("TICK")).to_bytes(4, "big"))[0]
            tap(rig, "YES", 500)  # tick
            snap(rig, "03-modules-after-tick")
            tick1 = bytes(rig.w(plan.addr("TICK")).to_bytes(4, "big"))[0]
            c.check(
                "MODULES lists the uploaded module; YES ticks it; APPLY & RESTART is offered",
                rig.w(plan.addr("NENT")) == 1
                and (tick0, tick1) == (0, 1)
                and rig.w(plan.addr("DK_OFFER")) == 1,
                f"entries {rig.w(plan.addr('NENT'))}, tick {tick0}->{tick1}, offer {rig.w(plan.addr('DK_OFFER'))}",
            )
            tap(rig, "DOWN", 300)  # the cursor to APPLY & RESTART
            n0 = rig.w(plan.addr("DK_POPUPS"))
            tap(rig, "YES", 700)  # first press: the popup
            snap(rig, "04-apply-popup")
            c.check(
                "first APPLY press: a popup, nothing saved",
                rig.w(plan.addr("DK_POPUPS")) == n0 + 1
                and rig.sel_records() == [None, None],
            )
            tap(rig, "NO", 500)
            rig.emu.drain_events()
            log_to(rig, "before-restart.log")
            tap(rig, "YES", 300)  # second press: save and restart
            D._wait_restart(rig)
            # ---- 3. after the restart ---------------------------------------------------------------------------------------------
            info = rig.boot_info()
            c.check(
                "the boot after APPLY & RESTART loads pluck (1 of 1, result 0, no safe mode)",
                info["count"] == 1
                and info["loaded"] == 1
                and info["results"] == [0]
                and info["safe"] == 0,
                str(info["results"]),
            )
            evs = log_to(rig, "boot2.log")
            loads = [e for e in evs if e[2] == L.K["MOD_LOAD"]]
            dsp_parts = [e for e in evs if e[2] == L.K["MOD_DSP"]]
            keeps = [e for e in evs if e[2] == L.K["RAM_KEEP"]]
            c.check(
                "the log has the LOAD event (file name; result 1 = the CF part loaded, the DSP part follows), the DSP part's event (result 0) and RAM_KEEP kept=1: RAM survived the menu restart",
                len(loads) == 1
                and loads[0][4][1] in (0, 1)
                and "dyn-plu" in L.ascii_of(*loads[0][4][2:5])
                and len(dsp_parts) == 1
                and dsp_parts[0][4][:2] == (0, 0)
                and len(keeps) == 1
                and keeps[0][4][0] == 1,
                f"loads {[e[4] for e in loads]}, dsp {[e[4] for e in dsp_parts]}, keep {[e[4] for e in keeps]}",
            )
            to_digikit(rig)
            tap(rig, "DOWN", 300)
            tap(rig, "YES", 900)  # ABOUT
            rows = DBG.about_rows(rig, plan)
            snap(rig, "05-about")
            c.check(
                "ABOUT: BOOT 1 OF 1 OK, the module, RAM KEPT: YES, no DSP STOPPED",
                "BOOT: 1 OF 1 OK" in rows
                and "RAM KEPT: YES" in rows
                and "DSP STOPPED" not in rows
                and any("PLUCK" in r for r in rows),
                str(rows),
            )
            tap(rig, "NO", 500)
            tap(rig, "DOWN", 300)
            tap(rig, "YES", 900)  # HEALTH
            rig.s.run_ms(1500)
            snap(rig, "06-health")
            hw = [rig.w(plan.addr("DK_HEALTH") + 4 * k) for k in range(15)]
            cpu, dsp_avg, dsp_max, heap_free = (
                hw[5] / 10,
                hw[11] / 10,
                hw[12] / 10,
                hw[14],
            )
            c.report["health_idle"] = {
                "cpu_pct": cpu,
                "dsp_avg_pct": dsp_avg,
                "dsp_max_pct": dsp_max,
                "heap_free": heap_free,
            }
            c.check(
                "HEALTH is sane: the CPU and DSP loads are figures below 100 %, the heap has megabytes free",
                hw[0] == 0x484C5448
                and 0 < dsp_avg < 100
                and 0 <= cpu <= 100
                and heap_free > 1_000_000,
                f"cpu {cpu} %, dsp {dsp_avg} % avg {dsp_max} % max, heap free {heap_free}",
            )
            tap(rig, "NO", 400)
            tap(rig, "NO", 400)
            tap(rig, "NO", 600)
            # ---- 4. the machine on a track, a note --------------------------------------------------------------------------------
            picked = None
            for downs in (PLUCK_HINT, PLUCK_HINT + 1, PLUCK_HINT - 1, PLUCK_HINT + 2):
                rig.s.do_tap(["FUNC", "SRC"])
                rig.s.run_ms(200)
                for _ in range(downs):
                    tap(rig, "DOWN", 80)
                snap(rig, f"07-machine-menu-{downs}-down")
                tap(rig, "YES", 300)
                tap(rig, "YES", 300)
                if rig.type_now() == rig.dyn_type(M.MODULE, "pluck"):
                    picked = downs
                    break
            c.check(
                "the run-time pluck machine can be chosen on track 1 (the type the boot assigned)",
                picked is not None,
                f"{picked} presses of DOWN",
            )
            snap(rig, "08-track-with-pluck")
            if picked is not None:
                c.report["machine_menu_downs"] = picked
            rig.s.run_ms(500)
            n_disp0 = dsp_words(rig, plan)
            # hold TRIG 1 for 12 s: dispatches rise, every one returns, the DSP never stops
            rig.note(c.work / "run.wav", max(M.TRIG_FRAME, rig.frames() + 300))
            snap(rig, "09-after-note")
            rig.emu.button(H.button(M.TRIG1), True)
            rig.s.run_ms(12000)
            n_disp1 = dsp_words(rig, plan)
            rig.emu.button(H.button(M.TRIG1), False)
            rig.s.run_ms(500)
            evs = log_to(rig, "boot2-after-note.log")
            c.report["dsp_counters"] = {"before": n_disp0, "after": n_disp1}
            if bisect:
                dd = n_disp1["dispatches"] - n_disp0["dispatches"]
                db = n_disp1["voice_beat"] - n_disp0["voice_beat"]
                c.check(
                    "bisect build: nearly every block passes stage 3 twice (the two voices of track 1; the dispatch counter counts the passes; at most 10 % of the blocks without), no routine is called (returns 0)",
                    dd > 1000
                    and 1.8 * db <= dd <= 2 * db + 4
                    and n_disp1["returns"] == 0,
                    f"{dd} passes in {db} blocks, returns {n_disp1['returns']}",
                )
                c.check(
                    "bisect build: the voice hook's heartbeat rises while the note is held; no DSP_STOPPED in the log",
                    db > 0 and not [e for e in evs if e[2] == L.K["DSP_STOPPED"]],
                    f"{n_disp0['voice_beat']} -> {n_disp1['voice_beat']}",
                )
                c.done(rig)
                return
            c.check(
                "the DSP's counters: dispatches to the registered type rise while the note sounds and every one returned (dispatches - returns is 0 or 1)",
                n_disp1["dispatches"] > n_disp0["dispatches"] + 1000
                and 0 <= n_disp1["dispatches"] - n_disp1["returns"] <= 1,
                f"{n_disp0} -> {n_disp1}",
            )
            c.check(
                "the voice hook's heartbeat and the link's rise together; no DSP_STOPPED in the log",
                n_disp1["voice_beat"] > n_disp0["voice_beat"]
                and not [e for e in evs if e[2] == L.K["DSP_STOPPED"]],
                f"{n_disp0['voice_beat']} -> {n_disp1['voice_beat']}",
            )
            diags = sorted(
                [e for e in evs if e[2] == L.K["DSP_DIAG"]], key=lambda e: e[0]
            )
            last_sets: list[int] = []
            for e in diags:
                if e[4][0] & 0xFF == 0:
                    last_sets.append(len(last_sets))
            words: list[int] = []
            for e in diags[-6:]:  # a set is six events (24 words, runtime 5)
                words += list(e[4][1:])
            c.report["last_logged_diag_words"] = [f"{w:08x}" for w in words[:24]]
            c.check(
                "the last logged DSP_DIAG set (every ten seconds) shows dispatches and returns of the registered type",
                len(words) >= 24 and words[15] > 0 and 0 <= words[15] - words[16] <= 1,
                f"dispatches {words[15] if len(words) > 15 else None}, returns {words[16] if len(words) > 16 else None}",
            )
            c.done(rig)

    probe_spec = None
    if probe:
        # the registered path (from the instruction after the scan's last branch to the stock path's label) and the breadcrumb routine:
        # the stock path runs for all 32 voices of every block, so the whole wrapper would swamp the core with interrupts.
        # `_dv_scan + 15` short words is the first instruction after the scan's last branch (`R0 = I4`, which keeps the row for the guard; `I12 = R2` follows) in every build of the wrapper.
        mask = PROBE_MASKS.get(probe) or int(probe, 16)
        reg_lo, reg_hi = plan.addr("_dv_scan") + 15, plan.addr("_dv_stock")
        probe_spec = f"{reg_lo:x}-{reg_hi:x},{plan.addr('_dv_crumb'):x}-{plan.addr('_dv_exp2'):x}:{mask:x}:{every}"
        c.report["irq_probe"] = probe_spec
    with _env("SHARC_IRQ_PROBE", probe_spec):
        c.parallel(*([main_run] if bisect or probe else [ref_boot, main_run]))
    if probe_spec:
        # the emulator prints its probe counters when it exits (the rig's `with` has ended): the run that held the note has the largest count
        import re

        stats = []
        for rd in run_dirs:
            for line in (rd / "daemon.stderr").read_text(errors="replace").splitlines():
                m = re.search(
                    r"irq_probe: .* (\d+) latched, (\d+) interrupts taken \((.*)\)",
                    line,
                )
                if m:
                    stats.append((int(m.group(1)), int(m.group(2)), m.group(3)))
        best = max(stats, default=(0, 0, ""))
        c.report["irq_probe_stats"] = {
            "latched": best[0],
            "taken": best[1],
            "by_bit": best[2],
        }
        c.check(
            "the probe latched interrupts inside the wrapper (so the pass above is a pass with interrupts taken between its instructions)",
            best[0] > 10000 and best[1] >= best[0] // 2,
            f"{best[0]} latched, {best[1]} interrupts taken ({best[2]})",
        )
    rep = Reporter()
    pcm = ref.get("pcm")
    run_wav = c.work / "run.wav"
    if pcm is not None and run_wav.exists():
        from tools.patchkit import hook_ab

        M.pcm_check(rep, pcm, hook_ab.read_wav(run_wav), c.report, "machine")
        c.checks += rep.checks
    c.report["screenshots"] = pngs
    (shots / "report.json").write_text(
        json.dumps(
            {"checks": [list(x) for x in c.checks], "report": c.report},
            indent=1,
            default=str,
        )
    )
    c.common_checks_upload(project_writes_ok=True)


def dsp_words(rig: Any, plan: Any) -> dict[str, int]:
    """The wrapper's own counters from DSP memory: the beats, the dispatches to registered types, the routines that returned, the last breadcrumb."""
    base = M.sym(rig.plan, "core/dsp-voice", "_dv_diag")
    raw = rig.dsp_bytes(base, 64)
    w = struct.unpack("<16I", raw)
    return {"voice_beat": w[0], "dispatches": w[13], "returns": w[14], "crumb": w[12]}
