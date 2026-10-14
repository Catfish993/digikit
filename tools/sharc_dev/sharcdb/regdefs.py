"""Register defs and uses of one instruction, by instruction form."""

from __future__ import annotations

from tools.sharc_dev import sharcfn, sharcinv

# --- register def/use -------------------------------------------------------
#
# Mirrors tools/sharc_dev/sharcfn/'s register-operand bit layouts (Figure 18-1/18-2/
# 18-3, Table 18-11/13/18/19/21) directly from the typed decode fields --
# never from mnemonic text, per the Type3c direction bug in the build
# report -- so the register identity always agrees with what that file's
# renderer would print for the same instruction. unmodelled sub-cases return
# an 'unknown' tag instead of a guessed def/use, per instruction, so the
# build report can count them per form.


def _compute_regdef_reguse(field23):
    """Register defs/uses for a 23-bit parallel compute field (1a/2a/
    2a_short/2b/3a/4a/5a_move/5a_swap/7a/9a_abs/9a_rel/11a). -> (defs:
    [(reg, 'compute')], uses: [reg], unknown: [tag])."""
    defs, uses, unknown = [], [], []
    cu, d = sharcinv.classify_compute(field23)
    if cu is None:
        return defs, uses, unknown
    opcode = d.get("opcode", 0)
    if cu == "ALU":
        is_float = d.get("is_float", False)
        if d.get("is_dual_addsub"):
            rs, ra, rx, ry = ((field23 >> sh) & 0xF for sh in (12, 8, 4, 0))
            Rs, Ra, Rx, Ry = (sharcfn.reg_name(r, is_float) for r in (rs, ra, rx, ry))
            defs += [(Rs, "compute"), (Ra, "compute")]
            uses += [Rx, Ry]
        else:
            name = sharcinv.ALU_OPS.get(opcode)
            rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
            Rn, Rx, Ry = (sharcfn.reg_name(r, is_float) for r in (rn, rx, ry))
            defs.append((Rn, "compute"))
            if name in sharcfn.UNARY_ALU_OPS:
                uses.append(Rx)
            else:
                # Binary, or an opcode ALU_OPS doesn't name: Table 18-11's
                # register positions are fixed regardless of which named op
                # this is, so Rn/Rx/Ry are certain even when the opcode
                # isn't; default to binary (both operands used) rather than
                # guess unary.
                uses += [Rx, Ry]
    elif cu == "MULT":
        # classify_compute()'s is_float is only correct for opcode 0x30
        # ("FN = FX*FY"); every other MULT opcode's bit 3 is MOD1's
        # Integer/Fraction result-format bit, not a float/fixed switch --
        # see tools/sharc_dev/sharcfn/'s decode_mult for the same fix and citation.
        is_float = opcode == 0x30
        rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
        Rn, Rx, Ry = (sharcfn.reg_name(r, is_float) for r in (rn, rx, ry))
        if d.get("housekeeping"):
            unknown.append("mult_housekeeping")
        elif d.get("is_plain_mul"):
            defs.append((Rn, "compute"))
            uses += [Rx, Ry]
        elif d.get("is_mac"):
            defs.append((Rn, "compute"))
            uses += [Rx, Ry, "MR"]
        else:
            unknown.append("mult_other")
    elif cu == "SHIFT":
        rn, rx, ry = ((field23 >> sh) & 0xF for sh in (8, 4, 0))
        defs.append(("R%d" % rn, "compute"))
        uses += ["R%d" % rx, "R%d" % ry]
    elif cu == "MULTIFN":
        top3 = (field23 >> 20) & 7
        is_float = bool(top3 & 1)
        if d.get("is_dual_addsub"):
            # Table 18-18/19: fixed layout, no opcode sub-table -- same
            # register math as tools/sharc_dev/sharcfn/'s decode_multifn dual branch.
            rya = (field23 & 3) + 12
            rxa = ((field23 >> 2) & 3) + 8
            rym = ((field23 >> 4) & 3) + 4
            rxm = ((field23 >> 6) & 3) + 0
            ra = (field23 >> 8) & 0xF
            rm = (field23 >> 12) & 0xF
            rs = (field23 >> 16) & 0xF
            Rm, Ra, Rs, Rxm, Rym, Rxa, Rya = (
                sharcfn.reg_name(v, is_float) for v in (rm, ra, rs, rxm, rym, rxa, rya)
            )
            defs += [(Rm, "compute"), (Ra, "compute"), (Rs, "compute")]
            uses += [Rxm, Rym, Rxa, Rya]
        else:
            # The regular MUL+ALU multifunction op's ALU sub-operation is a
            # table lookup (tools/sharc_dev/sharcfn/'s MULTIFN_ALU_SYNTAX, PGR Table
            # 12-12) whose operand count varies by row; left unmodelled here
            # rather than guessed.
            unknown.append("multifn_alu_compute")
    else:
        unknown.append("compute_cu_%r" % cu)
    return defs, uses, unknown


