"""A rig for the FX adapter core/fx: its chain on the concrete SHARC+ core, one block per call, as the hook at H1 makes it.

    rig = FxRig([Row("_te_half", "_te_state_a")])      # rows of the chain: process symbol, state symbol, enabled, parameter words
    b = rig.block(master)                                # master: 64 floats (32 left, 32 right) before the hook
    b.master                                             # the 64 floats after the hook; b.instructions, b.returned, b.ctx_bad, b.regs_bad

The adapter (modules/core/fx/src/dsp/fx.s) and the effect sources are assembled and linked the way the patch does: the table at TABLE, the master
block at MASTER, the hook's tail (the stock function's second instruction) at STOCK_BP. Data symbols (`.PKDATA`, `.PKBSS`, `.VAR`) go to free L2;
`.PKBSS` memory (the effects' states) starts poisoned, so an effect that reads a state the adapter did not zero shows. Every call draws a hostile
context (M5 ... MODE1) and fills the registers the stock function expects back with markers; both must come back unchanged.
Plain Python (no numpy, no pytest): it also runs under PyPy."""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from pathlib import Path

import support.dsp_voice_rig as dv
from tools.patchkit import pk_build, pk_sharc

ROOT = dv.ROOT
ADAPTER = ROOT / "modules/core/fx/src/dsp/fx.s"
EFFECTS = ROOT / "tests/support/fx_effects.s"
CODE_BASE = 0xB90000
DATA_BASE = 0x200A0000
DATA_END = 0x20100000
MASTER = 0x2009E000  # byte address of the 64 master floats
TABLE = dv.TABLE
ROW_WORDS = 16
POISON = 0x7FC00001  # the initial word of every .PKBSS
nw = dv.nw
# the row words (core/fx module.toml)
PC, DESC, STATE, ENABLED, P0, WAS, DIS, WSTART, WCOUNT, NFAULT = (
    0,
    1,
    2,
    3,
    4,
    8,
    10,
    11,
    12,
    13,
)
# registers the stock function expects back (callee-saved, its arguments) and the markers they carry into the hook
MARKS = {f"R{n}": 0x7000 + n for n in range(16)}
MARKS.update(
    {
        "I0": 0x20000010,
        "I1": 0x20000020,
        "I2": 0x20000030,
        "I3": 0x20000040,
        "I4": 0x20000050,
        "I5": 0x20000060,
        "I12": 0x20000070,
        "I13": 0x20000080,
        "I8": 0x20000090,
        "I9": 0x200000A0,
        "I10": 0x200000B0,
        "I11": 0x200000C0,
        "I14": 0x200000D0,
        "I15": 0x200000E0,
        "M1": 11,
        "M2": 12,
        "M3": 13,
        "M4": 14,
        "M8": 5,
        "M9": 6,
        "M10": 7,
        "M11": 8,
        "M12": 9,
        "B3": 0x20000040,
    }
)


