"""Type 6 (ShiftImm) and BIT TST run in both processing elements in SIMD mode.

SHARC+ PRM p.3-39 (R5 = LSHIFT R6 BY <data8> with S5 = LSHIFT S6 BY <data8>
implied) and p.13-43 (Type 6a SIMD Mode: the shift and the memory transfer
run in PEx and PEy; a condition is evaluated per element; a conditional
jump needs both elements, p.4-54).  The DN2 delay-send bug: a SIMD `btst`
left ASTATY stale, so `JUMP IF SZ` (an AND of both elements) was not taken.
"""


from support.paths import REPO_ROOT

from tools.sharc_dev import sharc_trace as st
from tools.sharc_dev.sharc_core.encoding import AZ_BIT, BTF_BIT, SZ_BIT
from tools.sharc_dev.sharc_disasm import Instruction
from tools.sharc_dev.sharcldr import LoadedMemory

U = st.UREG_CODES
SIMD = st.Const(1 << 21)
R, S = 0, 80


def make(**regs):
    uregs = {
        U["MODE1"]: SIMD,
        U["ASTATX"]: st.Const(0),
        U["ASTATY"]: st.Const(0),
    }
    for name, value in regs.items():
        uregs[U[name]] = st.Const(value)
    return st.State(0, uregs, concrete=LoadedMemory.from_stream(b""), assume_nw32=True)


def run(state, form, fields, length=6):
    [out] = st._execute(state, Instruction(0, length, form, fields, kind="confident"))
    assert out.stopped is None, out.stopped
    return out


def bit(state, name, position):
    value = state.uregs[U[name]]
    return bool(value.value >> position & 1)


def shift6b(cond=31, *, opcode=0x00, data8=4, rn=7, rx=4):
    return {
        "cond[4:0]": cond,
        "dataex[3:0]": 0,
        "shiftimm[22:16]": opcode,
        "shiftimm[15:0]": (data8 << 8) | (rn << 4) | rx,
    }


def test_6b_lshift_writes_r_and_s():
    state = make()
    state.uregs[R + 4] = st.Const(0x11223344)
    state.uregs[S + 4] = st.Const(0x00000055)
    out = run(state, "6b_shiftimm", shift6b())
    assert out.uregs[R + 7] == st.Const(0x12233440)
    assert out.uregs[S + 7] == st.Const(0x00000550)


def test_6b_btst_sets_sz_in_astatx_and_astaty():
    # ShiftImm 0x33 is BTST Rx BY data8 (PRM Table 17-9); SZ = bit clear.
    state = make()
    state.uregs[R + 2] = st.Const(0x10)
    state.uregs[S + 2] = st.Const(0x10)
    out = run(state, "6b_shiftimm", shift6b(opcode=0x33, data8=0, rn=0, rx=2))
    assert bit(out, "ASTATX", SZ_BIT) and bit(out, "ASTATY", SZ_BIT)
    # Different PEy data gives a different PEy flag.
    state.uregs[S + 2] = st.Const(0x11)
    out = run(state, "6b_shiftimm", shift6b(opcode=0x33, data8=0, rn=0, rx=2))
    assert bit(out, "ASTATX", SZ_BIT) and not bit(out, "ASTATY", SZ_BIT)


def test_firmware_pattern_btst_then_jump_if_sz_is_taken():
    # 0xb84225 btst R2 bit 0; 0xb84228 JUMP IF SZ (R2 = S2 = 0x10).
    state = make()
    state.uregs[R + 2] = st.Const(0x10)
    state.uregs[S + 2] = st.Const(0x10)
    state = run(state, "6b_shiftimm", shift6b(opcode=0x33, data8=0, rn=0, rx=2))
    jump = {
        "addr[23:16]": 0,
        "addr[15:0]": 0x40,
        "cond[4:0]": 0x08,  # SZ
        "b": 0,
        "j": 0,
        "a": 0,
    }
    out = run(state, "8a_abs", jump)
    assert out.pc_sw == 0x40


def test_branch_not_taken_when_pey_disagrees():
    state = make()
    state.uregs[R + 2] = st.Const(0x10)
    state.uregs[S + 2] = st.Const(0x11)
    state = run(state, "6b_shiftimm", shift6b(opcode=0x33, data8=0, rn=0, rx=2))
    jump = {
        "addr[23:16]": 0,
        "addr[15:0]": 0x40,
        "cond[4:0]": 0x08,
        "b": 0,
        "j": 0,
        "a": 0,
    }
    assert run(state, "8a_abs", jump).pc_sw != 0x40


def test_6b_sisd_leaves_s_registers_alone():
    state = make()
    state.uregs[U["MODE1"]] = st.Const(0)
    state.uregs[R + 4] = st.Const(1)
    state.uregs[S + 4] = st.Const(2)
    state.uregs[S + 7] = st.Const(0xAA)
    out = run(state, "6b_shiftimm", shift6b())
    assert out.uregs[R + 7] == st.Const(0x10)
    assert out.uregs[S + 7] == st.Const(0xAA)


