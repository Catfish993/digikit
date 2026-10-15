"""Spot checks of tools/sharc_dev/sharc_timing.py against the stall tables of the SHARC+ Core Programming Reference."""

from tools.sharc_dev import sharc_timing as t


def ins(
    pc,
    text,
    defs=(),
    uses=(),
    mem=(),
    branch="",
    delayed=False,
    always=True,
    nbytes=6,
    **kw,
):
    return t.Ins(
        pc=pc, nbytes=nbytes, text=text, defs=tuple(defs), uses=frozenset(uses), mem=tuple(mem), branch=branch,
        delayed=delayed, always=always, **kw,
    )  # fmt: skip


def test_dag_load_to_use():
    # Table 4-38 row 1: `Ix = DM(..); DM(Ix..)` stalls 4; row 2 (condition set before a conditional load) 5.
    load = ins(0x100, "I4 = DM(I1, M1)", defs=[("I4", "mem_load")])
    use = ins(
        0x103,
        "R0 = DM(I4, M6)",
        defs=[("R0", "mem_load")],
        uses=["I4", "M6"],
        mem=[("DM", "I4")],
    )
    assert t.dag_use(load, use) == (4.0, 4.0, 4.0)
    cond = ins(0x100, "IF EQ I4 = DM(I1, M1)", defs=[("I4", "mem_load")], always=False)
    assert t.dag_use(cond, use)[1] == 5.0
    # Row 4: an immediate load costs nothing.
    imm = ins(0x100, "I4 = 0x240000", defs=[("I4", "literal")])
    assert t.dag_use(imm, use) == t.ZERO
    # The transfer from a data register is not in the table: low 0, nominal 4.
    mv = ins(0x100, "I4 = R2", defs=[("I4", "move")])
    assert t.dag_use(mv, use) == (0.0, 4.0, 4.0)
    # Not used as an address: no stall.
    other = ins(0x103, "R0 = R1 + R2", defs=[("R0", "compute")], uses=["R1", "R2"])
    assert t.dag_use(load, other) == t.ZERO


def test_float_forward_and_move():
    # Table 4-36 row 1: a float compute followed by a dependent compute stalls 1.
    a = ins(0x100, "F1 = F2 + F3", defs=[("R1", "compute")], uses=["R2", "R3"])
    b = ins(0x103, "F4 = F1 * F5", defs=[("R4", "compute")], uses=["R1", "R5"])
    assert t.float_forward(a, b) == (0.0, 1.0, 1.0)
    fixed = ins(0x100, "R1 = R2 + R3", defs=[("R1", "compute")], uses=["R2", "R3"])
    assert t.float_forward(fixed, b) == t.ZERO
    c = ins(0x103, "F4 = F6 * F5", defs=[("R4", "compute")], uses=["R6", "R5"])
    assert t.float_forward(a, c) == t.ZERO
    # Table 4-43 row 1: a float result moved to a register outside the PE.
    mv = ins(0x103, "I0 = R1", defs=[("I0", "move")], uses=["R1"])
    assert t.float_to_move(a, mv) == (1.0, 1.0, 1.0)


def test_branch_costs_from_simulated_outcomes():
    # r = [executions, taken, hit & predicted taken & taken, hit & not taken & predicted not taken,
    #      hit & predicted taken & not taken, hit & predicted not taken & taken, miss & taken, miss & not taken]
    # Table 4-4: a delayed unconditional branch that hits the BTB costs 0, a non-delayed one 2; without the BTB 4 / 6.
    j = ins(0x100, "JUMP delayed", branch="jump", delayed=True)
    assert t.branch_cost_sim(j, [10, 10, 10, 0, 0, 0, 0, 0]) == (0, 0, 40)
    j2 = ins(0x100, "JUMP", branch="jump")
    assert t.branch_cost_sim(j2, [10, 10, 7, 0, 0, 0, 3, 0]) == (20, 14 + 18, 60)
    c = ins(0x100, "IF EQ JUMP", branch="cond_jump", always=False)
    # Never taken: free with and without the BTB.
    assert t.branch_cost_sim(c, [10, 0, 0, 10, 0, 0, 0, 0]) == (0, 0, 0)
    # Taken, predicted taken, hit: 2 each; mispredicted either way: 11 (non-delayed); a BTB miss on a taken branch: 11.
    assert t.branch_cost_sim(c, [10, 10, 10, 0, 0, 0, 0, 0]) == (20, 20, 110)
    assert t.branch_cost_sim(c, [4, 2, 1, 0, 1, 1, 1, 0]) == (4, 2 + 11 + 11 + 11, 22)
    # Delayed: a taken, predicted branch costs 0; predicted taken but not taken 11; predicted not taken but taken 9.
    cd = ins(
        0x100, "IF EQ JUMP delayed", branch="cond_jump", delayed=True, always=False
    )
    assert t.branch_cost_sim(cd, [3, 2, 1, 0, 1, 1, 0, 0]) == (0, 11 + 9, 18)


def test_loop_exit():
    counter = ins(
        0x100, "DO UNTIL LCE", branch="loop", loop_counter=True, loop_end=0x120
    )
    # A long counter loop without a change of flow in its tail runs F1-active: no cost. With one, E2-active: 11.
    assert t.loop_exit_cost(counter, 30, False, 8.0, 5) == (0.0, 0.0, 55.0)
    assert t.loop_exit_cost(counter, 30, True, 8.0, 5) == (0.0, 55.0, 55.0)
    # A loop that unrolls to fewer than eleven instructions always runs E2-active.
    assert t.loop_exit_cost(counter, 2, False, 3.0, 5)[1] == 55.0
    arith = ins(0x100, "DO UNTIL EQ", branch="loop")
    assert t.loop_exit_cost(arith, 30, False, 8.0, 5) == (55.0, 55.0, 55.0)


def test_regions():
    assert t.region_of_pc(0x1C4ECF) == "l1"
    assert t.region_of_pc(0xB80239) == "l2"
    assert t.region_of_pc(0x400000) == "other"


def test_check_fixes():
    # Table 8-1: the L1 instruction cache line is 512 bits.
    assert t.ICACHE_LINE == 64
    # Table 4-43 row 1 also counts a fixed-point multiplier result moved outside the PE.
    mul = ins(0x100, "R1 = R2 * R3 (SSI)", defs=[("R1", "compute")], uses=["R2", "R3"])
    mv = ins(0x103, "I0 = R1", defs=[("I0", "move")], uses=["R1"])
    assert t.float_to_move(mul, mv) == (1.0, 1.0, 1.0)
    add = ins(0x100, "R1 = R2 + R3", defs=[("R1", "compute")], uses=["R2", "R3"])
    assert t.float_to_move(add, mv) == t.ZERO
    # RTI and IDLE are recognised from the text, an undecoded 11a/11c return by its x bit.
    assert ins(0x100, "RTI (DB)").is_rti
    assert ins(0x100, "[11c] cond=0x1f j=0x1 lr=0x0 x=0x1").is_rti
    assert not ins(0x100, "[11c] cond=0x1f j=0x1 lr=0x0 x=0x0").is_rti
    assert ins(0x100, "IDLE").is_idle
    assert t.RTI_FLUSH == 7
    assert t.SAME_BLOCK == (1, 1, 2)
