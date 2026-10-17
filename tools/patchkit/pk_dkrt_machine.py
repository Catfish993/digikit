#!/usr/bin/env python3
"""A machine with its own DSP code, loaded at run time: PCM equal to the patched-in machine (docs/findings/32-runtime-dsp-machines.md).

    uv run python -m tools.patchkit.pk_dkrt_machine proof [--image dt2-1.17] [--quick] [--keep DIR]

`proof` builds two images of the same firmware with the same ColdFire-side modules (`core/runtime`, `core/dynreg`, `core/dyndsp`
and what they require), so both run the same hooks at every block interrupt: the REFERENCE has `machines/pluck` patched in; the
RUN-TIME image does not, and loads `test/dyn-pluck` (the same DSP source, packed with `patchkit pack --device dt2`) from the card
store through the runtime's mailbox while the firmware runs. Both are driven through the panel the same way (FUNC+SRC, pick
PLUCK, YES), the same note is played from the same DSP block (`run_until frames`), and the PCM is compared sample by sample.

Checks, in order: the load answers `LOADING` and finishes with 0; the machine is not in the type table before the DSP code is
acknowledged (sampled while the load runs); the DSP area holds the relocated image byte for byte (a second implementation of the
relocation, here, against the runtime's); the voice row holds the machine's type and the routine's pc; the track takes the machine
(type byte 18 against 9); the PCM equals the reference's; an unload frees the row (type back to 0xffffffff) and the range; a reload
reuses them; write-watch on `cf.expansion` (the patched range is the only writer; the staging buffer is excluded: the card read
lands there by DMA) and the flush check report nothing; no card store sector is written. Prints one JSON report; exit 0 when every
check holds.

`--quick` skips the reload after the unload.

On the Digitone II (`--image dn2-1.12`) the ColdFire registry for run-time machines is lane B's, so the proof covers the DSP half
only: the reference is `test/dn2-sine` patched in, the run-time image loads `test/dyn-sine-dn2` (the same DSP source, no machine
record), and the DSP's frame copy is forced to the machine type (`DSP_FRAME_POKE`) in both, 7 and 18. Same checks from the load to the PCM.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]

from tools.coldfire import arena_safety  # noqa: E402
from tools.control.digi_client import Emu  # noqa: E402
from tools.emulator import card_check  # noqa: E402
from tools.firmware import config  # noqa: E402
from tools.patchkit import hook_ab  # noqa: E402
from tools.patchkit import pk_harness as H  # noqa: E402
from tools.patchkit.pk_dkrt import (  # noqa: E402
    DEVICE_SYX,
    ProofError,
    Tools,
    build_card,
    patch,
)

LOADING = 1
STATE_IDLE, STATE_LOADING = 0, 1
FREE_TYPE = 0xFFFF_FFFF
L2_BYTE_BASE, L2_SW_BASE = 0x2000_0000, 0x00B8_0000
TRACK_OBJ_WORD = 0x4265338C  # the type byte is the third byte of this word (finding 02)
MODULES_STATIC = "core/runtime,core/dynreg,core/dyndsp"
PLUCK_STATIC_TYPE, PLUCK_DYN_TYPE = 9, 18
MODULE = "test/dyn-pluck"
MODULE_DN2 = "test/dyn-sine-dn2"
MODULES_STATIC_DN2 = "core/runtime,core/dyndsp,core/machines"  # core/machines in both: the same ColdFire hooks
SINE_STATIC_TYPE, SINE_DYN_TYPE = 7, 18
DN2_TYPE_POKE = "0x94={:#x}"  # byte offset of the DSP frame's halfword 74 (the machine type of slot 0)
# DspState (modules/core/runtime/src/rs/src/dsp.rs), byte offsets of the fields the proof reads
D_MAGIC, D_PHASE, D_STEP, D_TRANSFERS, D_ROW_WRITES, D_FAILURES = 0, 4, 12, 36, 40, 44
D_MOD_OFF, D_MOD_LEN, D_ROW_TYPE, D_ROW_PC, D_ROW_OWNER = 52, 84, 148, 180, 212
# State: last_result at 12; Mailbox: cmd 0, seq 4, result 8, arg 12
ST_LAST = 12
MB_CMD, MB_SEQ, MB_RESULT, MB_ARG = 0, 4, 8, 12
# frame at which the note starts, the same in both runs (dsp_frames counts from the boot)
TRIG_FRAME = 7500
TRIG_FRAME_DN2 = 6000
PRESS_OFFSET = (
    44_000  # ColdFire instructions after the block boundary (a block is 88 000)
)
TRIG1 = "TRIG 1"
NOTE_FRAMES = 1500  # 1 s of audio, captured from the trig on


class Fail(RuntimeError):
    pass


def be32(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def sym(plan: dict, owner: str, name: str) -> int:
    for k, v in plan["symbols"].items():
        if k.startswith(owner + "@") and k.split(":", 1)[1] == name:
            return int(v)
    raise ProofError(f"no symbol {owner}:{name}")


# ---- the second implementation of the DSP relocation -----------------------------------------------------------------------


def parse_dsp_section(dkm: bytes) -> dict[str, Any]:
    off, size = be32(dkm, 0x98), be32(dkm, 0x9C)
    s = dkm[off : off + size]
    if be32(s, 0) != 0x444B4453:
        raise Fail("no DSP section")
    image_off, reloc_off, nreloc = be32(s, 0x18), be32(s, 0x1C), be32(s, 0x20)
    import_off, nimport = be32(s, 0x24), be32(s, 0x28)
    voice_off, nvoice = be32(s, 0x2C), be32(s, 0x30)
    imports = []
    for i in range(nimport):
        e = s[import_off + 32 * i : import_off + 32 * (i + 1)]
        imports.append(e.split(b"\0")[0].decode())
    relocs = []
    for i in range(nreloc):
        o, w = struct.unpack_from(">II", s, reloc_off + 8 * i)
        relocs.append((o, w >> 28, (w >> 24) & 15, w & 0xFFFF))
    voices = [struct.unpack_from(">II", s, voice_off + 16 * i) for i in range(nvoice)]
    return {
        "device": be32(s, 8),
        "image": bytes(s[image_off : image_off + be32(s, 0x0C)]),
        "bss": be32(s, 0x10),
        "align": be32(s, 0x14),
        "imports": imports,
        "relocs": relocs,
        "voices": voices,
    }


def relocate(sec: dict[str, Any], base: int, imports: dict[str, int]) -> bytes:
    m = bytearray(sec["image"])
    pc_shift, byte_shift = (base - L2_BYTE_BASE) // 2, base - L2_BYTE_BASE
    for o, kind, cls, idx in sec["relocs"]:
        add = (
            pc_shift
            if cls == 0
            else byte_shift
            if cls == 1
            else imports[sec["imports"][idx]]
        )
        le16 = lambda a: m[a] | m[a + 1] << 8  # noqa: E731
        if kind == 0:
            v = ((m[o + 2] << 16) | le16(o + 4)) + add
            assert v < 1 << 24
            m[o + 2] = v >> 16 & 0xFF
            m[o + 4 : o + 6] = (v & 0xFFFF).to_bytes(2, "little")
        else:
            v = ((le16(o + 2) << 16) | le16(o + 4)) + add & 0xFFFFFFFF
            m[o + 2 : o + 4] = (v >> 16).to_bytes(2, "little")
            m[o + 4 : o + 6] = (v & 0xFFFF).to_bytes(2, "little")
    return bytes(m)


# ---- sessions --------------------------------------------------------------------------------------------------------------


class FolderSession(H.Session):
    """Session whose card is built from a +Drive folder (the module store is `digikit/modules` in it)."""

    folder: Path
    card_img: Path | None = None

    def open(self, image: Path | None = None) -> None:
        self.syx, self.ready = H.firmware_inputs(self.cell)
        exe = H.daemon_binary(self.cell.device, self.syx)
        run_dir = H.OUT / f"run-{self.run_name}"
        self.emu = Emu.spawn_coupled(
            self.syx, self.ready, timeout=300, binary=exe, run_dir=run_dir, env=self.env
        )
        self.emu.subscribe()
        # the Digitone II daemon takes a card image built from the folder (the Digitakt II builds it from the folder itself)
        self.card = (
            str(self.card_img)
            if self.card_img
            else self.emu.call("card.load", dir=str(self.folder))["card"]
        )
        self.load(image)


class Rig:
    """One image, one daemon: the helpers both kinds of run share."""

    def __init__(self, tag: str, image: str, modules: str, plan: dict, syx: Path, folder: Path, work: Path, watch: bool,
                 card_img: Path | None = None, extra_env: dict[str, str] | None = None):  # fmt: skip
        self.tag, self.image, self.plan, self.work = tag, image, plan, work
        self.device, self.fw = image[:3], image[4:]
        cell = H.Cell(self.device, self.fw, modules.split(","))
        env: dict[str, str] = dict(extra_env or {})
        self.summary: Path | None = None
        if watch:
            lo, hi = arena_safety.expansion(image)
            # the card reads land by DMA in the runtime's staging buffer (and the 512-byte record buffer): left out of the watch
            holes = [(sym(plan, "core/runtime", "DKRT_STAGE"), 64 * 1024)]
            with contextlib.suppress(
                ProofError
            ):  # an image of an older runtime has no record buffer
                holes.append((sym(plan, "core/runtime", "DKRT_SECBUF"), 512))
            ranges, at = [], lo
            for start, size in sorted(holes):
                if start > at:  # adjacent holes leave no range between them
                    ranges.append("0x%x-0x%x" % (at, start))
                at = start + size
            ranges.append("0x%x-0x%x" % (at, hi))
            out = work / f"{tag}.writewatch.jsonl"
            self.summary = out.with_suffix(".summary.json")
            env |= {
                "DIGI_WRITE_WATCH": ",".join(ranges),
                # from the first cache enable on: the reset path clears and copies the whole RAM before it
                "DIGI_WRITE_WATCH_AFTER": "cacr",
                "DIGI_WRITE_WATCH_ALLOW_PC": arena_safety.plan_allow_pc(
                    syx.with_suffix(".plan.json")
                ),
                "DIGI_FLUSH_CHECK": "1",
                "DIGI_WRITE_WATCH_OUT": str(out),
            }
        self.s = FolderSession(
            cell,
            H.Goldens(work / f"{tag}.goldens.json"),
            run_name=f"dkm-{tag}-{image}",
            env=env,
        )
        self.s.folder = folder
        self.s.card_img = card_img
        self.syx = syx
        self.checks: list[tuple[str, bool, str]] = []
        self.mb = sym(plan, "core/runtime", "DKRT_MAILBOX")
        self.state = sym(plan, "core/runtime", "DKRT_STATE")
        self.dsp = sym(plan, "core/runtime", "DKRT_DSP")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((f"{self.tag}: {name}", bool(ok), detail))
        print(f"{'ok  ' if ok else 'FAIL'} {self.tag}: {name} {detail}", flush=True)

    @property
    def emu(self) -> Any:
        assert self.s.emu is not None
        return self.s.emu

    def w(self, addr: int) -> int:
        return self.emu.peek32(addr) or 0

    def frames(self) -> int:
        return int(self.emu.call("status")["dsp_frames"])

    def start(self) -> None:
        self.s.open(self.syx)
        self.s.segment = "dkm"
        self.s.reset()
        self.s.run_ms(1500)

    # -- the runtime's mailbox
    def post(self, cmd: int, arg: str) -> int:
        b = arg.encode() + b"\0"
        b += bytes(-len(b) % 4)
        for i in range(0, len(b), 4):
            self.emu.poke32(self.mb + MB_ARG + i, struct.unpack(">I", b[i : i + 4])[0])
        seq0 = self.w(self.mb + MB_SEQ)
        self.emu.poke32(self.mb + MB_CMD, cmd)
        for _ in range(200):
            self.s.run_ms(20)
            if self.w(self.mb + MB_CMD) == 0 and self.w(self.mb + MB_SEQ) != seq0:
                break
        r = self.w(self.mb + MB_RESULT)
        return r - (1 << 32) if r >= 1 << 31 else r

    def last_result(self) -> int:
        r = self.w(self.state + ST_LAST)
        return r - (1 << 32) if r >= 1 << 31 else r

    def dsp_phase(self) -> int:
        return self.w(self.dsp + D_PHASE)

    def dsp_bytes(self, addr: int, n: int) -> bytes:
        out = bytearray()
        for off in range(0, n, 4096):
            h = self.emu.dsp_peek(addr + off, min(4096, n - off))["hex"]
            out += bytes.fromhex(h.replace("??", "00"))
        return bytes(out)

    def info_word(self, k: int) -> int:
        return self.w(sym(self.plan, "core/dyndsp", "dyndsp_info") + 4 * k)

    def voice_row(self, k: int) -> tuple[int, int]:
        rows = sym(self.plan, "core/dsp-voice", "voices.rows")
        first = self.info_word(6)
        raw = self.dsp_bytes(rows + 8 * (first + k), 8)
        return struct.unpack(
            "<II", raw
        )  # dsp_peek returns memory bytes: DSP words are little endian

    def load_and_wait(
        self, path: str, sample_type: int | None = None
    ) -> dict[str, Any]:
        """Post the load; sample the machine table while the DSP code is on its way; wait for the end."""
        f0 = self.frames()
        r0 = self.post(1, path)
        listed_early: bool | None = None
        waited = 0
        while self.dsp_phase() != 0 and waited < 400:
            if sample_type is not None:
                listed_early = bool(listed_early) or self.machine_listed(sample_type)
            self.s.run_ms(2)
            waited += 1
        # the queue may hold a start; wait for idle and a final result
        for _ in range(100):
            if self.dsp_phase() == 0 and self.last_result() != LOADING:
                break
            self.s.run_ms(10)
        return {
            "answer": r0,
            "result": self.last_result(),
            "frames": self.frames() - f0,
            "listed_while_loading": listed_early,
            "ticks_waited": waited,
        }

    # -- the firmware's machine rows (core/machines): the name pointer marks a row as used
    def machine_listed(self, typ: int) -> bool:
        rows = sym(self.plan, "core/machines", "machines.rows")
        first = sym(self.plan, "core/machines", "machines.first")
        size = sym(self.plan, "core/machines", "machines.row_size")
        name = self.w(rows + (typ - first) * size + 56)
        return bool(name) and (self.w(name) >> 24) != 0

    def type_now(self) -> int:
        return (self.w(TRACK_OBJ_WORD) >> 8) & 0xFF

    def select(self, downs: int) -> None:
        s = self.s
        s.do_tap(["FUNC", "SRC"])
        s.run_ms(200)
        for _ in range(downs):
            s.do_tap("DOWN")
            s.run_ms(80)
        s.do_tap("YES")
        s.run_ms(300)
        s.do_tap("YES")
        s.run_ms(300)

    def select_type(self, want: int, hint: int) -> int:
        """Open the list, move DOWN `downs` times and commit; try the hint and its neighbours until the track has WANT."""
        for downs in (hint, hint + 1, hint - 1, hint + 2, hint + 3):
            self.select(downs)
            if self.type_now() == want:
                return downs
        raise Fail(
            f"{self.tag}: no list position gives type {want}; track has {self.type_now()}"
        )

    def note(self, wav: Path, trig_frame: int) -> np.ndarray:
        """Play TRIG 1 from the DSP block `trig_frame`, the same instruction after the block's boundary in every run."""
        emu = self.emu
        if self.frames() >= trig_frame - 20:
            raise Fail(
                f"{self.tag}: setup took until frame {self.frames()}; raise TRIG_FRAME"
            )
        emu.call("run_until", frames=trig_frame)
        # half a block on: the key reaches the sequencer well away from a block boundary, so images whose ColdFire code differs by a
        # few hundred instructions on the key path still start the note in the same block
        emu.step(PRESS_OFFSET)
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.button(H.button(TRIG1), True)
        # the key stays down for the whole capture: the engine's release would reach the two images a block apart (the
        # ColdFire code on the key path differs) and shape the tail by a few 1e-5; nothing time-dependent is left in the comparison
        emu.call("run_until", frames=trig_frame + NOTE_FRAMES)
        emu.call("pcm.tap", action="capture_stop")
        emu.button(H.button(TRIG1), False)
        self.s.run_ms(50)
        self.s.health(f"note {self.tag}")
        return hook_ab.read_wav(wav)

    def finish(self) -> dict[str, Any]:
        """Close the daemon (which writes the write-watch summary) and read what it saw."""
        overlay = self.work / f"{self.tag}.overlay.bin"
        try:
            self.emu.call("card.overlay", path=str(overlay))
        except Exception as e:  # noqa: BLE001
            print(f"card.overlay: {e}", file=sys.stderr)
        self.s.close()
        out: dict[str, Any] = {}
        if self.summary is not None:
            for _ in range(50):
                if self.summary.exists():
                    break
                time.sleep(0.2)
            if self.summary.exists():
                d = json.loads(self.summary.read_text())
                out["write_events"] = d["events"]
                out["flush_hits"] = len(d["flush_hits"])
                out["cpushl"] = d["cpushl"]
                out["per_pc"] = d["per_pc"][:4]
        if overlay.exists():
            out["store_sector_writes"] = len(arena_safety.card_store_hits(overlay))
        return out


