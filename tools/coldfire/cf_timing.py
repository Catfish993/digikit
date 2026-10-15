"""MCF5441x ColdFire V4e core instruction execution times, as a table and a mapper.

Cycle numbers (facts only) from the MCF5441x Reference Manual, section 3.3.5 "Instruction Execution
Timing" (pages 110-118 of the manual, 3-26 to 3-34): every entry is C(R/W), core clocks at zero wait
states, no dual issue credited. Table numbers are cited at each table.

An executed-instruction record is one `insns` row of the JSON that the `cf-mix` build writes
(packages/emulator/boot/src/cfmix.rs, `DIGI_CF_MIX_OUT`): `id` is the form id of
tools/coldfire/cfisa/coldfire.json, `size` the operand size suffix, `ops` the operands in the table's
order as strings ("dn:3", "ind:2", "idx:an:xn:scale", "imm", ...), `flow` the control-flow kind.

    from tools.coldfire import cf_timing
    t = cf_timing.timing(row)      # Timing(c=..., r=..., w=..., hi=...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Timing:
    """One instruction: cycles C, operand reads R and writes W, and the cycles under the least
    favourable reading of the manual (`hi`: branch acceleration 3, RTS not predicted 8)."""

    c: int
    r: int = 0
    w: int = 0
    hi: int | None = None

    def worst(self) -> int:
        return self.c if self.hi is None else self.hi


# EA classes. Mode strings of the record map onto them.
_CLASS = {
    "dn": "rn",
    "an": "rn",
    "ind": "ind",
    "post": "post",
    "pre": "pre",
    "disp": "disp",
    "pcdisp": "disp",  # p.111: a PC-relative mode costs the same as the An-relative one
    "idx": "idx",
    "pcidx": "idx",
    "absw": "abs",
    "absl": "abs",
    "imm": "imm",
}


def ea_class(op: object) -> str | None:
    """The EA class ("rn", "ind", "post", "pre", "disp", "idx", "abs", "imm") of an operand."""
    if not isinstance(op, str):
        return None
    return _CLASS.get(op.split(":", 1)[0])


# MOVE.{B,W,L} (Tables 3-13 and 3-14, p.112-113; the numbers of the two tables are identical).
# Source class -> tuple over destinations (Rx, (Ax), (Ax)+, -(Ax), (d16,Ax), (d8,Ax,Xi*SF), xxx.wl);
# each cell (C, R, W) or None where the table has a dash (no such encoding).
_MOVE_DST = ("rn", "ind", "post", "pre", "disp", "idx", "abs")
_MOVE = {
    "rn": ((1, 0, 0), (1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (2, 0, 1), (1, 0, 1)),
    "ind": (
        (1, 1, 0),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (3, 1, 1),
        (2, 1, 1),
    ),
    "post": (
        (1, 1, 0),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (3, 1, 1),
        (2, 1, 1),
    ),
    "pre": (
        (1, 1, 0),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (2, 1, 1),
        (3, 1, 1),
        (2, 1, 1),
    ),
    "disp": ((1, 1, 0), (2, 1, 1), (2, 1, 1), (2, 1, 1), (2, 1, 1), None, None),
    "idx": ((2, 1, 0), (3, 1, 1), (3, 1, 1), (3, 1, 1), None, None, None),
    "abs": ((1, 1, 0), (2, 1, 1), (2, 1, 1), (2, 1, 1), None, None, None),
    "imm": ((1, 0, 0), (1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), None, None),
}


def move_timing(src: str, dst: str) -> Timing:
    row = _MOVE[src]
    cell = row[_MOVE_DST.index(dst)]
    if cell is None:
        # A combination the table does not list (it does not encode in 6 bytes with those modes):
        # price it as the nearest listed cell, source cost plus destination store.
        base = row[0]
        assert base is not None
        c, r, _ = base
        return Timing(c + 1, r, 1)
    c, r, w = cell
    return Timing(c, r, w)


def _ea_read(cls: str) -> Timing:
    """<ea>,Rx forms (ADD/AND/OR/SUB/CMP .L, MVS, MVZ, TST; Tables 3-15 to 3-17)."""
    if cls in ("rn", "imm"):
        return Timing(1)
    if cls == "idx":
        return Timing(2, 1, 0)
    return Timing(1, 1, 0)


def _ea_rmw(cls: str) -> Timing:
    """Dy,<ea> and #imm,<ea> read-modify-write forms (ADD/AND/OR/SUB/EOR Dy,<ea>; ADDQ/SUBQ)."""
    if cls == "rn":
        return Timing(1)
    if cls == "idx":
        return Timing(2, 1, 1)
    return Timing(1, 1, 1)