def _shortcompute_regdef_reguse(field12):
    """Type 2c (PRM Table 17-2/18-21): RN 7:4 is always the result; the
    unary-Rx opcodes (pass/not/float, and inc/dec -- PRM's "RN = RX +/- 1"
    reads only RX, never RN; see tools/sharc_dev/sharcfn/'s render_shortcompute)
    read only RX; every other opcode reads both RN and RX (PRM/
    tools/sharc_dev/sharcfn/'s render_shortcompute: "RN = op(RN, RX)")."""
    opcode = (field12 >> 8) & 0xF
    rn, rx = (field12 >> 4) & 0xF, field12 & 0xF
    is_float = opcode in sharcfn.FLOAT_SHORT_OPS
    Rn, Rx = sharcfn.reg_name(rn, is_float), sharcfn.reg_name(rx, is_float)
    defs = [(Rn, "compute")]
    uses = [Rx] if opcode in sharcfn._UNARY_RX_SHORT_OPS else [Rn, Rx]
    return defs, uses


def _shiftimm_regdef_reguse(f):
    """6a_mem's parallel ShiftImm sub-instruction (tools/sharc_dev/sharcfn/'s
    render_shiftimm): RN 7:4 is the result for every opcode this repo
    models, RX 3:0 is always read; the "or-" variants also read RN as the
    other OR operand; an opcode outside _SHIFTIMM_MNEMONICS (including
    btst, which that table names but whose own branch there is status-only)
    is left unmodelled."""
    defs, uses, unknown = [], [], []
    field = f.get("shiftimm", 0) & 0x7FFFFF
    opcode = (field >> 16) & 0x3F
    rn, rx = (field >> 4) & 0xF, field & 0xF
    Rn, Rx = "R%d" % rn, "R%d" % rx
    if opcode not in sharcfn._SHIFTIMM_MNEMONICS:
        unknown.append("shiftimm_unknown_opcode")
        return defs, uses, unknown
    if opcode in (0x08, 0x09):
        defs.append((Rn, "compute"))
        uses += [Rn, Rx]
    elif opcode in (0x00, 0x01, 0x10, 0x12, 0x30, 0x31, 0x32):
        defs.append((Rn, "compute"))
        uses.append(Rx)
    else:
        # btst (0x33): "[status only]" in render_shiftimm -- no Rn write.
        uses.append(Rx)
    return defs, uses, unknown


