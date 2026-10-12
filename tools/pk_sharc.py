#!/usr/bin/env python3
"""SHARC+ patchkit modules: assemble with Selache, link ourselves, re-decode every word, run on the core.

Selache (public, pinned; tools/pk_toolchains.py selache) assembles VISA source to a relocatable ELF whose
parcels are big-endian. This module

  1. assembles a source file (``assemble``) and reads sections, symbols and relocations (tools/pk_elf.py),
  2. places the section at a chosen short-word (PC) address and resolves the relocations (``link``),
  3. swaps every 16-bit parcel to the boot-stream order and re-decodes every instruction with our own
     decoder (tools/sharc_isa.py): Selache's instruction extents must equal ours, no instruction may be
     uncertain, and every relocated field must read back as the intended value (``verify``),
  4. optionally runs the linked code on the concrete SHARC+ core (``run_call``, tools/sharc_run.py).

Relocations Selache emits (offsets and symbol values are in 16-bit parcels, the PC unit):
  type 11  absolute 24-bit target of a 48-bit jump/call/cjump (low 24 bits of the instruction)
  type 12  absolute 32-bit immediate/address of a 48-bit instruction (low 32 bits)
Both carry the symbol's short-word address; the addend is encoded in the symbol name ("sym + 4").

    uv run python tools/pk_sharc.py module.s --base 0xB90000 [--extern name=0xADDR ...] [--json OUT]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pk_elf  # noqa: E402
import pk_toolchains  # noqa: E402
import sharc_selache as sel  # noqa: E402

SECTION = "seg_pmco"
PROCESSOR = "ADSP-21569"
R_ABS24, R_ABS32 = 11, 12
SW_ALIAS_BASE = 0x28000000  # loader byte address of short-word 0 (tools/sharcldr.py)


class PkSharcError(RuntimeError):
    pass


@dataclass
class SharcReloc:
    parcel: int  # offset of the instruction, in parcels
    rtype: int
    symbol: str
    addend: int


@dataclass
class SharcObject:
    source_sha256: str
    section: bytes  # Selache parcel order (big-endian per 16 bits)
    symbols: dict[
        str, tuple[int, bool]
    ]  # name -> (parcel offset, global); defined symbols only
    relocs: list[SharcReloc]
    rows: list[tuple[int, str]]  # Selache instruction extents: (parcel offset, raw hex)
    toolchain: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "arch": "sharc+",
            "parcel_order": "selache-be",
            "source_sha256": self.source_sha256,
            "toolchain": self.toolchain,
            "section": self.section.hex(),
            "symbols": {
                k: {"parcel": v[0], "global": v[1]} for k, v in self.symbols.items()
            },
            "relocs": [r.__dict__ for r in self.relocs],
        }


@dataclass
class Linked:
    base_sw: int
    code_selache: bytes
    code_boot: bytes  # little-endian per parcel, the order of the boot stream and of tools/sharc_isa.py
    symbols: dict[str, int]  # short-word addresses
    checked: int = 0  # instructions re-decoded


def _tool(name: str) -> Path:
    d = pk_toolchains.selache_dir()
    if d is None:
        raise PkSharcError(
            "selache is not built (uv run python tools/pk_toolchains.py selache)"
        )
    return d / "target" / "release" / name


def split_symbol(name: str) -> tuple[str, int]:
    m = re.fullmatch(r"\s*([A-Za-z_.$][\w.$]*)\s*(?:([+-])\s*(\w+))?\s*", name)
    if not m:
        raise PkSharcError(f"cannot parse relocation symbol {name!r}")
    add = int(m.group(3), 0) * (-1 if m.group(2) == "-" else 1) if m.group(2) else 0
    return m.group(1), add


def _object(obj: Path, source_sha: str, section: str | None) -> SharcObject:
    elf = pk_elf.parse(obj.read_bytes())
    if section is None:
        section = next(
            (x.name for x in elf.sections if x.name in (SECTION, "seg_swco")), SECTION
        )
    listing = subprocess.run(
        [str(_tool("seldump")), "-ns", section, str(obj)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    sec = elf.section(section)
    if sec is None:
        raise PkSharcError(
            f"no section {section} (a .SECTION/PM {section}; directive is required)"
        )
    rows = sel.parse_listing(listing)
    if b"".join(r.raw for r in rows) != sec.data:
        raise PkSharcError("Selache listing bytes do not equal the section bytes")
    symbols = {
        s.name: (s.value, s.bind != "local")
        for s in elf.symbols
        if s.name and 0 < s.shndx < 0xFF00 and elf.sections[s.shndx].name == section
    }
    relocs = []
    for r in elf.relocs:
        if r.section != section:
            raise PkSharcError(f"relocation in unexpected section {r.section}")
        if r.symbol.name == "":
            raise PkSharcError("section-relative relocation not supported")
        name, add = split_symbol(r.symbol.name)
        relocs.append(SharcReloc(r.offset, r.rtype, name, add + r.addend))
    tc = {"selache_rev": sel.PINNED_REVISION}
    for n in ("selas", "seldump"):
        tc[n + "_sha256"] = hashlib.sha256(_tool(n).read_bytes()).hexdigest()
    return SharcObject(
        source_sha,
        sec.data,
        symbols,
        relocs,
        [(r.parcel_address, r.raw.hex()) for r in rows],
        tc,
    )


def assemble(source: str, section: str | None = None) -> SharcObject:
    with tempfile.TemporaryDirectory(prefix="pk-sharc-") as d:
        src, obj = Path(d) / "m.s", Path(d) / "m.doj"
        src.write_text(source)
        r = subprocess.run(
            [str(_tool("selas")), "-proc", PROCESSOR, "-o", str(obj), str(src)],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise PkSharcError(f"selas failed:\n{r.stdout}{r.stderr}")
        return _object(obj, hashlib.sha256(source.encode()).hexdigest(), section)


def compile_c(source: str, section: str | None = None) -> SharcObject:
    """Selache's C compiler (selcc); evaluated in docs/findings/20-patch-toolchains.md."""
    with tempfile.TemporaryDirectory(prefix="pk-sharc-") as d:
        src, obj = Path(d) / "m.c", Path(d) / "m.doj"
        src.write_text(source)
        r = subprocess.run(
            [str(_tool("selcc")), "-proc", PROCESSOR, "-c", "-o", str(obj), str(src)],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise PkSharcError(f"selcc failed:\n{r.stdout}{r.stderr}")
        return _object(obj, hashlib.sha256(source.encode()).hexdigest(), section)


def self_check(code: bytearray, at: int, size: int, r: SharcReloc) -> None:
    """Selache leaves the field zero or holding the addend; anything else means we misread the relocation."""
    mask = (1 << (8 * size)) - 1
    have = int.from_bytes(code[at : at + size], "big")
    if have not in (0, r.addend & mask):
        raise PkSharcError(
            f"parcel {r.parcel:#x}: field {have:#x} is neither 0 nor the addend {r.addend} before relocation"
        )


def link(
    obj: SharcObject, base_sw: int, externs: dict[str, int] | None = None
) -> Linked:
    externs = externs or {}
    code = bytearray(obj.section)
    syms = {n: base_sw + off for n, (off, _g) in obj.symbols.items()}
    extents = dict(obj.rows)
    for r in obj.relocs:
        if r.symbol in syms:
            target = syms[r.symbol]
        elif r.symbol in externs:
            target = externs[r.symbol]
        else:
            raise PkSharcError(f"unresolved symbol {r.symbol}")
        target += r.addend
        row = bytes.fromhex(extents.get(r.parcel, ""))
        if len(row) != 6:
            raise PkSharcError(
                f"relocation at parcel {r.parcel:#x}: not a 48-bit instruction"
            )
        at = r.parcel * 2
        if r.rtype == R_ABS24:
            if not 0 <= target < 1 << 24:
                raise PkSharcError(
                    f"{r.symbol}: {target:#x} does not fit a 24-bit target"
                )
            self_check(code, at + 3, 3, r)
            code[at + 3 : at + 6] = target.to_bytes(3, "big")
        elif r.rtype == R_ABS32:
            self_check(code, at + 2, 4, r)
            code[at + 2 : at + 6] = (target & 0xFFFFFFFF).to_bytes(4, "big")
        else:
            raise PkSharcError(f"relocation type {r.rtype} not supported")
    out = Linked(base_sw, bytes(code), sel.swap_parcels(bytes(code)), syms)
    verify(obj, out, externs)
    return out


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


def verify(obj: SharcObject, linked: Linked, externs: dict[str, int]) -> None:
    """Every instruction decodes natively with the extent Selache chose; relocated fields read back."""
    rows = []
    for parcel, raw_hex in obj.rows:
        at = parcel * 2
        raw = linked.code_selache[at : at + len(raw_hex) // 2]
        rows.append(sel.ExternalInstruction(parcel, raw, ""))
    cmp = sel.compare_rows(rows)
    problems = []
    by_parcel = {}
    for item in cmp.instructions:
        by_parcel[item.external.parcel_address] = item
        if item.native_form_id is None or not item.extent_agrees:
            problems.append(
                f"parcel {item.external.parcel_address:#x} {item.external.raw.hex()}: "
                f"native {item.native_form_id or item.native_candidates} extent_agrees={item.extent_agrees}"
            )
    for r in obj.relocs:
        item = by_parcel[r.parcel]
        fields = dict(item.native_fields)
        want = (linked.symbols.get(r.symbol, externs.get(r.symbol)) or 0) + r.addend
        got = None
        for stem in ("addr", "data", "reladdr"):
            got = _field(fields, stem)
            if got is not None:
                break
        mask = 0xFFFFFF if r.rtype == R_ABS24 else 0xFFFFFFFF
        if got != want & mask:
            problems.append(
                f"parcel {r.parcel:#x} ({item.native_form_id}): relocated field reads {got} want {want & mask:#x}"
            )
    if problems:
        raise PkSharcError(
            "Selache output disagrees with our decoder:\n  " + "\n  ".join(problems)
        )
    linked.checked = len(cmp.instructions)


def build(
    source: str, base_sw: int, externs: dict[str, int] | None = None
) -> tuple[SharcObject, Linked]:
    obj = assemble(source)
    return obj, link(obj, base_sw, externs)


# ---------------------------------------------------------------- running on the concrete core


def run_call(
    linked: Linked,
    entry: str,
    regs: dict[str, int] | None = None,
    dm_words: dict[int, int] | None = None,
    reads: dict[str, tuple[int, int]] | None = None,
    max_steps: int = 100000,
    return_sw: int = 0xB9F000,
) -> dict[str, object]:
    """Run ``entry`` (a symbol) to its RTS on the concrete core and return registers and memory reads.

    ``dm_words`` maps normal-word DM addresses (L2: 0x08000000 up) to values to poke before the run;
    ``reads`` maps a result name to (normal-word address, word count). Floats are passed as raw bits.
    """
    import sharc_run
    import sharc_trace as st
    from sharcldr import LoadedMemory

    start = linked.symbols[entry]
    blk = {
        "target_address": SW_ALIAS_BASE + 2 * linked.base_sw,
        "byte_count": len(linked.code_boot),
        "fill": False,
        "payload_offset": 0,
        "payload_len": len(linked.code_boot),
        "index": 0,
    }
    mem = LoadedMemory(linked.code_boot, [blk])
    runner = sharc_run.Runner(mem, start, regs=regs or {}, breakpoints=[return_sw])
    for addr, word in (dm_words or {}).items():
        if not st._dm_write(
            runner.state, addr, 4, st.Const(word & 0xFFFFFFFF), normal_word=True
        ):
            raise PkSharcError(f"cannot poke DM word {addr:#x}")
    runner.state.call_stack.append(return_sw)
    res = runner.run(max_steps)
    out: dict[str, object] = {
        "halt": res.halt.to_json(),
        "instructions": res.instructions,
        "final_pc_sw": res.final_pc_sw,
    }
    for name, (addr, count) in (reads or {}).items():
        vals = []
        for k in range(count):
            v = st._dm_read(runner.state, addr + k, 4, normal_word=True)
            vals.append(None if v is None else v.value & 0xFFFFFFFF)
        out[name] = vals
    out["regs"] = {
        n: getattr(runner.state.uregs.get(st.UREG_CODES[n]), "value", None)
        for n in ("R0", "R1", "R2", "R4", "R8", "I0")
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("source")
    ap.add_argument(
        "--base",
        type=lambda x: int(x, 0),
        required=True,
        help="short-word (PC) address of the section",
    )
    ap.add_argument("--extern", action="append", default=[], metavar="NAME=ADDR")
    ap.add_argument("--json", help="write the object prototype here")
    a = ap.parse_args()
    externs = {k: int(v, 0) for k, v in (e.split("=") for e in a.extern)}
    obj, linked = build(Path(a.source).read_text(), a.base, externs)
    print(
        f"linked {len(linked.code_boot)} bytes at sw {a.base:#x}; {linked.checked} instructions re-decoded OK"
    )
    for n, v in sorted(linked.symbols.items(), key=lambda kv: kv[1]):
        print(f"  {v:#08x} {n}")
    if a.json:
        Path(a.json).write_text(json.dumps(obj.to_json(), indent=1))


if __name__ == "__main__":
    main()
