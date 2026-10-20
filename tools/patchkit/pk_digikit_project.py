"""The project round trip across module sets (scenario kind `roundtrip` of pk_digikit_scen; finding 39).

A project that uses run-time machines is saved by the firmware to its MmcFs slot. The slot holds the machine type byte of every
sound, the parameter values, the p-locks (pattern data) and the sound locks. The module set is fixed per boot, so a project can be
loaded and saved again by a boot that does not have a machine the project uses. What must hold:

  * nothing the project holds changes when it passes through a boot without the machine: after A (machine present), B (absent) and A
    again, the slot's bytes equal those of the first save;
  * a track whose machine is absent keeps its type (the saved sound records still carry it);
  * two different load orders of one set give the same bytes.

The runner saves through the panel (FUNC+SETTINGS, the same keys as the machine proofs), exports the card writes
(`card.overlay`), lays them over the card (`card_overlay.apply`) and boots the next set on the result. The slot is read from the
card files: the sectors `[0x58000 + 0x8000 * slot, + 0x8000)` of every card that was written.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from tools.emulator import card_overlay
from tools.patchkit import pk_dkrt_boot as B
from tools.patchkit import pk_dkrt_machine as M

SLOT0 = 0x58000
SLOT_SECTORS = 0x8000
SECTOR = 512
SOUND_HEAD = card_overlay.SOUND_HEAD
# The one word of the slot that the first save of a project sets differently from every later save, with or without any module change
# (finding 39): the 32-bit word at slot offset 0x18, after the project name. It is left out of the comparison with the first save; every
# later save is compared with the second save byte for byte, so the word is held to the same value there.
STAMP = ((0x18, 4),)


class Fail(RuntimeError):
    pass


def slot_sectors(overlay: Path) -> dict[int, bytes]:
    """The project-slot sectors of an overlay file, with the bytes the guest wrote laid over zeros (mask applied)."""
    out: dict[int, bytes] = {}
    for sector, mask, data in card_overlay.records(overlay.read_bytes()):
        if SLOT0 <= sector < SLOT0 + 128 * SLOT_SECTORS:
            buf = bytearray(SECTOR)
            for i in range(SECTOR):
                if mask[i >> 3] & (1 << (i & 7)):
                    buf[i] = data[i]
            out[sector] = bytes(buf)
    return out


def read_sectors(card: Path, sectors: list[int]) -> dict[int, bytes]:
    fd = os.open(card, os.O_RDONLY)
    try:
        return {
            s: os.pread(fd, SECTOR, s * SECTOR).ljust(SECTOR, b"\0") for s in sectors
        }
    finally:
        os.close(fd)


def slot_image(card: Path, touched: set[int]) -> dict[int, bytes]:
    return read_sectors(card, sorted(touched))


def sound_records(blob: dict[int, bytes], type_at: int) -> list[tuple[int, int]]:
    """(sector offset of the record, machine type) for each sound record found in the written slot sectors (runs of adjacent
    sectors are joined, a record may cross a sector boundary)."""
    out = []
    run: list[int] = []
    for s in [*sorted(blob), None]:
        if run and (s is None or s != run[-1] + 1):
            data = b"".join(blob[k] for k in run)
            at = data.find(SOUND_HEAD)
            while at >= 0:
                if at + type_at < len(data):
                    out.append((run[0] * SECTOR + at, data[at + type_at]))
                at = data.find(SOUND_HEAD, at + 1)
            run = []
        if s is not None:
            run.append(s)
    return out


def region_of(offset: int, device: str) -> str:
    """Name the part of the project slot a byte offset (from the slot start) falls in (finding 02, storage records)."""
    if device == "dt2":
        pat, kit, pool = 0x200, 0x200 + 128 * 0x15C00, 0xDA5A04
    else:
        pat, kit, pool = 0x200, 0x200 + 128 * 0x15C00, 0xC4B004 - 130 * 0x167
    if offset < pat:
        return "header"
    if offset < kit:
        return "pattern"
    if offset < pool:
        return "kit"
    return "pool"


def diff_slot(
    a: dict[int, bytes],
    b: dict[int, bytes],
    device: str,
    ignore: tuple[tuple[int, int], ...] = (),
) -> dict[str, Any]:
    """Compare two slot images sector by sector: the sectors only one has and the bytes that differ, by region. IGNORE: byte ranges
    (offset, length) of the first slot's start that are left out (the header stamp, see `STAMP`)."""
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    regions: dict[str, int] = {}
    first = None
    skip = {SLOT0 * SECTOR + off + i for off, n in ignore for i in range(n)}
    for s in sorted(set(a) & set(b)):
        if a[s] != b[s]:
            n = 0
            for i, (x, y) in enumerate(zip(a[s], b[s], strict=True)):
                if x != y and s * SECTOR + i not in skip:
                    n += 1
                    if first is None:
                        first = (
                            (s - SLOT0) // SLOT_SECTORS,
                            (s - SLOT0) % SLOT_SECTORS * SECTOR + i,
                        )
            if n:
                off = (s - SLOT0) % SLOT_SECTORS * SECTOR
                r = region_of(off, device)
                regions[r] = regions.get(r, 0) + n
    return {
        "only_first": len(only_a),
        "only_second": len(only_b),
        "differ_bytes": regions,
        "first_difference": first,
        "equal": not only_a and not only_b and not regions,
    }