def _mem_regdef_reguse(t, f):
    """Direct (15a/14a/14d), indexed (3a/3b/3d/6a_mem), immediate-offset
    (4a/4b/4d/15b) and Type3c memory forms. Direction always comes from the
    form's own `d` field (sharcfn._space_dir, or Type3c's own `d`), never
    from mnemonic text -- see the module docstring's Type3c note."""
    defs, uses = [], []
    if t == "3c":
        # Not in tools/sharc_dev/sharcfn/'s DIRECT/INDEXED_MEM_FORMS (that module's
        # render_instruction special-cases Type3c as the R2 push idiom
        # unconditionally instead); decoded directly here from its own
        # dmi/dmm/d/dreg fields (see the module docstring's Type3c note).
        i, m, d, dreg = f.get("dmi", 0), f.get("dmm", 0), f.get("d"), f.get("dreg")
        uses += ["I%d" % i, "M%d" % m]
        if dreg is not None:
            reg = "R%d" % dreg
            if d:
                uses.append(reg)
            else:
                defs.append((reg, "mem_load"))
        return defs, uses

    space, direction = sharcfn._space_dir(f)
    dreg, ureg = f.get("dreg"), f.get("ureg")
    reg = (
        "R%d" % dreg
        if dreg is not None
        else (sharcfn.ureg_name(ureg) if ureg is not None else None)
    )
    if t in sharcfn.INDEXED_MEM_FORMS:
        uses += ["I%d" % f.get("i", 0), "M%d" % f.get("m", 0)]
    elif t in sharcfn.IMMOFF_MEM_FORMS:
        uses.append("I%d" % f.get("i", 0))
    if reg is not None:
        if direction == "store":
            uses.append(reg)
        else:
            defs.append((reg, "mem_load"))
    return defs, uses


def _dual_mem_regdef_reguse(f):
    """1a/1b: simultaneous DM(dmi,dmm)/PM(pmi,pmm) reference; dmd/pmd follow
    the same 1=store/0=load convention as sharcfn._space_dir's `d`."""
    defs, uses = [], []
    dmi, dmm, dmdreg = f.get("dmi", 0), f.get("dmm", 0), f.get("dmdreg")
    pmi, pmm, pmdreg = f.get("pmi", 0), f.get("pmm", 0), f.get("pmdreg")
    uses += ["I%d" % dmi, "M%d" % dmm, "I%d" % pmi, "M%d" % pmm]
    if dmdreg is not None:
        reg = "R%d" % dmdreg
        if f.get("dmd"):
            uses.append(reg)
        else:
            defs.append((reg, "mem_load"))
    if pmdreg is not None:
        reg = "R%d" % pmdreg
        if f.get("pmd"):
            uses.append(reg)
        else:
            defs.append((reg, "mem_load"))
    return defs, uses