def build_folder(work: Path, tools: Tools, device: str) -> Path:
    folder = work / "plus_drive"
    mods = folder / "digikit" / "modules"
    mods.mkdir(parents=True, exist_ok=True)
    if device == "dt2":
        samples = work / "samples"
        card_check.make_samples(samples)
        for f in samples.iterdir():
            (folder / f.name).write_bytes(f.read_bytes())
        tools.patchkit_run(
            "pack", MODULE, "--device", device, "-o", mods / "pluck-d.dkm"
        )
    else:
        tools.patchkit_run(
            "pack", MODULE_DN2, "--device", device, "-o", mods / "sine-d.dkm"
        )
    return folder


def proof(
    image: str, quick: bool, keep: Path | None, watch: bool = True
) -> dict[str, Any]:
    device = image[:3]
    work = keep or Path(tempfile.mkdtemp(prefix="pk-dkrt-machine-"))
    work.mkdir(parents=True, exist_ok=True)
    tools = Tools()
    syx_stock = config.stock(DEVICE_SYX[image])
    folder = build_folder(work, tools, device)
    dkm = next((folder / "digikit" / "modules").glob("*.dkm")).read_bytes()
    sec = parse_dsp_section(dkm)
    report: dict[str, Any] = {"image": image, "work": str(work), "dkm_bytes": len(dkm)}
    rigs: list[Rig] = []
    try:
        fn = _proof if device == "dt2" else _proof_dn2
        return fn(
            image, quick, work, tools, syx_stock, folder, dkm, sec, report, watch, rigs
        )
    finally:
        for r in rigs:
            r.s.close()  # a daemon of ours is never left running after a failed check


