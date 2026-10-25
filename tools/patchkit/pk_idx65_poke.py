#!/usr/bin/env python3
"""Poke test of the free Sound array indices on the Digitone II (docs/findings/67-fx-routes-dn2.md): is index 65 of a Sound object used by any machine?

Index 100 of the Sound array has no row, is not saved and is not sent to the DSP (the setter accepts idx < 0x65); the send page uses it. Index 65 has no Sound row either, but
the firmware copies it into the per-voice DSP frame. If a poke of any value there changes nothing the DSP renders, on every stock machine, the index is free for a second send knob.

The stock firmware (DN2 1.12, the ready state) plays TRIG 1 live from a fixed DSP frame with the machine of track 1 chosen from the machine list (FM TONE, FM DRUM, WAVETONE, SWARMER
by the number of DOWN presses), once for each value of the halfword Sound[65] of all 16 tracks (0 as the reference, 0x3f00, 0x7f00, poked through `mem.cf.poke` before the note) and
captures the PCM and the DSP memory of the voice frames. The report names the machines whose PCM is not bit-identical to the reference and the DSP words that differ.

    uv run python -m tools.patchkit.pk_idx65_poke [--machines 0,1,2,3] [--values 0x3f00,0x7f00] [--index 65] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

from tools.patchkit import hook_ab
from tools.patchkit import pk_harness as H

KIT_PTR = 0x800052A0  # the firmware's pointer to the live kit object (devices/digitone-ii.toml [fx] kit_ptr)
SOUND_OFF, SOUND_STRIDE, ARRAY_OFF = (
    0x34,
    0x48B,
    0x14,
)  # the 16 Sound objects in the kit object; the parameter halfwords in a Sound object
FRAMES_BASE, FRAMES_BYTES = (
    0x25C48C,
    16 * 0x92 * 4,
)  # the per-slot DSP frames (finding 64 study): base and 16 slots of 0x92 words
TRIG_FRAME = 6000
NOTE_FRAMES = 1500
PRESS_OFFSET = 44_000
MACHINES = ("FM TONE", "FM DRUM", "WAVETONE", "SWARMER")


def poke_bytes(emu, addr: int, data: bytes) -> None:
    """Write bytes at any address through aligned 32-bit pokes (the ColdFire is big endian)."""
    for i, b in enumerate(data):
        a = addr + i
        w = emu.peek32(a & ~3) or 0
        sh = 24 - 8 * (a & 3)
        emu.poke32(a & ~3, (w & ~(0xFF << sh)) | (b << sh))


def peek_bytes(emu, addr: int, n: int) -> bytes:
    out = bytearray()
    for a in range(addr, addr + n):
        w = emu.peek32(a & ~3) or 0
        out.append((w >> (24 - 8 * (a & 3))) & 0xFF)
    return bytes(out)


def one(
    s: H.Session, work: Path, machine: int, index: int, value: int
) -> tuple[np.ndarray, bytes, int]:
    """Back to the ready state, machine MACHINE on track 1, Sound[INDEX] = VALUE on all 16 tracks, TRIG 1 from the fixed frame: (PCM, DSP frame bytes, the halfword read back after the note)."""
    s.reset()
    emu = s.emu
    assert emu is not None
    s.do_tap(["FUNC", "SRC"])
    s.run_ms(200)
    for _ in range(machine):
        s.do_tap("DOWN")
        s.run_ms(80)
    s.do_tap("YES")
    s.run_ms(300)
    s.do_tap("YES")
    s.run_ms(300)
    kit = emu.peek32(KIT_PTR) or 0
    for t in range(16):
        poke_bytes(
            emu,
            kit + SOUND_OFF + SOUND_STRIDE * t + ARRAY_OFF + 2 * index,
            struct.pack(">H", value),
        )
    frames = emu.call("status")["dsp_frames"]
    if frames >= TRIG_FRAME - 20:
        raise SystemExit(f"setup took until frame {frames}; raise TRIG_FRAME")
    emu.call("run_until", frames=TRIG_FRAME)
    emu.step(PRESS_OFFSET)
    wav = work / f"m{machine}-v{value:04x}.wav"
    emu.call("pcm.tap", action="capture", path=str(wav))
    emu.button(H.button("TRIG 1"), True)
    emu.call("run_until", frames=TRIG_FRAME + 200)
    dump = bytes.fromhex(
        emu.dsp_peek(FRAMES_BASE, FRAMES_BYTES)["hex"].replace("??", "00")
    )
    emu.call("run_until", frames=TRIG_FRAME + NOTE_FRAMES)
    emu.call("pcm.tap", action="capture_stop")
    emu.button(H.button("TRIG 1"), False)
    back = struct.unpack(
        ">H", peek_bytes(emu, kit + SOUND_OFF + ARRAY_OFF + 2 * index, 2)
    )[0]
    return hook_ab.read_wav(wav), dump, back


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--machines",
        default="0,1,2,3",
        help="DOWN presses in the machine list (0 FM TONE, 1 FM DRUM, 2 WAVETONE, 3 SWARMER)",
    )
    ap.add_argument("--values", default="0x3f00,0x7f00")
    ap.add_argument("--index", type=int, default=65)
    ap.add_argument("--out", type=Path, default=Path("out/runs/patchkit/idx65"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    values = [int(v, 0) for v in a.values.split(",")]
    cell = H.Cell("dn2", "1.12", [])
    report: dict = {"index": a.index, "machines": {}}
    with H.Session(cell, H.Goldens(a.out / "goldens.json"), run_name="idx65") as s:
        s.open(s.syx)
        for m in (int(x) for x in a.machines.split(",")):
            ref, ref_dump, _ = one(s, a.out, m, a.index, 0)
            row: dict = {
                "name": MACHINES[m] if m < len(MACHINES) else str(m),
                "active_samples": int((np.abs(ref) > 1e-6).sum()),
                "values": {},
            }
            for v in values:
                pcm, dump, back = one(s, a.out, m, a.index, v)
                r = hook_ab.compare(ref, pcm, 1.0)
                diff = [
                    i
                    for i in range(0, len(dump), 4)
                    if dump[i : i + 4] != ref_dump[i : i + 4]
                ]
                row["values"][f"{v:#06x}"] = {
                    "identical": bool(r["identical"]),
                    "max_err_ulps": float(r["max_err_ulps"]),
                    "read_back": back,
                    "dsp_frame_words_differ": [hex(FRAMES_BASE + i) for i in diff[:16]],
                    "n_differ": len(diff),
                }
            report["machines"][str(m)] = row
            print(json.dumps({m: row}, indent=1), flush=True)
    (a.out / "report.json").write_text(json.dumps(report, indent=1))
    ok = all(
        v["identical"]
        for r in report["machines"].values()
        for v in r["values"].values()
    )
    print(
        "PCM unchanged by the poke on every machine"
        if ok
        else "PCM CHANGED by the poke on some machine"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
