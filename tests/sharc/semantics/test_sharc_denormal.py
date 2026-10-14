"""Denormal behaviour of the SHARC+ float units (docs/findings/15, "Denormal
flush").

Rules under test (page numbers are PDF pages of out/refs/sharc-plus-prm,
"PRM", and of the ADSP-2106x user's manual, "2106x"):

- A denormal INPUT to a computational unit is read as a zero of the same
  sign (PRM p.100; 2106x p.45; per-op "Denormal inputs are flushed to
  +-zero", PRM pp.453-473).
- A post-rounded denormal RESULT is returned as a zero of the same sign
  (PRM pp.453-456, 461; 2106x p.45), and the ALU's AZ is set for it.
- The multiplier sets MU for a nonzero product that underflows, never for
  a denormal operand (PRM p.571, p.500).
- A data move is not a computation: denormal bits pass unchanged.
- PEy computes the same on the S register file.
"""

from support.sharc_trace_build import f32

from tools.sharc_dev import sharc_trace as st
from tools.sharc_dev.sharc_core.encoding import (
    AI_BIT,
    AN_BIT,
    AS_BIT,
    AV_BIT,
    AZ_BIT,
    MI_BIT,
    MN_BIT,
    MU_BIT,
    MV_BIT,
)
from tools.sharc_dev.sharc_disasm import Instruction
from tools.sharc_dev.sharcldr import LoadedMemory

U = st.UREG_CODES
SIMD = st.Const(1 << 21)
R, S = 0, 80

# Float ALU opcodes (cu=0) and the float multiply (cu=1, 0x30).
ADD, SUB, AVG, CMP, PASS, NEG = 0x81, 0x82, 0x89, 0x8A, 0xA1, 0xA2
ABS, SCALB, FIX, TRUNC, MIN, MAX = 0xB0, 0xBD, 0xC9, 0xCD, 0xE1, 0xE2
COPYSIGN, CLIP, RND, MANT, LOGB = 0xE0, 0xE3, 0xA5, 0xAD, 0xC1
MUL = (1, 0x30)

POS_DENORMAL = 0x00000001
NEG_DENORMAL = 0x80000001
BIG_DENORMAL = 0x007FFFFF  # the largest denormal
MIN_NORMAL = 0x00800000
ONE = 0x3F800000
HALF = 0x3F000000


def make(simd=False, **regs):
    uregs = {
        U["MODE1"]: SIMD if simd else st.Const(0),
        U["ASTATX"]: st.Const(0),
        U["ASTATY"]: st.Const(0),
        U["STKYX"]: st.Const(0),
        U["STKYY"]: st.Const(0),
    }
    for name, value in regs.items():
        uregs[U[name]] = st.Const(value)
    return st.State(0, uregs, concrete=LoadedMemory.from_stream(b""), assume_nw32=True)


def compute(state, opcode, rn=2, rx=0, ry=1, cu=0):
    field = (cu << 20) | (opcode << 12) | (rn << 8) | (rx << 4) | ry
    fields = {"compute[22:16]": field >> 16, "compute[15:0]": field & 0xFFFF}
    [out] = st._execute(state, Instruction(0, 4, "2a_short", fields, kind="confident"))
    assert out.stopped is None, out.stopped
    return out


def run(opcode, x, y=0, cu=0, base=R, simd=False):
    """F2 = op(F0, F1) with X, Y in the register file BASE (R or S)."""
    state = make(simd=simd)
    state.uregs[base + 0] = st.Const(x)
    state.uregs[base + 1] = st.Const(y)
    return compute(state, opcode, cu=cu)


def bit(out, name, position):
    return bool(out.uregs[U[name]].value >> position & 1)


def result(out, base=R):
    return out.uregs[base + 2].value


# -- inputs -----------------------------------------------------------------