def _proof(
    image, quick, work, tools, syx_stock, folder, dkm, sec, report, watch, rigs
) -> dict[str, Any]:  # noqa: ANN001
    checks: list[tuple[str, bool, str]] = []
    t0 = time.monotonic()

    # ---- the reference: pluck patched in
    ref_img = work / f"{image}-ref.syx"
    ref_plan = patch(tools, syx_stock, ref_img, MODULES_STATIC + ",machines/pluck")
    ref = Rig(
        "ref",
        image,
        MODULES_STATIC + ",machines/pluck",
        ref_plan,
        ref_img,
        folder,
        work,
        watch,
    )
    rigs.append(ref)
    ref.start()
    hint = 7
    hint = ref.select_type(PLUCK_STATIC_TYPE, hint)
    ref.check(
        "the track takes the patched-in PLUCK (type 9)",
        ref.type_now() == PLUCK_STATIC_TYPE,
        f"downs {hint}",
    )
    ref_pcm = ref.note(work / "ref.wav", TRIG_FRAME)
    ref.check(
        "the reference note is audible",
        float(abs(ref_pcm).max()) > 0.01,
        f"{len(ref_pcm)} samples, peak {float(abs(ref_pcm).max()):.3f}",
    )
    report["ref"] = ref.finish()
    checks += ref.checks

    # ---- the run-time image
    run_img = work / f"{image}-run.syx"
    run_plan = patch(tools, syx_stock, run_img, MODULES_STATIC)
    run = Rig("run", image, MODULES_STATIC, run_plan, run_img, folder, work, watch)
    rigs.append(run)
    run.start()
    area = run.info_word(3)
    run.check(
        "the DSP area is where dsp-link put it",
        area == sym(run_plan, "core/dsp-link", "_dl_area"),
        f"{area:#x}",
    )
    run.check(
        "no voice row is claimed before the load",
        all(run.voice_row(k)[0] == FREE_TYPE for k in range(8)),
    )
    load = run.load_and_wait("modules/pluck-d.dkm", PLUCK_DYN_TYPE)
    report["load"] = load
    run.check(
        "dkrt_load answers LOADING", load["answer"] == LOADING, f"{load['answer']}"
    )
    run.check("the load finishes with 0", load["result"] == 0, f"{load['result']}")
    run.check(
        "the machine was not listed while the DSP code was on its way",
        load["listed_while_loading"] is False,
        f"{load['listed_while_loading']}",
    )
    run.check(
        "the machine is listed after the load", run.machine_listed(PLUCK_DYN_TYPE)
    )
    run.check(
        "the DSP loader state is idle and clean",
        run.dsp_phase() == 0 and run.w(run.dsp + D_FAILURES) == 0,
    )
    off = run.w(run.dsp + D_MOD_OFF)
    base = area + off
    imports = {}
    nimp = run.info_word(9)
    info = sym(run_plan, "core/dyndsp", "dyndsp_info")
    for i in range(nimp):
        raw = b"".join(
            run.w(info + 40 + 32 * i + 4 * k).to_bytes(4, "big") for k in range(8)
        )
        imports[raw[:28].split(b"\0")[0].decode()] = int.from_bytes(raw[28:32], "big")
    want = relocate(sec, base, imports)
    got = run.dsp_bytes(base, len(want))
    run.check(
        "the DSP area holds the relocated image byte for byte",
        got == want,
        f"{len(want)} bytes at {base:#x}, {sum(a != b for a, b in zip(got, want, strict=True))} differ",
    )
    pc = L2_SW_BASE + (base + sec["voices"][0][1] - L2_BYTE_BASE) // 2
    rows = [run.voice_row(k) for k in range(8)]
    claimed = [k for k, r in enumerate(rows) if r[0] != FREE_TYPE]
    run.check(
        "one voice row holds type 18 and the routine's pc",
        len(claimed) == 1 and rows[claimed[0]] == (PLUCK_DYN_TYPE, pc),
        f"{[(hex(a), hex(b)) for a, b in rows if a != FREE_TYPE]} want {pc:#x}",
    )
    hint = run.select_type(PLUCK_DYN_TYPE, 7)
    run.check(
        "the track takes the run-time PLUCK (type 18)",
        run.type_now() == PLUCK_DYN_TYPE,
        f"downs {hint}",
    )
    run_pcm = run.note(work / "run.wav", TRIG_FRAME)
    pcm_check(run, ref_pcm, run_pcm, report, "machine")
    # ---- unload (refused while a track holds the machine: E_TYPE_IN_USE -26), a control note, reload
    r = run.post(2, "test/dyn-pluck")
    run.check(
        "unload is refused while the track holds the machine (-26)", r == -26, f"{r}"
    )
    word = run.w(TRACK_OBJ_WORD)
    run.emu.poke32(TRACK_OBJ_WORD, word & ~0xFF00)  # the track moves to type 0 (SAMPLE)
    run.s.run_ms(100)
    r = run.post(2, "test/dyn-pluck")
    run.check("unload answers 0 once the track is elsewhere", r == 0, f"{r}")
    run.s.run_ms(300)
    rows = [run.voice_row(k) for k in range(8)]
    run.check("the voice row is free again", all(t == FREE_TYPE for t, _ in rows))
    run.check(
        "the machine is gone from the table", not run.machine_listed(PLUCK_DYN_TYPE)
    )
    run.check("the range is free again", run.w(run.dsp + D_MOD_LEN) == 0)
    if not quick:
        # control: the track is put back on type 18, the voice row is free, so the DSP renders the note as stock SAMPLE: not the pluck
        run.emu.poke32(TRACK_OBJ_WORD, word)  # back on type 18, which has no row now
        run.s.run_ms(100)
        ctl = run.note(work / "after-unload.wav", TRIG_FRAME + 2500)
        diff = int((ctl != ref_pcm).sum()) if len(ctl) == len(ref_pcm) else -1
        run.check(
            "after the unload the same note is not the pluck (the comparison has teeth)",
            diff != 0 and float(abs(ctl).max()) < 0.8 * float(abs(ref_pcm).max()),
            f"{diff} samples differ, peak {float(abs(ctl).max()):.3f} against {float(abs(ref_pcm).max()):.3f}",
        )
        load2 = run.load_and_wait("modules/pluck-d.dkm")
        run.check(
            "the reload finishes with 0 and reuses the range",
            load2["result"] == 0 and run.w(run.dsp + D_MOD_OFF) == off,
            f"{load2}",
        )
    run.s.health("end")
    report["run"] = run.finish()
    checks += run.checks
    checks += watch_checks(report, watch)
    report["checks"] = [{"name": a, "ok": b, "detail": c} for a, b, c in checks]
    report["pass"] = all(c[1] for c in checks)
    report["seconds"] = round(time.monotonic() - t0, 1)
    (work / "report.json").write_text(json.dumps(report, indent=1))
    return report


