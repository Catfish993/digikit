#!/usr/bin/env python3
"""Scan the DSP image (container section 7) of a firmware .syx for stores the SHARC+ ISA does not have.

    uv run python -m tools.patchkit.pk_isacheck IMAGE.syx

A store through an index register (Types 3a, 3b, 3d, 15a, 15b) cannot take a source register of the same DAG: `DM(x, Ia) = Ureg` not a
DAG1 register, `PM(x, Ic) = Ureg` not a DAG2 register (finding 49 section 10; the rule is `ureg_class_problems` here, the same one
`patchkit build` fails a module build on: packages/patchkit/src/build/sharc_obj.rs). The stock images have none. `mise run patch:preflight` runs this scan after the Rust checks.

The scan decodes the code blocks of the boot stream with the aligned decoder that built the sharcdb databases (`sharcflow.aligned`):
the stock code blocks (`sharcinv.CODE_BLOCKS`, the layout of the DT2 images; the Digitone II list of `sharcdb.schema.KNOWN_CODE_BLOCKS`)
and the blocks a patch appended at the end of the stream (the run of blocks below 0x80000000 before the final block). Data blocks are not
scanned: decoded as code they give false stores (finding 49 section 10.2, 105 on DN2 1.12).
Exit status 1 when a store is found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tools.firmware.container import sections
from tools.firmware.elz import depack_section
from tools.firmware.extract import classify
from tools.sharc_dev import sharcinv, sharcldr

ROOT = Path(__file__).resolve().parents[2]


def _field(fields: dict[str, int], stem: str) -> int | None:
    """Join the pieces ``stem[hi:lo]`` of a decoded form into one number."""
    parts = []
    for k, v in fields.items():
        m = re.fullmatch(re.escape(stem) + r"\[(\d+):(\d+)\]", k)
        if m:
            parts.append((int(m.group(2)), v))
    if not parts:
        return fields.get(stem)
    return sum(v << lo for lo, v in parts)


# The forms that address memory through an index register (Ia/Ic, with an immediate or register modifier): the ISA encodes the source of
# their stores as UREGXDAG1 / UREGXDAG2. Type 14a (`DM(<addr32>) = Ureg`) has no index register and takes the full UREG class.
INDEXED_MEMORY_FORMS = frozenset({"3a", "3b", "3d", "15a", "15b"})


def ureg_class_problems(fields: dict[str, int], where: str, form: str) -> list[str]:
    """A store through DM with an index register cannot take a DAG1 register (I0-I7, M0-M7, L0-L7, B0-B7) and a store through PM cannot
    take a DAG2 register: the ISA encodes the source of such a store as UREGXDAG1 / UREGXDAG2 (SHARC+ PRM 15-7, 15-10, 13-13: "the Ureg
    may not be from the same DAG as Ia/Mb or Ic/Md"). `form` is the instruction form id (Types 3a, 3b, 3d, 15a, 15b are checked; Type 14a,
    the direct-address store, is exempt, as is every other form). Loads take every register. The stock DSP image has not one store of the
    excluded kind (finding 49 section 10). The assembler and the emulator accept them; the silicon does not run them as written."""
    if form not in INDEXED_MEMORY_FORMS:
        return []
    d = _field(fields, "d")
    u = _field(fields, "ureg")
    if d != 1 or u is None or not 0x10 <= u <= 0x4F:
        return []
    g = _field(fields, "g")
    if g is None:
        return []  # a form with no DM/PM select (an instruction word with no memory-side choice)
    dag = 1 if (u & 0xF) < 8 else 2
    if (g == 0 and dag == 1) or (g == 1 and dag == 2):
        name = "IMLB"[(u >> 4) - 1] + str(u & 0xF)
        return [f"{where}: {'PM' if g else 'DM'} store of {name} (DAG{dag})"]
    return []


FINAL_BIT = 1 << 15
# The stock code blocks, by the target address they load to (the indices differ between images, the targets do not). Left out: the DT2
# block at 0x282d7158 (388 bytes; its one hit, PM store of B10 at pc 0x16b8fe, is inside a data table) and the DN2 block at 0x2001e888
# (data decoded as code: 231 hits outside every function; finding 49 section 10.2).
DT2_CODE_TARGETS = {
    0x20000000,
    0x2838004C,
    0x283801A4,
    0x28380328,
    0x28380548,
    0x283826C8,
    0x283827CC,
}
DN2_CODE_TARGETS = DT2_CODE_TARGETS | {0x2838261C, 0x28382720}


def section7(syx: Path) -> bytes:
    """The depacked DSP boot stream of a firmware file."""
    c, secs = sections(syx)
    for sid, off, clen, _dest in secs:
        if sid == 7:
            kind, payload = classify(bytes(c[off : off + clen]))
            return depack_section(payload) if kind == "packed" else payload
    raise SystemExit(f"{syx}: no section 7")


def code_blocks(blocks: list[dict]) -> list[int]:
    """Indices of the blocks to scan: stock code by target address, then the appended run before the final block."""
    out = [
        b["index"]
        for b in blocks
        if not b["fill"] and b["byte_count"] and b["target_address"] in DN2_CODE_TARGETS
    ]
    k = len(blocks) - 1
    if blocks and blocks[k]["block_code"] & FINAL_BIT:
        k -= 1
    while k >= 0:
        b = blocks[k]
        if b["fill"] or not b["byte_count"] or b["target_address"] >= 0x80000000:
            break
        if b["index"] not in out:
            out.append(b["index"])
        k -= 1
    return sorted(out)


def scan(blob: bytes) -> list[str]:
    """One message per excluded store in the code blocks of the boot stream `blob`."""
    blocks = sharcldr.parse_blocks(blob)
    mem = sharcldr.LoadedMemory.from_stream(blob, blocks)
    by = {b["index"]: b for b in blocks}
    found = []
    for idx in code_blocks(blocks):
        r = sharcinv.analyze_block(mem, by, idx, 8)
        if r is None:
            continue
        for off, ins in r["insns"]:
            for msg in ureg_class_problems(ins.fields, "", ins.type_name):
                sw = r["base_sw"] + off // 2
                found.append(f"block {idx} pc {sw:#x}{msg}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    found = scan(section7(Path(argv[0])))
    if found:
        print(
            f"ISA check: FAIL {len(found)} store(s) the ISA does not allow in section 7 (finding 49 section 10)"
        )
        for f in found:
            print("  " + f)
        return 1
    print(
        "ISA check: ok, no store through an index register of a same-DAG register in section 7"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