def test_6b_conditional_simd_is_per_element():
    # cond 0 is EQ (AZ): PEx true, PEy false -> only R7 changes.
    state = make()
    state.uregs[U["ASTATX"]] = st.Const(1 << AZ_BIT)
    state.uregs[R + 4] = st.Const(1)
    state.uregs[S + 4] = st.Const(2)
    state.uregs[R + 7] = st.Const(0xAA)
    state.uregs[S + 7] = st.Const(0xBB)
    out = run(state, "6b_shiftimm", shift6b(cond=0))
    assert out.uregs[R + 7] == st.Const(0x10)
    assert out.uregs[S + 7] == st.Const(0xBB)
    assert not bit(out, "ASTATY", AZ_BIT)
    # Both false: skipped, nothing changes.
    state = make()
    state.uregs[R + 4] = st.Const(1)
    state.uregs[R + 7] = st.Const(0xAA)
    state.uregs[S + 7] = st.Const(0xBB)
    out = run(state, "6b_shiftimm", shift6b(cond=0))
    assert out.uregs[R + 7] == st.Const(0xAA)
    assert out.uregs[S + 7] == st.Const(0xBB)


def fields_6a(*, store, cond=31, dreg=4, rn=7, rx=4, data8=4):
    return {
        "cond[4:0]": cond,
        "g": 0,
        "i[2:0]": 0,
        "m[2:0]": 4,
        "d": int(store),
        "dreg[3:0]": dreg,
        "dataex[3:0]": 0,
        "shiftimm[22:16]": 0,
        "shiftimm[15:0]": (data8 << 8) | (rn << 4) | rx,
    }


def test_6a_simd_store_shift_and_companion_transfer():
    # PRM p.13-43: PEx stores R4 at Ia and shifts R4; PEy stores S4 at Ia+1
    # (one normal word; four bytes in byte space) and shifts S4.  Only the
    # explicit I register post-modifies.
    state = make(I0=0x80000000, M4=1, R4=0x11, MODE1=1 << 21)
    state.uregs[S + 4] = st.Const(0x22)
    out = run(state, "6a_mem", fields_6a(store=True))
    assert out.overlay[0x80000000] == 0x11
    assert out.overlay[0x80000004] == 0x22
    assert out.uregs[U["I0"]] == st.Const(0x80000004)
    assert out.uregs[R + 7] == st.Const(0x110)
    assert out.uregs[S + 7] == st.Const(0x220)


def test_6a_simd_load_writes_companion_register():
    state = make(I0=0x80000000, M4=1)
    state.overlay.update(
        {0x80000004 + i: b for i, b in enumerate((0x78, 0x56, 0x34, 0x12))}
    )
    state.overlay.update(
        {0x80000000 + i: b for i, b in enumerate((0x04, 0x03, 0x02, 0x01))}
    )
    out = run(state, "6a_mem", fields_6a(store=False, dreg=3))
    assert out.uregs[R + 3] == st.Const(0x01020304)
    assert out.uregs[S + 3] == st.Const(0x12345678)


def test_6a_conditional_simd_both_true_and_both_false():
    state = make(I0=0x80000000, M4=1, R4=0x11, ASTATX=1 << AZ_BIT, ASTATY=1 << AZ_BIT)
    state.uregs[S + 4] = st.Const(0x22)
    out = run(state, "6a_mem", fields_6a(store=True, cond=0))
    assert out.overlay[0x80000004] == 0x22
    state = make(I0=0x80000000, M4=1, R4=0x11)
    out = run(state, "6a_mem", fields_6a(store=True, cond=0))
    assert out.overlay == {}
    assert out.uregs[U["I0"]] == st.Const(0x80000000)


def test_6a_conditional_simd_mixed_outcome_stops():
    state = make(I0=0x80000000, M4=1, R4=0x11, ASTATX=1 << AZ_BIT)
    out = st._execute(
        state,
        Instruction(0, 6, "6a_mem", fields_6a(store=True, cond=0), kind="confident"),
    )[0]
    assert out.stopped == "mixed-PE conditional SIMD Type6a"


def test_18a_bit_test_ustat_sets_btf_per_element():
    # PRM p.4-55 "Bit Test Flag in SIMD Mode": BIT TST USTAT1 tests
    # USTAT1 in PEx and USTAT2 in PEy.
    state = make(USTAT1=0x80000000, USTAT2=0)
    fields = {
        "bop": 4,
        "sreg": 0,
        "data[31:16]": 0x8000,
        "data[15:0]": 0,
    }
    out = run(state, "18a", fields)
    assert bit(out, "ASTATX", BTF_BIT) and not bit(out, "ASTATY", BTF_BIT)