def _proof_dn2(
    image, quick, work, tools, syx_stock, folder, dkm, sec, report, watch, rigs
) -> dict[str, Any]:  # noqa: ANN001
    checks: list[tuple[str, bool, str]] = []
    t0 = time.monotonic()
    card = work / "card-dn2.img"
    build_card(tools, folder, card, syx_stock, "dn2")
    ref_img = work / f"{image}-ref.syx"
    ref_plan = patch(tools, syx_stock, ref_img, MODULES_STATIC_DN2 + ",test/dn2-sine")
    ref = Rig("ref", image, MODULES_STATIC_DN2 + ",test/dn2-sine", ref_plan, ref_img, folder, work, watch, card,
              {"DSP_FRAME_POKE": DN2_TYPE_POKE.format(SINE_STATIC_TYPE)})  # fmt: skip
    rigs.append(ref)
    ref.start()
    ref_pcm = ref.note(work / "ref.wav", TRIG_FRAME_DN2)
    ref.check(
        "the reference note is audible",
        float(abs(ref_pcm).max()) > 0.01,
        f"{len(ref_pcm)} samples, peak {float(abs(ref_pcm).max()):.3f}",
    )
    report["ref"] = ref.finish()
    checks += ref.checks

    run_img = work / f"{image}-run.syx"
    run_plan = patch(tools, syx_stock, run_img, MODULES_STATIC_DN2)
    run = Rig("run", image, MODULES_STATIC_DN2, run_plan, run_img, folder, work, watch, card,
              {"DSP_FRAME_POKE": DN2_TYPE_POKE.format(SINE_DYN_TYPE)})  # fmt: skip
    rigs.append(run)
    run.start()
    area = run.info_word(3)
    run.check(
        "the DSP area is where dsp-link put it",
        area == sym(run_plan, "core/dsp-link", "_dl_area"),
        f"{area:#x}",
    )
    run.check(
        "no voice row is claimed before the load",
        all(run.voice_row(k)[0] == FREE_TYPE for k in range(8)),
    )
    load = run.load_and_wait("modules/sine-d.dkm")
    report["load"] = load
    run.check(
        "dkrt_load answers LOADING", load["answer"] == LOADING, f"{load['answer']}"
    )
    run.check("the load finishes with 0", load["result"] == 0, f"{load['result']}")
    run.check(
        "the DSP loader state is idle and clean",
        run.dsp_phase() == 0 and run.w(run.dsp + D_FAILURES) == 0,
    )
    base = area + run.w(run.dsp + D_MOD_OFF)
    want = relocate(sec, base, run_imports(run, run_plan))
    got = run.dsp_bytes(base, len(want))
    run.check(
        "the DSP area holds the relocated image byte for byte",
        got == want,
        f"{len(want)} bytes at {base:#x}, {sum(a != b for a, b in zip(got, want, strict=True))} differ",
    )
    pc = L2_SW_BASE + (base + sec["voices"][0][1] - L2_BYTE_BASE) // 2
    rows = [run.voice_row(k) for k in range(8)]
    claimed = [k for k, r in enumerate(rows) if r[0] != FREE_TYPE]
    run.check(
        "one voice row holds type 18 and the routine's pc",
        len(claimed) == 1 and rows[claimed[0]] == (SINE_DYN_TYPE, pc),
        f"{[(hex(a), hex(b)) for a, b in rows if a != FREE_TYPE]} want {pc:#x}",
    )
    run_pcm = run.note(work / "run.wav", TRIG_FRAME_DN2)
    pcm_check(run, ref_pcm, run_pcm, report, "voice")
    r = run.post(2, "test/dyn-sine-dn2")
    run.check("unload answers 0", r == 0, f"{r}")
    run.s.run_ms(300)
    run.check(
        "the voice row is free again",
        all(run.voice_row(k)[0] == FREE_TYPE for k in range(8)),
    )
    run.check("the range is free again", run.w(run.dsp + D_MOD_LEN) == 0)
    if not quick:
        load2 = run.load_and_wait("modules/sine-d.dkm")
        run.check(
            "the reload finishes with 0 and reuses the range",
            load2["result"] == 0,
            f"{load2}",
        )
    run.s.health("end")
    report["run"] = run.finish()
    checks += run.checks
    checks += watch_checks(report, watch)
    report["checks"] = [{"name": a, "ok": b, "detail": c} for a, b, c in checks]
    report["pass"] = all(c[1] for c in checks)
    report["seconds"] = round(time.monotonic() - t0, 1)
    (work / "report.json").write_text(json.dumps(report, indent=1))
    return report