def test_add_reads_denormal_inputs_as_zero():
    # PRM p.453: "Denormal inputs are flushed to +-zero".
    out = run(ADD, POS_DENORMAL, ONE)
    assert result(out) == ONE
    out = run(ADD, BIG_DENORMAL, NEG_DENORMAL)
    assert result(out) == 0  # (+0) + (-0)
    assert bit(out, "ASTATX", AZ_BIT)


def test_subtract_and_average_read_denormal_inputs_as_zero():
    assert result(run(SUB, ONE, POS_DENORMAL)) == ONE
    assert result(run(SUB, POS_DENORMAL, ONE)) == f32(-1.0)
    assert result(run(AVG, POS_DENORMAL, f32(2.0))) == ONE


def test_denormal_input_sign_is_kept_on_the_zero():
    # -0 + -0 = -0, +0 + -0 = +0 (round to nearest).
    out = run(ADD, NEG_DENORMAL, 0x80000000)
    assert result(out) == 0x80000000
    assert bit(out, "ASTATX", AN_BIT) and bit(out, "ASTATX", AZ_BIT)
    out = run(ADD, POS_DENORMAL, 0x80000000)
    assert result(out) == 0
    assert not bit(out, "ASTATX", AN_BIT)


def test_compare_treats_denormals_as_zero():
    # Two different denormals are equal; a denormal equals zero and is not
    # less than a positive denormal.
    out = run(CMP, POS_DENORMAL, BIG_DENORMAL)
    assert bit(out, "ASTATX", AZ_BIT) and not bit(out, "ASTATX", AN_BIT)
    out = run(CMP, NEG_DENORMAL, 0)
    assert bit(out, "ASTATX", AZ_BIT)
    out = run(CMP, NEG_DENORMAL, POS_DENORMAL)
    assert bit(out, "ASTATX", AZ_BIT)
    out = run(CMP, POS_DENORMAL, ONE)
    assert bit(out, "ASTATX", AN_BIT) and not bit(out, "ASTATX", AZ_BIT)


def test_pass_neg_abs_of_denormal():
    # pass/neg: "Denormal inputs are flushed to +-zero"; abs: to +zero
    # (PRM pp.458-460). AS of abs is the input's sign (p.459).
    assert result(run(PASS, POS_DENORMAL)) == 0
    assert result(run(PASS, NEG_DENORMAL)) == 0x80000000
    assert result(run(NEG, POS_DENORMAL)) == 0x80000000
    assert result(run(NEG, NEG_DENORMAL)) == 0
    out = run(ABS, NEG_DENORMAL)
    assert result(out) == 0
    assert bit(out, "ASTATX", AZ_BIT) and bit(out, "ASTATX", AS_BIT)


def test_min_max_clip_copysign_rnd_read_denormals_as_zero():
    assert result(run(MIN, POS_DENORMAL, f32(-1.0))) == f32(-1.0)
    assert result(run(MAX, POS_DENORMAL, f32(-1.0))) == 0
    # min(+0, -0) is -0, max(+0, -0) is +0 (PRM pp.472-473).
    assert result(run(MIN, POS_DENORMAL, NEG_DENORMAL)) == 0x80000000
    assert result(run(MAX, NEG_DENORMAL, POS_DENORMAL)) == 0
    assert result(run(CLIP, POS_DENORMAL, ONE)) == 0
    assert result(run(COPYSIGN, BIG_DENORMAL, f32(-1.0))) == 0x80000000
    assert result(run(RND, NEG_DENORMAL)) == 0x80000000


def test_scalb_flushes_denormal_input_and_result():
    # 2**-127 (denormal) scaled up is still a flushed zero.
    state = make()
    state.uregs[0] = st.Const(BIG_DENORMAL)
    state.uregs[1] = st.Const(10)
    assert result(compute(state, SCALB)) == 0
    # A normal scaled below 2**-126 returns zero (PRM p.461).
    state = make()
    state.uregs[0] = st.Const(MIN_NORMAL)
    state.uregs[1] = st.Const(-1 & 0xFFFFFFFF)
    out = compute(state, SCALB)
    assert result(out) == 0 and bit(out, "ASTATX", AZ_BIT)