def _ea_store(cls: str) -> Timing:
    """CLR and MOV3Q to <ea> (Tables 3-15, 3-17): one write, 2 cycles with an indexed EA."""
    if cls == "rn":
        return Timing(1)
    if cls == "idx":
        return Timing(2, 0, 1)
    return Timing(1, 0, 1)


def _bit_dy(name: str, cls: str) -> Timing:
    """BTST/BCHG/BCLR/BSET Dy,<ea> (Table 3-16, p.114)."""
    if name == "btst_r":
        if cls == "rn":
            return Timing(2)
        return Timing(2 if cls == "idx" else 1, 1, 0)
    if cls == "rn":
        return Timing(2)
    return Timing(3 if cls == "idx" else 2, 1, 1)


def _bit_imm(name: str, cls: str) -> Timing:
    """BTST/BCHG/BCLR/BSET #imm,<ea> (Table 3-16, p.114)."""
    if name == "btst_i":
        return Timing(1) if cls == "rn" else Timing(1, 1, 0)
    return Timing(2) if cls == "rn" else Timing(2, 1, 1)


# One-register-operand instructions at Dn, 1 cycle (Table 3-15, p.113): BITREV, BYTEREV, EXT, EXTB,
# FF1, NEG, NEGX, NOT, SATS, Scc, SWAP. Table 3-16: ADDI, ANDI, CMPI, EORI, ORI, SUBI (#imm,Dx),
# ADDX, SUBX, ASL/ASR/LSL/LSR, MOVEQ. Table 3-17 (p.115-116): PULSE, TPF.
_ONE_CYCLE = {
    "bitrev",
    "byterev",
    "ff1",
    "negx",
    "neg",
    "not",
    "swap",
    "ext_w",
    "ext_l",
    "extb_l",
    "sats",
    "scc",
    "ori",
    "andi",
    "subi",
    "addi",
    "eori",
    "cmpi",
    "addx",
    "subx",
    "asr_i",
    "asl_i",
    "asr_r",
    "asl_r",
    "lsr_i",
    "lsl_i",
    "lsr_r",
    "lsl_r",
    "moveq",
    "pulse",
    "tpf",
    "move_from_sr",
    "move_from_ccr",
    "move_to_ccr",
}

# EMAC (Table 3-18, p.116-117): MAC/MSAC 1, with a load 1(1/0); register moves 1; MACSR load 8,
# MASK load 7; muls/mulu 4 (muls.w and mulu.w with (d8,An,Xn*SF) 5).
_EMAC_ONE = {
    "mac",
    "msac",
    "move_to_acc",
    "move_acc_acc",
    "move_from_acc",
    "movclr",
    "move_from_macsr",
    "move_macsr_ccr",
    "move_to_accext01",
    "move_from_accext01",
    "move_to_accext23",
    "move_from_accext23",
    "move_from_mask",
}
_EMAC_LOAD = {"mac_load", "msac_load"}
_EMAC_STORE = {  # an accumulator-side register read into a GPR: exposed pipeline costs 4 after a load/MAC
    "move_from_acc",
    "movclr",
    "move_from_macsr",
    "move_from_mask",
    "move_from_accext01",
    "move_from_accext23",
}
_EMAC_PRODUCER = {"mac", "msac", "mac_load", "msac_load", "move_to_acc"}


def emac_exposed(prev_id: str, id_: str) -> bool:
    """Table 3-18 note (p.117): a store of an EMAC register right after a load, MAC or MSAC
    takes 4 cycles instead of 1."""
    return id_ in _EMAC_STORE and prev_id in _EMAC_PRODUCER