def pcm_check(
    run: Rig,
    ref_pcm: np.ndarray,
    run_pcm: np.ndarray,
    report: dict[str, Any],
    what: str,
    until: int | None = None,
) -> None:
    """The run-time PCM equals the reference's: exactly, or sample for sample from the note's onset on. The two images do not
    reach the audition at the same ColdFire instruction (the run-time image has loaded a module, the Digitone II reference has
    a machine module's code on the key path) and the PCM tap starts at the block in progress, so the capture may start up to a
    few frames apart; the sound, from the onset on, must not differ in a single sample. UNTIL (interleaved samples after the onset)
    cuts the comparison before the key is released: on the Digitone II the release reaches the engine a block apart in the two images."""
    exact = len(run_pcm) == len(ref_pcm) and bool(np.array_equal(run_pcm, ref_pcm))
    shift, aligned_differ = aligned_compare(ref_pcm, run_pcm, until)
    report["pcm"] = {
        "samples": len(run_pcm),
        "exact": exact,
        "onset_shift_samples": shift,
        "differ_after_alignment": aligned_differ,
        "compared_samples_after_onset": until,
        "peak": float(abs(run_pcm).max()),
    }
    run.check(
        f"PCM of the run-time {what} equals the patched-in {what}'s (exact, or sample for sample from the onset on)",
        (exact and until is None)
        or (
            shift is not None
            and shift % 2 == 0
            and abs(shift) <= 128
            and aligned_differ == 0
        ),
        f"{len(run_pcm)} samples, exact {exact}, onset shift {shift}, {aligned_differ} differ after alignment",
    )


