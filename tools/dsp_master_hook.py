#!/usr/bin/env python3
"""Master-bus hook for the SHARC+ DSP (spike 0.7): a routine in free L2 that
scales the final stereo block before the firmware converts it to Q31.

    uv run python tools/dsp_master_hook.py PATCH_OUT --device dt2|dn2
        [--gain 0.5 | --gain-from DM_ADDR] [--no-hook] [--listing]

The hook site is the only call of the block converter (finding 06, "Master
hook"): the converter reads 64 floats (L then R, 32 samples each) from the
master work buffer and writes interleaved Q31 into the SPORT ring. The hook
retargets that CALL to our routine, which multiplies the 64 floats in place
and tail-jumps to the converter, so the converter's frame, return and
delay-slot work are unchanged. The routine uses R0, R1, I4 only; all are dead
at the call (the converter overwrites I4 from R4 and the caller reloads the
rest from its frame).

The patch is written for `coupled-run --dsp-patch`, `tools/digi_client.py
dsp.patch` or `dsp_patch.py syx` (once the boot-stream append of spike 0.4
exists). The routine and its gain word live at L2 byte 0x200a0000 /
0x200a0100, past the last word either loader writes (DT2 0x2001ab7b, DN2
0x2008823b).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sharc_asm  # noqa: E402

# Per device: the converter, the CALL that reaches it (its operand is the hook),
# the delayed CALL's word (for the `was` check), the master float buffer, the
# BASE .syx hash.
DEVICES = {
    "dt2": {
        "syx": "Digitakt_II_OS1.16.syx",
        "base": "278541e466edcd77d6b3e018a91fb90185932d3c7de224dd3e68294dddf3a9ec",
        "converter": 0x1C74A1,
        "call_site": 0x1C7734,
        "buffer": 0x25F180,
    },
    "dn2": {
        "syx": "Digitone_II_OS1.11.syx",
        "base": "2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0",
        "converter": 0x1C9D3F,
        "call_site": 0x1C9FD2,
        "buffer": 0x268438,
    },
}

CODE_PC = 0xBD0000  # L2 byte 0x200a0000
CODE_BYTE = 0x200A0000
GAIN_ADDR = 0x200A0100  # one float word


def f32_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def build_hook(device: str, gain_addr: int = GAIN_ADDR) -> sharc_asm.Asm:
    d = DEVICES[device]
    a = sharc_asm.Asm(CODE_PC)
    a.load_abs("R0", gain_addr)
    a.ureg_imm("I4", d["buffer"])
    a.do_lcntr(64, "last")
    a.load("R1", 4, 5, post=False)  # R1 = DM(I4, M5), M5 = 0
    a.fmul(1, 1, 0)  # F1 = F1 * F0
    a.label("last")
    a.store("R1", 4, 6, post=True)  # DM(I4, M6) = R1, M6 = 1 word
    a.jump_abs(d["converter"])
    return a


def patch_text(device: str, gain: float | None, gain_addr: int, hook: bool) -> str:
    d = DEVICES[device]
    a = build_hook(device, gain_addr)
    out = [
        f"# {device} master hook (spike 0.7): scale the master float block, then run the Q31 converter.",
        f"# Routine at pc {CODE_PC:#x} (L2 byte {CODE_BYTE:#x}); gain word at DM {gain_addr:#x}.",
        f"BASE {d['base']}",
    ]
    out += a.patch_lines()
    if gain is not None:
        out.append(f"DM {gain_addr:x} {f32_bits(gain):08x}  ; gain {gain}")
    if hook:
        site = d["call_site"]
        was = f"1804{d['converter'] >> 16:04x}{d['converter'] & 0xFFFF:04x}"
        new = f"1804{CODE_PC >> 16:04x}{CODE_PC & 0xFFFF:04x}"
        out.append(f"PM {site:x} {new} was {was}  ; CALL converter -> CALL hook")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path)
    p.add_argument("--device", choices=sorted(DEVICES), required=True)
    p.add_argument(
        "--gain", type=float, default=1.0, help="initial gain written to the gain word"
    )
    p.add_argument(
        "--no-gain-word", action="store_true", help="do not write the gain word"
    )
    p.add_argument("--gain-addr", type=lambda s: int(s, 0), default=GAIN_ADDR)
    p.add_argument(
        "--no-hook", action="store_true", help="routine only, call site untouched"
    )
    p.add_argument("--listing", action="store_true")
    args = p.parse_args(argv)
    if args.listing:
        print("\n".join(build_hook(args.device, args.gain_addr).listing()))
    args.out.write_text(
        patch_text(
            args.device,
            None if args.no_gain_word else args.gain,
            args.gain_addr,
            not args.no_hook,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