def test_float_to_fixed_of_denormal_is_zero():
    for opcode in (FIX, TRUNC):
        out = run(opcode, NEG_DENORMAL)
        assert result(out) == 0 and bit(out, "ASTATX", AZ_BIT)


def test_mant_and_logb_of_denormal_treat_it_as_zero():
    out = run(MANT, BIG_DENORMAL)
    assert result(out) == 0 and bit(out, "ASTATX", AZ_BIT)
    # logb(+-0) is -infinity with AV (PRM p.462).
    out = run(LOGB, BIG_DENORMAL)
    assert result(out) == 0xFF800000 and bit(out, "ASTATX", AV_BIT)


def test_multiply_reads_denormal_operands_as_zero_without_underflow():
    out = run(0x30, BIG_DENORMAL, f32(2.0), cu=1)
    assert result(out) == 0
    assert not bit(out, "ASTATX", MU_BIT)  # "never cause underflows"
    assert not bit(out, "ASTATX", MV_BIT)
    # Sign of the zero is the sign of the product.
    out = run(0x30, NEG_DENORMAL, f32(2.0), cu=1)
    assert result(out) == 0x80000000 and bit(out, "ASTATX", MN_BIT)
    assert not bit(out, "ASTATX", MU_BIT)


def test_multiply_infinity_times_denormal_is_invalid():
    # The denormal is a zero, so this is infinity times zero (PRM p.500 MI).
    out = run(0x30, 0x7F800000, POS_DENORMAL, cu=1)
    assert bit(out, "ASTATX", MI_BIT)


def test_add_infinity_with_denormal_is_infinity():
    assert result(run(ADD, 0x7F800000, POS_DENORMAL)) == 0x7F800000


# -- results ----------------------------------------------------------------


def test_add_underflowing_result_flushes_with_sign_and_az():
    # 1.5 * 2**-126 - 2**-126 = 2**-127, a denormal: returned as +zero.
    out = run(SUB, 0x00C00000, MIN_NORMAL)
    assert result(out) == 0
    assert bit(out, "ASTATX", AZ_BIT) and not bit(out, "ASTATX", AN_BIT)
    # The negated pair gives a negative result: -zero, AN set.
    out = run(SUB, 0x80C00000, 0x80800000)
    assert result(out) == 0x80000000
    assert bit(out, "ASTATX", AZ_BIT) and bit(out, "ASTATX", AN_BIT)
    assert not bit(out, "ASTATX", AV_BIT) and not bit(out, "ASTATX", AI_BIT)


def test_post_rounded_boundary_of_the_flush():
    # The 24-bit value 2**-126 - 2**-150 is a denormal; 2**-126 - 2**-151 is
    # the tie that rounds up to 2**-126 (nearest even), which is normal.
    from tools.sharc_dev.sharc_core.floats import _float32_result

    exact = 2.0**-126 - 2.0**-150
    tie = 2.0**-126 - 2.0**-151
    assert _float32_result(exact) == (0, False, True)
    assert _float32_result(-exact) == (0x80000000, False, True)
    assert _float32_result(tie) == (MIN_NORMAL, False, False)
    assert _float32_result(2.0**-126) == (MIN_NORMAL, False, False)
    assert _float32_result(0.0) == (0, False, False)
    assert _float32_result(-0.0) == (0x80000000, False, False)
    # Far below the denormal range: still a zero, still an underflow.
    assert _float32_result(2.0**-200) == (0, False, True)


def test_multiply_underflow_flushes_and_sets_mu_and_mus():
    # 2**-100 * 2**-100 = 2**-200: zero, MU and the sticky MUS set.
    tiny = f32(2.0**-100)
    out = run(0x30, tiny, tiny, cu=1)
    assert result(out) == 0
    assert bit(out, "ASTATX", MU_BIT) and not bit(out, "ASTATX", MV_BIT)
    assert bit(out, "STKYX", MU_BIT)
    out = run(0x30, tiny, f32(-(2.0**-100)), cu=1)
    assert result(out) == 0x80000000 and bit(out, "ASTATX", MN_BIT)
    assert bit(out, "ASTATX", MU_BIT)