def _regs_in(op: object) -> int:
    return op.get("n", 0) if isinstance(op, dict) else 0


def timing(row: dict) -> Timing | None:
    """C(R/W) of one instruction record, or None for a form the tables do not cover (FPU forms)."""
    id_ = row["id"]
    ops = row.get("ops", [])
    cls = [ea_class(o) for o in ops]
    first = next((c for c in cls if c), "rn")

    if id_ in ("move", "movea"):
        return move_timing(cls[0] or "rn", cls[1] or "rn")
    if id_ in _ONE_CYCLE:
        return Timing(1)
    if id_ == "move_to_sr":
        return Timing(
            4
        )  # p.116: 1 when #imm sets SR[13]; the firmware's use is not distinguished
    if id_ == "movec":
        return Timing(20, 0, 1)
    if id_ in ("btst_r", "bchg_r", "bclr_r", "bset_r"):
        return _bit_dy(id_, cls[1] or "rn")
    if id_ in ("btst_i", "bchg_i", "bclr_i", "bset_i"):
        return _bit_imm(id_, cls[1] or "rn")
    if id_ == "clr":
        return _ea_store(cls[0] or "rn")
    if id_ == "tst":
        return _ea_read(first)
    if id_ == "tas":
        return Timing(2, 1, 1) if first == "idx" else Timing(1, 1, 1)
    if id_ == "lea":
        return Timing(2) if first == "idx" else Timing(1)
    if id_ == "pea":
        return Timing(2, 0, 1) if first == "idx" else Timing(1, 0, 1)
    if id_ in ("mulu_l", "muls_l"):
        return Timing(4, 0 if first in ("rn", "imm") else 1, 0)
    if id_ in ("mulu_w", "muls_w"):
        return Timing(5 if first == "idx" else 4, 0 if first in ("rn", "imm") else 1, 0)
    if id_ in ("divu_w", "divs_w"):
        return Timing(
            21 if first == "idx" else 20, 0 if first in ("rn", "imm") else 1, 0
        )
    if id_ in ("divu_l", "divs_l", "remu_l", "rems_l"):
        return Timing(35, 0 if first in ("rn", "imm") else 1, 0)
    if id_ in ("addq", "subq"):
        return _ea_rmw(cls[1] or "rn")
    if id_ == "mov3q":
        return _ea_store(cls[1] or "rn")
    if id_ in (
        "add_to_d",
        "sub_to_d",
        "and_to_d",
        "or_to_d",
        "adda",
        "suba",
        "cmp",
        "cmpa_w",
        "cmpa_l",
        "mvs",
        "mvz",
    ):
        return _ea_read(first)
    if id_ in ("add_to_ea", "sub_to_ea", "and_to_ea", "or_to_ea", "eor"):
        return _ea_rmw(cls[1] or "rn")
    if id_ in _EMAC_ONE:
        return Timing(1)
    if id_ in _EMAC_LOAD:
        return Timing(1, 1, 0)
    if id_ == "move_to_macsr":
        return Timing(8)
    if id_ == "move_to_mask":
        return Timing(7)
    if id_ == "nop":
        return Timing(6)
    if id_ == "stop":
        return Timing(6)
    if id_ == "trap":
        return Timing(18, 1, 2)
    if id_ == "rte":
        return Timing(15, 2, 0)
    if id_ == "rts":
        return Timing(
            2, 1, 0, hi=9
        )  # 2 predicted, 9 mispredicted, 8 not predicted (note 3, p.118)
    if id_ == "link":
        return Timing(2, 0, 1)
    if id_ == "unlk":
        return Timing(1, 1, 0)
    if id_ in ("move_to_usp", "move_from_usp"):
        return Timing(3)
    if id_ == "wddata":
        return Timing(2 if first == "idx" else 1, 1, 0)
    if id_ == "wdebug":
        return Timing(3, 2, 0)
    if id_ == "intouch":
        return Timing(19, 1, 0)
    if id_ == "cpushl":
        return Timing(18, 0, 1)  # 9 to 18 by cache field; the larger value
    if id_ == "halt" or id_ == "illegal":
        return Timing(1)
    if id_ in ("movem_load", "movem_store"):
        n = max(_regs_in(o) for o in ops) or 1
        return Timing(n, n, 0) if id_ == "movem_load" else Timing(n, 0, n)
    # Branches (Tables 3-19, 3-20, p.118).
    if id_ == "bra":
        return Timing(
            1, 0, 1, hi=3
        )  # 1 to 3 by IFP/OEP decoupling (note 1); table prints (0/1)
    if id_ == "bsr":
        return Timing(1, 0, 1, hi=3)
    if id_ in ("jmp", "jsr"):
        w = 1 if id_ == "jsr" else 0
        if first == "ind":
            return Timing(5, 0, w)
        if first == "idx":
            return Timing(6, 0, w)
        if first == "abs" or (ops and str(ops[0]).startswith("pcdisp")):
            return Timing(1, 0, w, hi=3)  # branch-accelerated, 1 to 3 (notes 1, 2)
        return Timing(5, 0, w)
    if id_ == "bcc":
        return Timing(1)  # see bcc_cycles(): the cost depends on the prediction
    return None