def pattern0_changes(
    a: dict[int, bytes], b: dict[int, bytes], n: int = 0x15C00
) -> list[tuple[int, int, int]]:
    """(offset in pattern 0, byte of A, byte of B) for the bytes that differ in the first pattern record of slot 0
    (patternStorage at slot + 0x200: 16 tracks of trig, p-lock and sound-lock data, finding 02)."""
    out = []
    for k in range(n // SECTOR + 1):
        sec = SLOT0 + (0x200 + k * SECTOR) // SECTOR
        x, y = a.get(sec, bytes(SECTOR)), b.get(sec, bytes(SECTOR))
        for i in range(SECTOR):
            if x[i] != y[i]:
                out.append((sec * SECTOR - SLOT0 * SECTOR + i - 0x200, x[i], y[i]))
    return [o for o in out if o[0] < n]


def card_after(c: Any, base: Path, overlay: Path) -> Path:
    """BASE with the guest's writes laid over it, as a cached card file named by content."""
    h = hashlib.sha256(base.name.encode() + overlay.read_bytes()).hexdigest()[:20]
    from tools.patchkit import pk_digikit_scen as S

    dst = S.CACHE / "cards" / f"{c.image}-after-{h}.img"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = c.work / f"after-{h}.img"
        card_overlay.apply(base, overlay, tmp)
        shutil.move(tmp, dst)
    return dst


def track_type(rig: B.BootRig, track: int) -> int:
    """The machine type byte of a track's sound (Digitakt II: the kit tracks lie 0x450 apart, finding 02)."""
    return (rig.w(M.TRACK_OBJ_WORD + 0x450 * (track - 1)) >> 8) & 0xFF


def select_dt2(c: Any, rig: B.BootRig, track: int, want: int, hint: int) -> None:
    seen: list[tuple[int, int]] = []
    # The first SRC list after a project save may not take (the keys go to the save's leftovers; how many presses it eats depends on the engine), so the
    # hint is tried up to four times (the coupled engine ate two tries, the ColdFire-only one). Every try starts from the top of the list (UP presses), so a try does not depend on the machine the track holds.
    for downs in (hint, hint, hint, hint, hint + 1, hint - 1):
        rig.select(downs, ups=12)
        seen.append((downs, track_type(rig, track)))
        if seen[-1][1] == want:
            c.check(f"track {track} holds type {want}", True, f"{downs} downs")
            return
    raise Fail(
        f"no list position gives type {want} on track {track}: (downs, type) {seen}"
    )


def build_project(c: Any, rig: B.BootRig, t: dict[str, Any]) -> None:
    """The panel script that makes the project: the run-time machine on track 1 (and the second one on track 2), parameter
    values, a p-lock and a sound lock on step 1 of track 1."""
    s = rig.s
    machines = t["machines"]  # [{track, type, hint}]
    for m in machines:
        if m["track"] != 1:
            s.do_tap(["TRK", f"TRIG {m['track']}"])
            s.run_ms(300)
        if c.dt2:
            select_dt2(c, rig, m["track"], m["type"], m["hint"])
        else:
            rig.select(m["hint"])
        s.do_tap("SYN" if not c.dt2 else "SRC")
        s.run_ms(300)
        for enc, by in (("ENC A", 9), ("ENC B", -7)):  # parameter values
            s.do_turn(enc, by)
            s.run_ms(100)
    if any(m["track"] != 1 for m in machines):
        s.do_tap(["TRK", "TRIG 1"])
        s.run_ms(300)
    # a note trig, a p-lock and a preset (sound) lock on it: GRID RECORDING, tap the trig, hold it and turn knobs
    s.do_tap("RECORD")
    s.run_ms(200)
    s.do_tap("TRIG 1")
    s.run_ms(200)
    s.emu.button(M.H.button("TRIG 1"), True)
    s.run_ms(100)
    s.do_turn("ENC C", t.get("lock_turn", 11))
    s.run_ms(100)
    s.do_turn("LEVEL", t.get("sound_lock_turn", 2))
    s.run_ms(300)
    s.emu.button(M.H.button("TRIG 1"), False)
    s.run_ms(300)
    s.do_tap("RECORD")
    s.run_ms(200)


def save_project(rig: B.BootRig) -> None:
    s = rig.s
    s.do_tap(["FUNC", "SETTINGS"])
    s.run_ms(300)
    s.do_tap("YES")  # slot 001
    s.run_ms(500)
    if rig.device == "dn2":
        s.do_tap("YES")  # the name, save
    s.run_ms(3500)


def load_project(rig: B.BootRig) -> None:
    s = rig.s
    s.do_tap("SETTINGS")
    s.run_ms(300)
    for _ in range(3):
        s.do_tap("YES")
        s.run_ms(500)
    s.do_tap("YES")
    s.run_ms(6000)
    s.health("project loaded")


def export(rig: B.BootRig, name: str) -> Path:
    out = rig.work / f"{rig.tag}-{name}.overlay.bin"
    rig.emu.call("card.overlay", path=str(out))
    return out


def boot_check(
    c: Any, img: Any, card: Path, tag: str, label: str, sel: list[str]
) -> threading.Thread:
    """The coupled boot of CARD in a thread: every module loaded, its DSP part included (the panel script runs elsewhere meanwhile)."""

    def run() -> None:
        try:
            with c.rig(img, card, tag, settle_ms=B.SETTLE_MS) as rig:
                info = rig.boot_info()
                c.check(
                    f"{label}: every module loaded",
                    info["loaded"] == len(sel)
                    and info["safe"] == 0
                    and all(r == 0 for r in info["results"]),
                    str(info["results"]),
                )
                c.done(rig)
        except Exception as e:  # noqa: BLE001  a failed boot check fails the scenario with its reason
            c.check(f"{label}: coupled boot check: {type(e).__name__}: {e}", False)

    th = threading.Thread(target=run, name=tag)
    th.start()
    return th


def dyn_numbers(rig: B.BootRig, t: dict[str, Any], sets: dict[str, list[str]], name: str,
                known: dict[str, int], c: Any) -> None:
    """The run-time machines the scenario names (`types` and `machines[].machine`, as "<module id>:<machine id>") get their number from the boot's
    assignment table. A machine keeps its number in every later boot that loads its module (the card remembers it: D7)."""
    specs = list(t.get("types", [])) + [m["machine"] for m in t["machines"] if "machine" in m]
    for spec in dict.fromkeys(x for x in specs if isinstance(x, str)):
        if spec.split(":")[0] not in sets[name]:
            continue
        got = rig.dyn_type(*spec.split(":"))
        if spec in known:
            c.check(f"set {name}: {spec} keeps type {known[spec]}", got == known[spec], f"type {got}")
        known[spec] = got
    c.report[f"assigned_{name}_{len(known)}"] = rig.assign_table()


def resolve_scenario(t: dict[str, Any], known: dict[str, int]) -> dict[str, Any]:
    """A copy of the scenario table with the machine specs replaced by the numbers of the first boot."""
    out = dict(t)
    out["types"] = [known[x] if isinstance(x, str) else x for x in t.get("types", [])]
    out["machines"] = [
        {**m, "type": known[m["machine"]]} if "machine" in m else m
        for m in t["machines"]
    ]
    return out


def run_roundtrip(c: Any) -> None:
    t = c.table()
    known: dict[str, int] = {}
    sets: dict[str, list[str]] = t["sets"]
    files: dict[str, str] = {}
    for mods in sets.values():
        for m in mods:
            if m not in files:
                files[m] = c.pack(m, m.split("/")[-1] + ".dkm")
    img = c.image_for(B.RT, "run")
    type_at = 0xD2 if c.dt2 else 0xF4
    order = t["order"]
    base_card: Path | None = None
    saves: list[
        tuple[str, Path, dict[int, bytes]]
    ] = []  # (set name, card after, slot sectors)
    touched: set[int] = set()
    edited = False
    engine = t.get(
        "engine", "cf"
    )  # cf: the panel script runs on ColdFire-only daemons, the boot check on coupled ones, in parallel
    checks: list[threading.Thread] = []
    for step, name in enumerate(order):
        sel = [files[m] for m in sets[name]]
        card = c.card(sel, base=base_card) if base_card else c.card(sel)
        if base_card:
            c.used_cards.add(card.name)
        if engine == "cf":
            checks.append(
                boot_check(
                    c, img, card, f"rtb{step}", f"boot {step + 1} (set {name})", sel
                )
            )
        with c.rig(
            img, card, f"rt{step}", cf_only=engine == "cf", settle_ms=B.SETTLE_MS
        ) as rig:
            info = rig.boot_info()
            if (
                engine == "cf"
            ):  # the ColdFire half of the boot check; a module's DSP part is still pending here
                c.check(
                    f"boot {step + 1} (set {name}): every module loaded (ColdFire part)",
                    info["count"] == len(sel)
                    and info["safe"] == 0
                    and all(r in (0, B.E_LOADING) for r in info["results"]),
                    str(info["results"]),
                )
            else:
                c.check(
                    f"boot {step + 1} (set {name}): every module loaded",
                    info["loaded"] == len(sel)
                    and info["safe"] == 0
                    and all(r == 0 for r in info["results"]),
                    str(info["results"]),
                )
            dyn_numbers(rig, t, sets, name, known, c)
            if step == 0:
                t = resolve_scenario(t, known)
                save_project(rig)
                base_ov = export(rig, "base")
                build_project(c, rig, t)
                edited = True
            else:
                load_project(rig)
            save_project(rig)
            ov = export(rig, "save")
            c.done(rig)
        after = card_after(c, card, ov)
        sec = set(slot_sectors(ov))
        touched |= sec
        if step == 0:
            base_sec = slot_sectors(base_ov)
            touched |= set(base_sec)
            base_card_after = card_after(c, card, base_ov)
        saves.append((name, after, slot_sectors(ov)))
        base_card = after
    for th in checks:
        th.join()
    assert edited
    # compare: the slot of every save against the first
    imgs = [slot_image(card, touched) for _, card, _ in saves]
    base_img = slot_image(base_card_after, touched)
    d0 = diff_slot(base_img, imgs[0], c.device)
    c.report["edit_vs_baseline"] = d0
    # (the firmware saves only the patterns and kits that changed: on the Digitakt II an untouched project's save does not write pattern 1 at all,
    # so its bytes compare against the card's zeros)
    pc = pattern0_changes(base_img, imgs[0])
    c.report["pattern0_changes"] = pc[:80]
    c.report["pattern0_changed_bytes"] = len(pc)
    c.check(
        "control: the panel script changed the project (the first save differs from a save of the untouched project)",
        not d0["equal"] and d0["differ_bytes"].get("pattern", 0) > 0,
        str(d0),
    )
    trig = [o for o in pc if o[0] < 0x20]
    locks = [o for o in pc if o[0] >= 0x20]
    c.check(
        "control: pattern 1 gained a trig (its first bytes) and lock data (the bytes after the trig table)",
        bool(trig) and bool(locks),
        f"{len(trig)} trig bytes, {len(locks)} other bytes: {pc[:8]}",
    )
    c.check(
        "control: the sounds changed too (parameter values, the machine type)",
        d0["differ_bytes"].get("kit", 0) + d0["differ_bytes"].get("pool", 0) > 0,
        str(d0["differ_bytes"]),
    )
    first_types = sound_records({s: b for s, b in imgs[0].items()}, type_at)
    for want in t.get("types", []):
        c.check(
            f"the first save holds a sound with the run-time machine's type {want}",
            any(ty == want for _, ty in first_types),
            f"{sorted({ty for _, ty in first_types})}",
        )
    for k in range(1, len(saves)):
        d = diff_slot(imgs[0], imgs[k], c.device, STAMP)
        c.report[f"save{k + 1}_vs_save1"] = d
        c.check(
            f"project slot after set {saves[k][0]} (boot {k + 1}) equals the first save (but for the header stamp)",
            d["equal"],
            str(d),
        )
        if k >= 2:
            d2 = diff_slot(imgs[1], imgs[k], c.device)
            c.report[f"save{k + 1}_vs_save2"] = d2
            c.check(
                f"project slot after set {saves[k][0]} (boot {k + 1}) is byte-identical to the second save (stamp included)",
                d2["equal"],
                str(d2),
            )
        types = sound_records(imgs[k], type_at)
        for want in t.get("types", []):
            c.check(
                f"boot {k + 1} (set {saves[k][0]}): sound records still carry type {want}",
                any(ty == want for _, ty in types),
                f"{sorted({ty for _, ty in types})}",
            )
    stamp = [int.from_bytes(img_[SLOT0][0x18:0x1C], "big") for img_ in imgs]
    c.report["header_stamps"] = [hex(x) for x in stamp]
    c.check(
        "the header stamp changes once (first save to second) and then stays",
        len(stamp) < 3 or (stamp[0] != stamp[1] and len(set(stamp[1:])) == 1),
        str([hex(x) for x in stamp]),
    )
    c.common_checks()