def test_multiply_denormal_product_is_flushed_with_mu():
    # 2**-70 * 2**-70 = 2**-140 is a denormal product.
    out = run(0x30, f32(2.0**-70), f32(2.0**-70), cu=1)
    assert result(out) == 0
    assert bit(out, "ASTATX", MU_BIT) and bit(out, "STKYX", MU_BIT)


def test_multiply_exact_zero_is_not_an_underflow():
    out = run(0x30, 0, f32(2.0), cu=1)
    assert result(out) == 0 and not bit(out, "ASTATX", MU_BIT)
    assert not bit(out, "STKYX", MU_BIT)


def test_multiply_normal_result_clears_mu_but_not_the_sticky():
    state = make()
    state.uregs[0] = st.Const(f32(2.0**-100))
    state.uregs[1] = st.Const(f32(2.0**-100))
    out = compute(state, 0x30, cu=1)
    assert bit(out, "STKYX", MU_BIT)
    out.uregs[0] = st.Const(ONE)
    out.uregs[1] = st.Const(ONE)
    out = compute(out, 0x30, cu=1)
    assert result(out) == ONE
    assert not bit(out, "ASTATX", MU_BIT)
    assert bit(out, "STKYX", MU_BIT)  # sticky: set, never cleared by an op


def test_filter_state_decays_to_zero():
    # The late-run case: x * g with g > 0.5 sticks at a 1-ulp denormal in
    # plain IEEE and reaches zero when denormals flush.
    state = make()
    value = f32(2.0**-120)
    gain = f32(0.75)
    for _ in range(200):
        state.uregs[0] = st.Const(value)
        state.uregs[1] = st.Const(gain)
        state = compute(state, 0x30, cu=1)
        value = result(state)
    assert value == 0


# -- data moves -------------------------------------------------------------


def test_register_move_keeps_denormal_bits():
    # R3 = R0 (Type 5b) is a transfer, not a computation.
    state = make()
    state.uregs[R + 0] = st.Const(NEG_DENORMAL)
    fields = {
        "cond[4:0]": 31,
        "srcureghigh[4:0]": 0,
        "srcureglow[1:1]": 0,
        "srcureglow[0:0]": 0,
        "dstureg[6:0]": 3,
    }
    [out] = st._execute(state, Instruction(0, 4, "5b_move", fields, kind="confident"))
    assert out.stopped is None
    assert out.uregs[3] == st.Const(NEG_DENORMAL)


# -- SIMD -------------------------------------------------------------------


def test_simd_pey_flushes_the_same_way():
    state = make(simd=True)
    state.uregs[R + 0] = st.Const(POS_DENORMAL)
    state.uregs[R + 1] = st.Const(ONE)
    state.uregs[S + 0] = st.Const(NEG_DENORMAL)
    state.uregs[S + 1] = st.Const(0x80000000)
    out = compute(state, ADD)
    assert result(out, R) == ONE
    assert result(out, S) == 0x80000000  # -0 + -0
    assert bit(out, "ASTATY", AZ_BIT) and bit(out, "ASTATY", AN_BIT)
    assert not bit(out, "ASTATX", AZ_BIT)


def test_simd_pey_multiply_underflow_sets_muy():
    state = make(simd=True)
    state.uregs[R + 0] = st.Const(ONE)
    state.uregs[R + 1] = st.Const(ONE)
    tiny = f32(2.0**-100)
    state.uregs[S + 0] = st.Const(tiny)
    state.uregs[S + 1] = st.Const(tiny)
    out = compute(state, 0x30, cu=1)
    assert result(out, R) == ONE and result(out, S) == 0
    assert bit(out, "ASTATY", MU_BIT) and bit(out, "STKYY", MU_BIT)
    assert not bit(out, "ASTATX", MU_BIT) and not bit(out, "STKYX", MU_BIT)