def register_effects(insn_type: str, f: dict):
    """-> (defs: [(reg, kind)], uses: [reg], unknown: [tag]) for every
    register a decoded instruction defines or uses, deduplicated, from its
    typed fields (see the per-helper docstrings above and the module's
    SCHEMA comment on `regdef`/`reguse` for the kind vocabulary). Returns
    ([], [], []) for a form with no modelled register effect at all (a
    branch/call/return/NOP/EMU/etc. -- these are not "unknown", they are
    simply out of this table's scope: they define/use no general register
    tools/sharc_dev/sharc_trace.py's State.uregs models the same way this file's other
    tables do)."""
    defs, uses, unknown = [], [], []
    t = insn_type

    if t in sharcinv.COMPUTE_FORMS:
        field23 = f.get("compute")
        if field23:
            d2, u2, unk2 = _compute_regdef_reguse(field23)
            defs += d2
            uses += u2
            unknown += unk2
    elif t == "2c":
        field12 = f.get("compute")
        if field12 is not None:
            d2, u2 = _shortcompute_regdef_reguse(field12)
            defs += d2
            uses += u2

    if (
        t in sharcfn.DIRECT_MEM_FORMS
        or t in sharcfn.INDEXED_MEM_FORMS
        or t in sharcfn.IMMOFF_MEM_FORMS
        or t == "3c"
    ):
        d2, u2 = _mem_regdef_reguse(t, f)
        defs += d2
        uses += u2
    elif t in sharcfn.DUAL_MEM_FORMS:
        d2, u2 = _dual_mem_regdef_reguse(f)
        defs += d2
        uses += u2

    if t == "6a_mem":
        d3, u3, unk3 = _shiftimm_regdef_reguse(f)
        defs += d3
        uses += u3
        unknown += unk3

    if f.get("u") and f.get("i") is not None:
        # DAG post-modify: the memory forms above already record I%d as
        # used (the pre-modify base for the access); u=1 also writes it.
        i_reg = "I%d" % f["i"]
        defs.append((i_reg, "dag_modify"))
        uses.append(i_reg)

    if t in ("5a_move", "5b_move"):
        dst = f.get("dstureg")
        src_hi, src_lo = f.get("srcureghigh", 0), f.get("srcureglow", 0) & 3
        src = (src_hi << 2) | src_lo
        if dst is not None:
            defs.append((sharcfn.ureg_name(dst), "move"))
        uses.append(sharcfn.ureg_name(src))
    elif t in ("5a_swap", "5b_swap"):
        c, d = f.get("cdreg"), f.get("dreg")
        if c is not None and d is not None:
            rc, rd = "R%d" % c, "R%d" % d
            defs += [(rc, "swap"), (rd, "swap")]
            uses += [rc, rd]
    elif t in ("17a", "17b"):
        ureg = f.get("ureg")
        if ureg is not None:
            defs.append((sharcfn.ureg_name(ureg), "literal"))
    elif t == "18a":
        sreg = f.get("sreg", 0)
        code = sharcfn.UREG_CODES.get("USTAT1", 0) + sreg
        reg = sharcfn.ureg_name(code)
        bop = f.get("bop", 0)
        uses.append(reg)
        if bop not in (4, 5):
            # set/clear/toggle (and the unnamed bop=3) read-modify-write;
            # bit-test/xor-test (4/5) only read the register into BTF.
            defs.append((reg, "literal"))
    elif t in ("19a", "19a_scaled", "19a_bitrev"):
        bank = 8 if f.get("g") else 0
        src_low, dis_low = f.get("is", 0), f.get("idis", 0)
        src, dst = bank + src_low, bank + (src_low ^ dis_low)
        defs.append(("I%d" % dst, "dag_modify"))
        uses.append("I%d" % src)
    elif t == "7a":
        # PRM Type7a MODIFY (pp.13-46/13-48): "Ia = MODIFY(Ia,Mb)" -- the
        # same source-XOR-idis destination trick as Type19a above, but the
        # modify delta is an M register (not a literal), so it is also a
        # use, like 16a/16b's mandatory I+=M below. Previously unmodelled
        # entirely: Type7a's compute half (COMPUTE_FORMS, above) recorded
        # register effects, but this instruction's own I-register write
        # never did, at any decode_table.json field, so the DAG modify a
        # Type7a always performs was invisible to `regdef`/`reguse`.
        bank = 8 if f.get("g") else 0
        src_low, dis_low = f.get("is", 0), f.get("idis", 0)
        src, dst = bank + src_low, bank + (src_low ^ dis_low)
        defs.append(("I%d" % dst, "dag_modify"))
        uses += ["I%d" % src, "M%d" % (bank + f.get("m", 0))]
    elif t in ("16a", "16b"):
        i, m = f.get("i", 0), f.get("m", 0)
        defs.append(("I%d" % i, "dag_modify"))
        uses += ["I%d" % i, "M%d" % m]
    elif t == "12a_imm":
        defs.append(("LCNTR", "literal"))
    elif t == "12a_ureg":
        ureg = f.get("ureg")
        defs.append(("LCNTR", "move"))
        if ureg is not None:
            uses.append(sharcfn.ureg_name(ureg))

    seen_def, uniq_defs = set(), []
    for reg, kind in defs:
        if (reg, kind) not in seen_def:
            seen_def.add((reg, kind))
            uniq_defs.append((reg, kind))
    uniq_uses = list(dict.fromkeys(uses))
    return uniq_defs, uniq_uses, unknown