def f2w(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def w2f(w: int) -> float:
    return struct.unpack("<f", struct.pack("<I", w & 0xFFFFFFFF))[0]


@dataclass
class Row:
    pc: str  # the process routine's symbol
    state: str  # the state block's data symbol
    enabled: int = 1
    params: tuple[int, ...] = (0, 0, 0, 0)
    desc: str = "_te_desc"


@dataclass
class Block:
    master: tuple[float, ...]  # the 64 floats after the hook
    instructions: int
    returned: bool  # the hook ended in the stock function's second instruction
    halt: dict
    ctx_bad: list[str]  # context registers that did not come back
    regs_bad: list[str]  # marked registers that did not come back
    raw: dict = field(default_factory=dict)


@dataclass
class _Built:
    linked: list
    data: dict[str, int]
    inits: list[tuple[int, bytes, bool]]
    symbols: dict[str, int]


_BUILT: dict[tuple, _Built] = {}


def _assemble(path: Path) -> pk_sharc.SharcObject:
    dirs = [ROOT / "sdk", path.parent]
    return pk_sharc.assemble(pk_build.expand_includes(path.read_text(), dirs))


def build(sources: tuple[Path, ...] = (EFFECTS,)) -> _Built:
    key = (ADAPTER, *sources)
    if key not in _BUILT:
        _BUILT[key] = _build(sources)
    return _BUILT[key]


def _build(sources: tuple[Path, ...]) -> _Built:
    objs = [_assemble(ADAPTER)] + [_assemble(Path(s)) for s in sources]
    addr = DATA_BASE
    data: dict[str, int] = {}
    inits: list[tuple[int, bytes, bool]] = []
    for o in objs:
        where: dict[str, int] = {}
        for sec, (raw, _al, is_bss) in o.data.items():
            where[sec] = addr
            inits.append((addr, raw, is_bss))
            addr += (len(raw) + 3) & ~3
        for name, (sec, off, _g) in o.data_symbols.items():
            data[name] = where[sec] + off
    if addr > DATA_END:
        raise RuntimeError(f"data symbols need {addr - DATA_BASE:#x} bytes of L2")
    externs = {
        "_fx_table": TABLE,
        "_fx_master": MASTER,
        "_fx_back": dv.STOCK_BP,
        **data,
    }
    bases, base = [], CODE_BASE
    for o in objs:
        bases.append(base)
        base += (len(o.section) // 2 + 0xFF) & ~0xFF
    if base > dv.STOCK_BP:
        raise RuntimeError(f"code ends at {base:#x}, past the stand-ins")
    syms: dict[str, int] = {}
    for o, b in zip(objs, bases, strict=True):
        syms.update({n: b + off for n, (off, _g) in o.symbols.items()})
    ext = {**externs, **syms}
    linked = [pk_sharc.link(o, b, ext) for o, b in zip(objs, bases, strict=True)]
    return _Built(linked, data, inits, syms)


class FxRig:
    def __init__(
        self,
        rows: list[Row],
        sources: tuple[Path, ...] = (EFFECTS,),
        seed: int = 0xD1617,
        step_limit: int = 200_000,
        record_stores: bool = False,
    ) -> None:
        if len(rows) > 4:
            raise ValueError("a chain has at most 4 effects")
        self.built = build(sources)
        self.rows = rows
        self.ctx_rng = random.Random(seed)
        self.step_limit = step_limit
        self.last_ctx: dict[str, int] = {}
        self.record_stores = record_stores
        self.reset()

    def reset(self) -> None:
        b = self.built
        self.sess = pk_sharc.CallSession(b.linked, dv.RETURN_SW, breaks=(dv.STOCK_BP,))
        poke = self.sess.poke
        for k in range(5 * ROW_WORDS):
            poke(nw(TABLE) + k, 0)
        for k in range(64):
            poke(nw(MASTER) + k, 0)
        for addr, raw, is_bss in b.inits:
            for k in range(len(raw) // 4):
                w = (
                    POISON
                    if is_bss
                    else int.from_bytes(raw[4 * k : 4 * k + 4], "little")
                )
                poke(nw(addr) + k, w)
        for i, r in enumerate(self.rows):
            base = nw(TABLE) + i * ROW_WORDS
            poke(base + PC, b.symbols[r.pc])
            poke(base + DESC, b.data[r.desc])
            poke(base + STATE, b.data[r.state])
            poke(base + ENABLED, r.enabled)
            for j, p in enumerate(r.params):
                poke(base + P0 + j, p & 0xFFFFFFFF)
        if self.record_stores:
            self.sess.record_stores()

    # -- the table -------------------------------------------------------------------------------------------------------
    def row_word(self, i: int, word: int) -> int:
        return self.sess.peek(nw(TABLE) + i * ROW_WORDS + word) or 0

    def set_row_word(self, i: int, word: int, value: int) -> None:
        self.sess.poke(nw(TABLE) + i * ROW_WORDS + word, value & 0xFFFFFFFF)

    def set_enabled(self, i: int, on: bool) -> None:
        self.set_row_word(i, ENABLED, 1 if on else 0)

    def state_word(self, i: int, k: int) -> int:
        return self.sess.peek(nw(self.built.data[self.rows[i].state]) + k) or 0

    def symbol_word(self, name: str, k: int = 0) -> int:
        return self.sess.peek(nw(self.built.data[name]) + k) or 0

    # -- a block ---------------------------------------------------------------------------------------------------------
    def block(
        self, master, ctx: dict | str | None = None, regs: dict | None = None
    ) -> Block:
        if ctx is None:
            ctx = dv.draw_context(self.ctx_rng)
        elif ctx == "friendly":
            ctx = dict(dv.FRIENDLY)
        else:
            ctx = {**dv.FRIENDLY, **ctx}
        self.last_ctx = dict(ctx)
        for k, x in enumerate(master):
            self.sess.poke(nw(MASTER) + k, f2w(x))
        self.sess.poke(nw(dv.STACK) - 1, dv.RETURN_SW - 1)
        self.sess.poke(nw(dv.STACK), 0x20000FF0)
        marks = {**MARKS, **(regs or {})}
        call_regs = {**marks, **ctx, "I6": dv.STACK, "I7": dv.STACK}
        names = tuple(sorted(marks))
        res = self.sess.call(
            "_fx_entry",
            call_regs,
            max_steps=self.step_limit,
            read_regs=("I6", "I7", *names, *dv.CONTEXT_REGS),
        )
        halt = res["halt"]
        returned = halt["reason"] == "breakpoint" and res["final_pc_sw"] == dv.STOCK_BP
        out = tuple(w2f(self.sess.peek(nw(MASTER) + k) or 0) for k in range(64))
        mask = 0xFFFFFFFF
        ctx_bad = [
            n for n in dv.CONTEXT_REGS if (res["regs"][n] or 0) & mask != ctx[n] & mask
        ]
        regs_bad = [n for n in names if (res["regs"][n] or 0) & mask != marks[n] & mask]
        for n in ("I6", "I7"):
            if (res["regs"][n] or 0) & mask != dv.STACK:
                regs_bad.append(n)
        return Block(
            out,
            res["instructions"],
            returned,
            halt,
            ctx_bad if returned else [],
            regs_bad if returned else [],
            res,
        )