def aligned_compare(
    ref: np.ndarray, run: np.ndarray, until: int | None = None
) -> tuple[int | None, int]:
    """(onset of RUN minus onset of REF in samples, samples that differ over the common length from the onsets on)."""

    def onset(x: np.ndarray) -> int:
        nz = np.flatnonzero(np.abs(x) > 1e-6)
        return int(nz[0]) if len(nz) else -1

    a, b = onset(ref), onset(run)
    if a < 0 or b < 0:
        return None, -1
    n = min(len(ref) - a, len(run) - b)
    if until is not None:
        n = min(n, until)
    return b - a, int((ref[a : a + n] != run[b : b + n]).sum())


def run_imports(run: Rig, plan: dict) -> dict[str, int]:
    """The DSP symbols `dyndsp_info` exports (name, value), as the ColdFire sees them."""
    out = {}
    info = sym(plan, "core/dyndsp", "dyndsp_info")
    for i in range(run.info_word(9)):
        raw = b"".join(
            run.w(info + 40 + 32 * i + 4 * k).to_bytes(4, "big") for k in range(8)
        )
        out[raw[:28].split(b"\0")[0].decode()] = int.from_bytes(raw[28:32], "big")
    return out


def watch_checks(report: dict[str, Any], watch: bool) -> list[tuple[str, bool, str]]:
    checks = []
    for tag in ("ref", "run"):
        rep = report[tag]
        if watch:
            checks.append(
                (
                    f"{tag}: write-watch: no stray write into cf.expansion",
                    rep.get("write_events") == 0,
                    f"{rep.get('write_events')} {rep.get('per_pc')}",
                )
            )
            checks.append(
                (
                    f"{tag}: flush check: no hit",
                    rep.get("flush_hits") == 0,
                    f"{rep.get('flush_hits')} hits, {rep.get('cpushl')} cpushl",
                )
            )
        checks.append(
            (
                f"{tag}: no card store sector written",
                rep.get("store_sector_writes") == 0,
                f"{rep.get('store_sector_writes')}",
            )
        )
    for n, ok, d in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {n} {d}", flush=True)
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("proof")
    p.add_argument("--image", default="dt2-1.17")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--keep", type=Path)
    p.add_argument("--no-watch", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = proof(a.image, a.quick, a.keep, not a.no_watch)
    except (Fail, ProofError, H.Failure) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in rep.items() if k != "checks"}, indent=1))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