def bcc_cycles(taken: int, not_taken: int, mispredicted: int) -> tuple[int, int]:
    """(low, nominal) cycles of a Bcc run `taken` + `not_taken` times with `mispredicted` of them
    mispredicted (Table 3-20, p.118): low assumes every taken branch folds in the branch cache (0)
    and nothing mispredicts; nominal costs 1 per branch and 8 per misprediction."""
    return not_taken, taken + not_taken + 8 * mispredicted


_OPREG = re.compile(r"^(dn|an|ind|post|pre|disp):(\d+)$")


def dest_register(row: dict) -> int | None:
    """The register number (0-7 Dn, 8-15 An) a register-direct destination or an in-place
    operand writes, or None. Post-increment and pre-decrement results are not counted: the manual
    states they reach the next instruction without a stall (p.111)."""
    id_ = row["id"]
    ops = row.get("ops", [])
    if id_ in (
        "cmp",
        "cmpa_w",
        "cmpa_l",
        "cmpi",
        "tst",
        "btst_r",
        "btst_i",
        "pea",
        "jmp",
        "jsr",
        "bra",
        "bsr",
        "bcc",
        "mac",
        "msac",
        "mac_load",
        "msac_load",
        "nop",
        "rts",
        "rte",
    ):
        return None
    if id_.startswith("move_to") or id_ in ("movem_store",):
        return None
    if not ops:
        return None
    last = ops[-1] if id_ not in ("movem_load",) else None
    if last is None:
        return None
    m = _OPREG.match(last) if isinstance(last, str) else None
    if m and m.group(1) in ("dn", "an"):
        n = int(m.group(2))
        return n if m.group(1) == "dn" else 8 + n
    return None


def address_uses(row: dict) -> list[tuple[int, int]]:
    """(register, stall cycles) for each register the instruction uses to form an address: an
    An base (or index with scale 1) costs 2 when it was written by the previous instruction,
    an index with scale 2 or 4 costs 3 (p.111). The 3 cycles after MULS also come out as 3 for
    an index register."""
    uses: list[tuple[int, int]] = []
    for op in row.get("ops", []):
        if not isinstance(op, str):
            continue
        parts = op.split(":")
        mode = parts[0]
        if mode in ("ind", "post", "pre", "disp"):
            uses.append((8 + int(parts[1]), 2))
        elif mode == "idx":
            an, xn, scale = int(parts[1]), int(parts[2]), int(parts[3])
            uses.append((8 + an, 2))
            uses.append((xn, 2 if scale == 1 else 3))
        elif mode == "pcidx":
            xn, scale = int(parts[1]), int(parts[2])
            uses.append((xn, 2 if scale == 1 else 3))
    return uses


def stall_cycles(prev: dict, row: dict) -> int:
    """Pipeline stall cycles `row` suffers when it directly follows `prev` (p.111) plus the
    exposed EMAC pipeline cost (p.117)."""
    stall = 0
    dest = dest_register(prev)
    if dest is not None:
        stall = max((c for r, c in address_uses(row) if r == dest), default=0)
    if emac_exposed(prev["id"], row["id"]):
        stall += 3
    return stall
