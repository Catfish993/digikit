"""ColdFire-aware disassembly.

Capstone 5 cannot decode several ColdFire-only opcodes. Two bite hard on this
firmware, and both silently desynchronise a linear sweep:

  MVS/MVZ  0111 ddd1 ss mmmrrr   e.g. 0x71F9 -- sits directly on the bootstrap
                                 version gate; misreading it inverts the answer.
  FF1.L    0000 0100 11 000rrr   (0x04C0-0x04C7) -- also unimplemented in
                                 Unicorn, see emu/boot.py.

Prefer Ghidra's 68000:BE:32:Coldfire for real work. This module exists so
scripted sweeps do not lie to you.

Always start a sweep at a known instruction boundary: a prologue
(link a6,#N = 0x4E56, or lea -N(a7),a7 = 0x4FEF) or a verified branch target.
"""

import struct

from capstone import CS_ARCH_M68K, CS_MODE_BIG_ENDIAN, CS_MODE_M68K_040, Cs

_md = Cs(CS_ARCH_M68K, CS_MODE_BIG_ENDIAN | CS_MODE_M68K_040)


def decode_mvsz(img, off):
    """-> (mnemonic, operands, size) for ColdFire MVS/MVZ, else None."""
    if off + 2 > len(img):
        return None
    w = struct.unpack_from(">H", img, off)[0]
    if (w & 0xF100) != 0x7100:
        return None
    dn, ss, mmm, rrr = (w >> 9) & 7, (w >> 6) & 3, (w >> 3) & 7, w & 7
    mn = {0: "mvs.b", 1: "mvs.w", 2: "mvz.b", 3: "mvz.w"}[ss]
    if mmm == 7 and rrr == 1:
        return mn, "$%08x.l, d%d" % (struct.unpack_from(">I", img, off + 2)[0], dn), 6
    if mmm == 7 and rrr == 0:
        return mn, "$%04x.w, d%d" % (struct.unpack_from(">H", img, off + 2)[0], dn), 4
    if mmm == 5:
        d16 = struct.unpack_from(">h", img, off + 2)[0]
        return mn, "%s$%x(a%d), d%d" % ("-" if d16 < 0 else "", abs(d16), rrr, dn), 4
    if mmm == 6:  # (d8, An, Xn) -- indexed
        ext = struct.unpack_from(">H", img, off + 2)[0]
        xn = (ext >> 12) & 0xF
        xreg = ("a%d" % (xn - 8)) if xn >= 8 else ("d%d" % xn)
        wl = "l" if ext & 0x0800 else "w"
        disp = ext & 0xFF
        return mn, "$%x(a%d, %s.%s), d%d" % (disp, rrr, xreg, wl, dn), 4
    simple = {
        0: "d%d, d%%d" % rrr,
        2: "(a%d), d%%d" % rrr,
        3: "(a%d)+, d%%d" % rrr,
        4: "-(a%d), d%%d" % rrr,
    }
    if mmm in simple:
        return mn, simple[mmm] % dn, 2
    return None


def decode_ff1(img, off):
    w = struct.unpack_from(">H", img, off)[0]
    if 0x04C0 <= w <= 0x04C7:
        return "ff1.l", "d%d" % (w & 7), 2
    return None


def disasm(img, base, start, end, annotate=None):
    """Yield (addr, hexbytes, mnemonic, operands) over [start, end).

    `img` loads at `base`; addresses are virtual. Undecodable words are
    emitted as .word rather than silently skipped.
    """
    pc = start
    while pc < end:
        off = pc - base
        hit = decode_mvsz(img, off) or decode_ff1(img, off)
        if hit:
            mn, ops, sz = hit
            yield pc, img[off : off + sz].hex(), mn, ops
            pc += sz
            continue
        produced = False
        for ins in _md.disasm(img[off : end - base], pc):
            nxt = ins.address - base
            if ins.address > pc and (decode_mvsz(img, nxt) or decode_ff1(img, nxt)):
                break
            yield ins.address, ins.bytes.hex(), ins.mnemonic, ins.op_str
            pc = ins.address + ins.size
            produced = True
            if pc >= end:
                break
            o = pc - base
            if decode_mvsz(img, o) or decode_ff1(img, o):
                break
        if not produced:
            w = struct.unpack_from(">H", img, pc - base)[0]
            yield pc, "%04x" % w, ".word", "$%04x" % w
            pc += 2


def main():
    import sys

    img = open(sys.argv[1], "rb").read()  # noqa: SIM115
    base = int(sys.argv[2], 16)
    start, end = int(sys.argv[3], 16), int(sys.argv[4], 16)
    for a, hx, mn, ops in disasm(img, base, start, end):
        print("%08x  %-22s %-9s %s" % (a, hx, mn, ops))


if __name__ == "__main__":
    main()
