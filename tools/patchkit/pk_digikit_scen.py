#!/usr/bin/env python3
"""Boot-time scenarios of the digikit runtime: data files and one runner (docs/findings/39-digikit-boot-tests.md).

    uv run python -m tools.patchkit.pk_digikit_scen list
    uv run python -m tools.patchkit.pk_digikit_scen run [NAME ...] [--image dt2-1.17] [--tier quick|full] [--keep DIR]

A scenario is a TOML file in `tests/digikit/scenarios/`:

    [scenario]
    name = "equiv-dyn-pluck"       # the file's stem
    kind = "equiv" | "roundtrip" | "safe" | "cut"
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
  safe       a module whose init resets the machine, twice: the third boot is safe mode (no module, reported).
  cut        the card states a power cut leaves between the two copies of the selection record.

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
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from tools.patchkit import pk_dkrt_boot as B  # noqa: E402
from tools.patchkit import pk_dkrt_machine as M  # noqa: E402

SCENARIOS = ROOT / "tests" / "digikit" / "scenarios"
CACHE = ROOT / "out" / "cache" / "digikit"
ALL_IMAGES = {
    "dt2": ("dt2-1.16", "dt2-1.17"),
    "dn2": ("dn2-1.11", "dn2-1.12"),
}
QUICK_IMAGES = {"dt2": "dt2-1.17", "dn2": "dn2-1.12"}
RT = B.RT
FIRMWARE_FIRST = 0x50000  # the working project; the project slots follow at 0x58000 (finding 02, storage records)


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
        self.t0 = time.monotonic()

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
    ) -> Path:
        """A card image, cached by content: the store of the folder and the record copies (or SELECTION), optionally
        laid over a `base` card file (the card a scenario left behind). Returns the cached file."""
        h = hashlib.sha256(self.image.encode())
        for f in sorted((self.env.folder / "digikit" / "modules").glob("*.dkm")):
            h.update(f.name.encode() + f.read_bytes())
        a, b = recs if recs else (None, None)
        if recs is None and selection is not None:
            a = b = B.make_record(selection, seq, counter, flags)
        for x in (a, b):
            h.update(x or b"-")
        if base is not None:  # a card made by `card_after` is named by its content
            h.update(base.name.encode())
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
            shutil.move(tmp, dst)
        return dst

    def rig(
        self,
        img: B.Img,
        card: Path,
        tag: str,
        extra_env: dict[str, str] | None = None,
    ) -> B.BootRig:
        ck = hashlib.sha256(card.name.encode() + img.syx.read_bytes()).hexdigest()[:10]
        if "-after-" in card.name or card.name in self.used_cards:
            # a card the firmware saved a project on never runs the DT2 filesystem check the readiness contract waits for
            extra_env = {"DIGI_COLD_RELAX_FS": "1", **(extra_env or {})}
        return self.env.boot(
            img, card, f"scn-{ck}", watch=True, extra_env=extra_env
        )  # one run directory per (firmware, card): its boot cache is reused

    def done(self, rig: B.BootRig) -> None:
        """Collect the card writes and the watch summaries of a rig that is finished."""
        try:
            self.store_writes |= set(rig.card_store_writes())
            from tools.coldfire import arena_safety

            ov = rig.work / f"{rig.tag}.all-overlay.bin"
            self.all_writes |= set(arena_safety.parse_overlay(ov.read_bytes()))
        except Exception as e:  # noqa: BLE001
            print(f"card.overlay: {e}", file=sys.stderr)
        w = rig.watch_summaries()
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


# ---- the kinds ------------------------------------------------------------------------------------------------------------------


def run_equiv(c: Ctx) -> None:
    t = c.table()
    mods = [
        c.pack(m, f"m{i}.dkm") for i, m in enumerate(t["modules"])
    ]  # the run-time set, in selection order
    twin = t.get("twin", "")
    run_img = c.image_for(RT, "run")
    empty = c.card(None)
    with c.rig(run_img, empty, "none") as rig:
        base = decode_tables(rig)
        c.report["boot_none"] = rig.boot_info()
        c.done(rig)
    ref: dict[str, Any] = base
    ref_pcm = None
    play = t.get("play", "")
    trig = t.get("trig_frame", M.TRIG_FRAME if c.dt2 else M.TRIG_FRAME_DN2)
    env_ref, env_run = {}, {}
    if (
        play == "sine"
    ):  # finding 32: the frame copy's type is forced to the static or the run-time number
        env_ref = {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_STATIC_TYPE)}
        env_run = {"DSP_FRAME_POKE": M.DN2_TYPE_POKE.format(M.SINE_DYN_TYPE)}
    if twin:
        ref_img = c.image_for(RT + "," + twin, "ref")
        with c.rig(ref_img, empty, "ref", env_ref) as rig:
            ref = decode_tables(rig)
            if play:
                if c.dt2:
                    rig.select_type(t["static_type"], t.get("hint", 7))
                ref_pcm = rig.note(c.work / "ref.wav", trig)
                c.check(
                    "the reference note is audible",
                    float(abs(ref_pcm).max()) > 0.01,
                    f"peak {float(abs(ref_pcm).max()):.3f}",
                )
            c.done(rig)
    card = c.card(mods)
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
        got = decode_tables(rig)
        used = [r for r in rig.voice_rows() if r[0] != B.FREE_TYPE]
        want_voices = t.get("voices", 0)
        c.check(
            "voice rows claimed",
            len(used) == want_voices,
            f"{[(hex(a), hex(b)) for a, b in used]}, want {want_voices}",
        )
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
            if c.dt2:
                rig.select_type(M.PLUCK_DYN_TYPE, t.get("hint_run", t.get("hint", 7)))
            pcm = rig.note(c.work / "run.wav", trig)
            M.pcm_check(rig, ref_pcm, pcm, c.report, "machine" if c.dt2 else "voice")
            c.checks += rig.checks
        c.done(rig)
    c.common_checks()


def run_safe(c: Ctx) -> None:
    t = c.table()
    mod = c.pack(t["fault"], "fault.dkm")
    other = [c.pack(m, f"m{i}.dkm") for i, m in enumerate(t.get("modules", []))]
    img = c.image_for(RT, "run")
    with c.rig(img, c.card([mod]), "safe") as rig:
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
    if other:
        with c.rig(img, c.card(other, flags=1), "safe-flag") as rig:
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
    if c.tier == "quick":  # the first cases only: every boot of a case costs about 40 s
        cases = cases[: t.get("quick_cases", len(cases))]
    for case in cases:
        a, b = named[case["a"]], named[case["b"]]
        card = c.card(None, recs=(a, b))
        with c.rig(img, card, "cut") as rig:
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
    if t.get("runtime_cut", True):
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
    c.common_checks()


def run_roundtrip(c: Ctx) -> None:
    from tools.patchkit import pk_digikit_project as P

    P.run_roundtrip(c)


KINDS = {
    "equiv": run_equiv,
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
    try:
        KINDS[scen.kind](c)
    except Exception as e:  # noqa: BLE001  a scenario that cannot run is a failed scenario, with its reason
        import traceback

        traceback.print_exc()
        c.check(f"scenario error: {type(e).__name__}: {e}", False)
    rep = c.result()
    (work / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep


@dataclass
class Summary:
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r["pass"] for r in self.results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["list", "run"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--image", action="append", default=[])
    ap.add_argument("--tier", choices=["quick", "full"], default="quick")
    ap.add_argument("--keep", type=Path)
    a = ap.parse_args(argv)
    scens = selected(a.tier, a.names)
    if a.cmd == "list":
        for s in scens:
            print(f"{s.name:28s} {s.kind:10s} {s.tier:5s} {','.join(s.images(a.tier))}")
        return 0
    summary = Summary()
    for s in scens:
        for image in a.image or s.images(a.tier):
            if image[:3] not in s.devices and not s.data["scenario"].get("images"):
                continue
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
