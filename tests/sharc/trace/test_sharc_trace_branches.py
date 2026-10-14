"""Symbolic tracer: jumps, calls, delay slots."""

import unittest
from importlib import import_module

T = import_module("tools.sharc_dev.sharc_trace")
Instruction = import_module("tools.sharc_dev.sharc_disasm").Instruction
L = import_module("tools.sharc_dev.sharcldr")
from support.sharc_trace_build import insn, loader_block, loader_memory  # noqa: E402

_floats = import_module(
    "tools.sharc_dev.sharc_core.floats"
)  # _double_to_fixed isn't re-exported by T

from support.sharc_trace_build import TraceRunMixin  # noqa: E402


class TraceBranchFormsTest(TraceRunMixin, unittest.TestCase):
    """Jumps, calls and type3a/9b/2a forms."""

    def _type9a_abs_fields(self, **changes):
        fields = {
            "b": 0,
            "a": 0,
            "cond[4:0]": 31,
            "pmi[2:2]": 1,
            "pmi[1:0]": 0,
            "pmm[2:0]": 5,
            "j": 0,
            "e": 0,
            "ci": 0,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        fields.update(changes)
        return fields

    def test_type8a_loop_abort_only_pops_stacks_when_taken(self):
        for delayed in (0, 1):
            for taken in (False, True):
                with self.subTest(delayed=delayed, taken=taken):
                    state = T.State(
                        10,
                        {
                            T.UREG_CODES["MODE1"]: T.Const(0),
                            T.UREG_CODES["ASTATX"]: T.Const((1 << 11) if taken else 0),
                        },
                        call_stack=[0x99, 0x13],
                        loops=[T.Loop(0x13, 0x20, 2, 1)],
                    )
                    branch = insn(
                        "8a_rel",
                        {
                            "a": 1,
                            "b": 0,
                            "j": delayed,
                            "cond[4:0]": 7,
                            "reladdr[23:16]": 0,
                            "reladdr[15:0]": 30,
                        },
                        6,
                    )
                    advanced = self.run_one(state, branch)
                    self.assertEqual(len(advanced.loops), 0 if taken else 1)
                    self.assertEqual(
                        advanced.call_stack, [0x99] if taken else [0x99, 0x13]
                    )
                    self.assertEqual(advanced.pc_sw, 13 if delayed or not taken else 40)

    def test_type9a_abs_rejects_ci_modifier(self):
        # PGR p.9-36/PRM p.4-45: (CI) clears the currently-serviced
        # interrupt's IRPTL/IMASKP bit; this tracer does not track which
        # interrupt (if any) is active, so there is no sound bit to clear
        # and this must fail closed rather than guess.
        stopped = self.run_one(
            T.State(10), insn("9a_abs", self._type9a_abs_fields(ci=1), 6)
        )
        self.assertEqual(stopped.stopped, "unsupported Type9a CI modifier")

    def test_type9a_abs_la_pops_loop_and_pc_stacks_once(self):
        # SHARC+ Core Programming Reference p.4-44 ("Loop Abort"): JUMP
        # (LA) "pops the PC and loop address stacks" -- "only one pop is
        # performed" -- when the jump executes. call_stack holds both the
        # loop's own PC-stack entry (0x13, pushed by the loop setup) and
        # an unrelated call return address on top (0x99); LA pops each
        # stack independently and exactly once (Type20a's LPO+PPO do the
        # same single-level pop, reused here -- see
        # sequencer._apply_loop_abort).
        state = T.State(
            10,
            {T.UREG_CODES["I12"]: T.Const(0x2000), T.UREG_CODES["M13"]: T.Const(4)},
            call_stack=[0x13, 0x99],
            loops=[T.Loop(0x13, 0x20, 2, 1)],
        )
        advanced = self.run_one(state, insn("9a_abs", self._type9a_abs_fields(a=1), 6))
        self.assertEqual(advanced.pc_sw, 0x2004)
        self.assertEqual(advanced.loops, [])
        self.assertEqual(advanced.call_stack, [0x13])

    def test_type9a_abs_stops_on_unknown_indirect_target(self):
        stopped = self.run_one(
            T.State(10), insn("9a_abs", self._type9a_abs_fields(), 6)
        )
        self.assertEqual(
            stopped.stopped, "unknown 9a_abs indirect target through I12/M13"
        )

    def test_type9a_abs_immediate_jump_with_known_registers(self):
        state = T.State(
            10, {T.UREG_CODES["I12"]: T.Const(0x2000), T.UREG_CODES["M13"]: T.Const(4)}
        )
        advanced = self.run_one(state, insn("9a_abs", self._type9a_abs_fields(), 6))
        self.assertEqual(advanced.pc_sw, 0x2004)
        self.assertIsNone(advanced.pending)

    def test_type9a_abs_delayed_jump_with_compute(self):
        fields = self._type9a_abs_fields(
            **{"pmm[2:0]": 7, "j": 1, "compute[22:16]": 0x02, "compute[15:0]": 0x9220}
        )
        state = T.State(
            10,
            {
                T.UREG_CODES["I12"]: T.Const(0x30000),
                T.UREG_CODES["M15"]: T.Const(0),
                T.UREG_CODES["R2"]: T.Const(4),
            },
        )
        advanced = self.run_one(state, insn("9a_abs", fields, 6))
        self.assertEqual(advanced.uregs[T.UREG_CODES["R2"]], T.Const(5))
        self.assertEqual(advanced.pending.target, 0x30000)

    def test_type9a_abs_i4_m6_delayed_is_return_with_compute(self):
        fields = self._type9a_abs_fields(
            **{"pmm[2:0]": 6, "j": 1, "compute[22:16]": 0x02, "compute[15:0]": 0x9220}
        )
        state = T.State(0x10, {T.UREG_CODES["R2"]: T.Const(4)}, call_stack=[0x200])
        returned = self.run_one(state, insn("9a_abs", fields, 6))
        self.assertEqual(returned.uregs[T.UREG_CODES["R2"]], T.Const(5))
        self.assertTrue(returned.pending.return_from_call)
        stopped = self.run_one(T.State(0x10), insn("9a_abs", fields, 6))
        self.assertEqual(stopped.stopped, "return without followed call")

    def test_delayed_call_returns_after_variable_width_delay_slots(self):
        for push_length, expected in ((2, 17), (6, 19)):
            with self.subTest(push_length=push_length):
                state = T._transfer(
                    T.State(10), insn("25a_direct", {}, 6), 99, True, True
                )[0]
                state = T._advance(state, insn("3a", {}, push_length))[0]
                state = T._advance(state, insn("16a", {}, 6))[0]
                self.assertEqual(state.stopped, "external-call")
                self.assertEqual(state.trace[-1]["return_sw"], expected)

    def test_dedupe_key_ignores_history_but_not_future_state(self):
        a = T.State(5, {1: T.Const(2)}, steps=3)
        b = T.State(5, {1: T.Const(2)}, steps=9)
        b.trace.append({"action": "compute"})
        self.assertEqual(T._dedupe_key(a), T._dedupe_key(b))
        c = T.State(5, {1: T.Const(2)}, at_loaded_entry=True)
        self.assertNotEqual(T._dedupe_key(a), T._dedupe_key(c))
        d = T.State(5, {1: T.Const(3)})
        self.assertNotEqual(T._dedupe_key(a), T._dedupe_key(d))

    def test_return_idiom_checks_known_i12_m14_target(self):
        fields = {
            "b": 0,
            "a": 0,
            "cond[4:0]": 0x1F,
            "pmi[2:2]": 1,
            "pmi[1:0]": 0,
            "pmm[2:0]": 6,
            "j": 1,
        }
        regs = {T.UREG_CODES["I12"]: T.Const(0x1FF), T.UREG_CODES["M14"]: T.Const(1)}
        ok = self.run_one(
            T.State(0x10, dict(regs), call_stack=[0x200]), insn("9b_abs", fields)
        )
        self.assertIsNone(ok.stopped)
        self.assertTrue(ok.pending.return_from_call)
        regs[T.UREG_CODES["I12"]] = T.Const(0x2FF)
        bad = self.run_one(
            T.State(0x10, regs, call_stack=[0x200]), insn("9b_abs", fields)
        )
        self.assertEqual(
            bad.stopped, "return target 0x300 differs from recorded return 0x200"
        )

    def test_type3a_post_modify_scales_normal_word_modifier(self):
        fields = {
            "u": 1,
            "i": 7,
            "m": 7,
            "g": 0,
            "d": 1,
            "l": 0,
            "ureg": 2,
            "cond": 31,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        state = T.State(
            1,
            {
                T.UREG_CODES["I7"]: T.Const(0x1000),
                T.UREG_CODES["M7"]: T.Const(0xFFFFFFFF),
                2: T.Const(5),
            },
            assume_nw32=True,
        )
        stored = self.run_one(state, insn("3a", fields, 6))
        self.assertEqual(stored.uregs[T.UREG_CODES["I7"]], T.Const(0x1000 - 4))

    def _type3a_fields(self, cond):
        return {
            "u": 1,
            "i": 0,
            "m": 0,
            "cond": cond,
            "g": 0,
            "d": 1,
            "l": 0,
            "ureg": 2,
            "compute": 0,
        }

    def test_type3a_predicate_false_skips_transfer_and_compute(self):
        astatx_code = T.UREG_CODES["ASTATX"]
        memory = loader_memory(loader_block(1, 0x80, 4, payload=b"\0" * 4))
        state = T.State(
            1,
            {
                16: T.Const(0x80),
                32: T.Const(1),
                2: T.Const(0xAABBCCDD),
                astatx_code: T.Const(0),  # AC clear -> cond 0x03 (AC) is False
            },
            concrete=memory,
            assume_nw32=True,
        )
        result = self.run_one(state, insn("3a", self._type3a_fields(0x03), 6))
        self.assertEqual(result.pc_sw, 4)  # PC still advances on a skip
        self.assertEqual(result.uregs[16], T.Const(0x80))  # no post-modify
        self.assertEqual(T._dm_read(result, 0x80, 4), T.Const(0))  # store never ran
        self.assertEqual(
            (result.trace[-1]["action"], result.trace[-1]["predicate_assumption"]),
            ("type3a-skipped", False),
        )

    def test_type3a_predicate_true_performs_transfer_and_annotates_trace(self):
        astatx_code = T.UREG_CODES["ASTATX"]
        memory = loader_memory(loader_block(1, 0x80, 4, payload=b"\0" * 4))
        state = T.State(
            1,
            {
                16: T.Const(0x80),
                32: T.Const(1),
                2: T.Const(0xAABBCCDD),
                astatx_code: T.Const(1 << T.AC_BIT),  # AC set -> cond 0x03 True
            },
            concrete=memory,
            assume_nw32=True,
        )
        result = self.run_one(state, insn("3a", self._type3a_fields(0x03), 6))
        self.assertTrue(result.trace[-1]["concrete_write"])
        self.assertEqual(T._dm_read(result, 0x80, 4), T.Const(0xAABBCCDD))
        self.assertEqual(result.uregs[16], T.Const(0x84))
        self.assertEqual(
            (result.trace[-1]["condition"], result.trace[-1]["predicate_assumption"]),
            (0x03, True),
        )

    def test_type3a_unknown_predicate_forks_execute_and_skip(self):
        memory = loader_memory(loader_block(1, 0x80, 4, payload=b"\0" * 4))
        state = T.State(
            1,
            {16: T.Const(0x80), 32: T.Const(1), 2: T.Const(0xAABBCCDD)},
            concrete=memory,
            assume_nw32=True,
        )
        executed, skipped = T._execute(state, insn("3a", self._type3a_fields(0x03), 6))
        self.assertEqual((executed.pc_sw, skipped.pc_sw), (4, 4))
        self.assertEqual(executed.uregs[16], T.Const(0x84))
        self.assertEqual(skipped.uregs[16], T.Const(0x80))
        self.assertEqual(T._dm_read(executed, 0x80, 4), T.Const(0xAABBCCDD))
        self.assertEqual(T._dm_read(skipped, 0x80, 4), T.Const(0))
        self.assertEqual(
            (
                executed.trace[-1]["condition"],
                executed.trace[-1]["predicate_assumption"],
            ),
            (0x03, True),
        )
        self.assertEqual(
            (skipped.trace[-1]["action"], skipped.trace[-1]["predicate_assumption"]),
            ("type3a-skipped", False),
        )

    def test_type9b_abs_indirect_jump_uses_dag2_registers(self):
        fields = {
            "b": 0,
            "a": 0,
            "cond[4:0]": 0x1F,
            "pmi[2:2]": 1,
            "pmi[1:0]": 1,
            "pmm[2:0]": 5,
            "j": 1,
            "ci": 0,
        }
        state = T.State(
            0x10, {T.UREG_CODES["I13"]: T.Const(0x300), T.UREG_CODES["M13"]: T.Const(0)}
        )
        jumped = self.run_one(state, insn("9b_abs", fields))
        self.assertEqual(jumped.pending.target, 0x300)
        stopped = self.run_one(T.State(0x10), insn("9b_abs", fields))
        self.assertEqual(
            stopped.stopped, "unknown 9b_abs indirect target through I13/M13"
        )

    def test_full_compute_negate(self):
        state = self.run_one(
            T.State(1, {2: T.Const(5), T.UREG_CODES["ASTATX"]: T.Const(0)}),
            insn("2a_short", {"compute[22:16]": 0x02, "compute[15:0]": 0x2120}),
        )
        self.assertEqual(state.uregs[1], T.Const(0xFFFFFFFB))
        self.assertEqual(state.trace[-1]["operation"], "negate")

    def test_provisional_form_runs_only_when_named(self):
        word = T.Instruction(
            0, 6, "14d", {"ex": 1}, kind="uncertain", note="source: prm"
        )
        stopped = self.run_one(T.State(1), word)
        self.assertEqual(stopped.stopped, "uncertain or undecodable form: source: prm")
        self.assertEqual(stopped.provisional_used, ())
        allowed = self.run_one(T.State(1, provisional_forms=("14d",)), word)
        self.assertEqual(allowed.provisional_used, ("14d",))
        # The form now reaches its own handler, which stops for its own reason.
        self.assertEqual(allowed.stopped, "unsupported Type14d exclusive access")

    def test_type6b_bit_test_immediate_is_status_only(self):
        astatx = T.UREG_CODES["ASTATX"]
        for label, source_value, expected_sz in (
            ("bit set", 0b1000, False),
            ("bit clear", 0b0100, True),
        ):
            with self.subTest(label=label):
                fields = {
                    "cond[4:0]": 31,
                    "dataex[3:0]": 0,
                    "shiftimm[22:16]": 0x33,
                    "shiftimm[15:0]": (3 << 8) | (1 << 4) | 2,
                }
                state = self.run_one(
                    T.State(1, {2: T.Const(source_value), astatx: T.Const(0)}),
                    insn("6b_shiftimm", fields, 6),
                )
                self.assertNotIn(1, state.uregs)
                self.assertTrue(state.trace[-1]["status_only"])
                self.assertEqual(
                    state.uregs[astatx], T.Const((1 << 12) if expected_sz else 0)
                )

    def test_full_compute_register_to_mr_move_is_recorded(self):
        state = self.run_one(
            T.State(1, {T.UREG_CODES["R2"]: T.Const(0x1234)}),
            insn(
                "2a",
                {
                    "cond[4:0]": 31,
                    "compute[22:16]": 0x41,
                    "compute[15:0]": 0x0200,
                },
                6,
            ),
        )
        self.assertEqual(state.uregs[T.UREG_CODES["R2"]], T.Const(0x1234))
        self.assertEqual(
            (state.trace[-1]["operation"], state.trace[-1]["result_register"]),
            ("mr-data-move-mr0f", "MRF"),
        )
        # A register->MR0F move only defines the accumulator's low 32 bits
        # (PRM p.3-11): MR1F/MR2F stay unknown until written, so the whole
        # 80-bit accumulator is not yet fully known. Seed it directly here
        # (as though MR1F/MR2F had also been written 0, sign-extending
        # 0x1234's positive integer format) so the multiply-accumulate
        # chain below still has a concrete accumulator to read.
        self.assertFalse(state.special["MRF"].known)
        state.special["MRF"] = T._mr_from_signed(0x1234)
        state.uregs[T.UREG_CODES["R10"]] = T.Const(3)
        state = self.run_one(
            state,
            insn(
                "2a",
                {
                    "cond[4:0]": 31,
                    "compute[22:16]": 0x1B,
                    "compute[15:0]": 0x40A2,
                },
                6,
            ),
        )
        self.assertEqual(state.special["MRF"].signed(), 0x1234 + 3 * 0x1234)
        self.assertEqual(state.trace[-1]["operation"], "multiply-accumulate")
        state.uregs[T.UREG_CODES["R6"]] = T.Const(2)
        state.uregs[T.UREG_CODES["R11"]] = T.Const(4)
        state = self.run_one(
            state,
            insn(
                "2a",
                {
                    "cond[4:0]": 31,
                    "compute[22:16]": 0x1B,
                    "compute[15:0]": 0x07B6,
                },
                6,
            ),
        )
        self.assertEqual(state.uregs[T.UREG_CODES["R7"]], T.Const(0x48D8))
        # Same operation name as the mrf-destination accumulate above --
        # compute_mult.py's _mac_handler now names the whole family by
        # add/subtract, not by which of RN/mrf/mrb receives the result.
        self.assertEqual(state.trace[-1]["operation"], "multiply-accumulate")


class TraceDelaySlotTest(TraceRunMixin, unittest.TestCase):
    """Delay slots, type8 and relative jumps."""

    def test_19a_constant_and_unknown(self):
        f = {
            "g": 1,
            "idis[2:0]": 2,
            "is[2:0]": 1,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        self.assertEqual(
            self.run_one(T.State(1, {25: T.Const(7)}), insn("19a", f, 6)).uregs[27],
            T.Const(5),
        )
        self.assertIsInstance(
            self.run_one(T.State(1), insn("19a", f, 6)).uregs[27], T.Unknown
        )

    def test_delay_slots_variable_width_and_target(self):
        branch = insn(
            "8a_abs",
            {"b": 0, "j": 1, "cond[4:0]": 31, "addr[23:16]": 0, "addr[15:0]": 99},
            6,
        )
        s = self.run_one(T.State(10), branch)
        s = self.run_one(s, insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4))
        s = self.run_one(
            s, insn("17a", {"ureg[6:0]": 1, "data[31:16]": 0, "data[15:0]": 2}, 6)
        )
        self.assertEqual(s.pc_sw, 99)
        self.assertEqual([e["pc_sw"] for e in s.trace], [10, 13, 15])

    def test_conditional_forks_have_independent_two_slot_delays(self):
        branch = insn(
            "8a_abs",
            {"b": 0, "j": 1, "cond[4:0]": 1, "addr[23:16]": 0, "addr[15:0]": 30},
            6,
        )
        taken, not_taken = T._execute(T.State(10), branch)
        self.assertEqual(taken.trace[-1]["action"], "branch")
        self.assertEqual(not_taken.trace[-1]["action"], "branch-not-taken")
        not_taken.trace[-1]["action"] = "changed-not-taken"
        self.assertEqual(taken.trace[-1]["action"], "branch")

        # The not-taken state retains its delay marker and rejects transfers
        # in both delay slots.
        self.assertEqual(
            self.run_one(not_taken, branch).stopped, "nested delayed transfer"
        )
        _, not_taken = T._execute(T.State(10), branch)
        not_taken = self.run_one(
            not_taken, insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4)
        )
        self.assertEqual(
            self.run_one(not_taken, branch).stopped, "nested delayed transfer"
        )

        taken, not_taken = T._execute(T.State(10), branch)
        slot32 = insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4)
        slot48 = insn("17a", {"ureg[6:0]": 1, "data[31:16]": 0, "data[15:0]": 2}, 6)
        taken = self.run_one(self.run_one(taken, slot32), slot48)
        not_taken = self.run_one(self.run_one(not_taken, slot32), slot48)
        self.assertEqual(taken.pc_sw, 30)
        self.assertEqual(not_taken.pc_sw, 18)
        self.assertEqual([e["pc_sw"] for e in taken.trace], [10, 13, 15])
        self.assertEqual([e["pc_sw"] for e in not_taken.trace], [10, 13, 15])

    def test_type8_without_db_transfers_immediately(self):
        branch = insn(
            "8a_abs",
            {"b": 0, "j": 0, "cond[4:0]": 31, "addr[23:16]": 0, "addr[15:0]": 30},
            6,
        )
        state = self.run_one(T.State(10), branch)
        self.assertEqual(state.pc_sw, 30)
        self.assertIsNone(state.pending)
        self.assertEqual(state.steps, 1)

        conditional = insn(
            "8a_abs",
            {"b": 0, "j": 0, "cond[4:0]": 1, "addr[23:16]": 0, "addr[15:0]": 30},
            6,
        )
        taken, not_taken = T._execute(T.State(10), conditional)
        self.assertEqual(taken.pc_sw, 30)
        self.assertEqual(not_taken.pc_sw, 13)
        self.assertEqual(not_taken.trace[-1]["action"], "branch-not-taken")

    def test_type8_call_without_db_uses_immediate_return_address(self):
        call = insn(
            "8a_abs",
            {"b": 1, "j": 0, "cond[4:0]": 31, "addr[23:16]": 0, "addr[15:0]": 99},
            6,
        )
        taken = self.run_one(T.State(10), call)
        self.assertEqual(taken.stopped, "external-call")
        self.assertEqual(
            (taken.trace[-1]["target_sw"], taken.trace[-1]["return_sw"]), (99, 13)
        )

        conditional = insn(
            "8a_abs",
            {"b": 1, "j": 0, "cond[4:0]": 1, "addr[23:16]": 0, "addr[15:0]": 99},
            6,
        )
        taken, not_taken = T._execute(T.State(10), conditional)
        self.assertEqual(taken.stopped, "external-call")
        self.assertEqual(taken.trace[-1]["return_sw"], 13)
        self.assertEqual(not_taken.pc_sw, 13)
        self.assertIsNone(not_taken.stopped)
        self.assertEqual(not_taken.trace[-1]["action"], "branch-not-taken")

        negative = insn(
            "8a_rel",
            {
                "b": 1,
                "j": 0,
                "cond[4:0]": 31,
                "reladdr[23:16]": 0x80,
                "reladdr[15:0]": 0,
            },
            6,
        )
        guarded = self.run_one(
            T.State(
                0,
                concrete=loader_memory(loader_block(0, 0, 4, payload=b"\0" * 4)),
                follow_loaded_calls=True,
            ),
            negative,
        )
        self.assertEqual(guarded.stopped, "external-call")
        self.assertEqual(guarded.trace[-1]["target_sw"], 0x800000)

    def test_delayed_call_returns_after_variable_width_slots(self):
        call = insn("25a_direct", {"addr[23:16]": 0, "addr[15:0]": 99}, 4)
        s = self.run_one(T.State(10), call)
        s = self.run_one(s, insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4))
        s = self.run_one(
            s, insn("17a", {"ureg[6:0]": 1, "data[31:16]": 0, "data[15:0]": 2}, 6)
        )
        self.assertEqual(s.stopped, "external-call")
        self.assertEqual(s.trace[-1]["return_sw"], 17)
        self.assertEqual(s.trace[-1]["target_sw"], 99)
        self.assertEqual([e["pc_sw"] for e in s.trace[:-1]], [10, 10, 12, 14])

    def test_type9a_relative_branch_applies_compute_on_true_bit_test(self):
        branch = insn(
            "9a_rel",
            {
                "b": 0,
                "a": 0,
                "cond[4:0]": 0x0D,
                "j": 1,
                "e": 0,
                "ci": 0,
                "compute[22:16]": 0x02,
                "compute[15:0]": 0x9220,
                "reladdr[5:5]": 0,
                "reladdr[4:0]": 7,
            },
            6,
        )
        true_state = self.run_one(
            T.State(
                10,
                {
                    T.UREG_CODES["R2"]: T.Const(4),
                    T.UREG_CODES["ASTATX"]: T.Const(1 << 18),
                },
            ),
            branch,
        )
        self.assertEqual(true_state.uregs[T.UREG_CODES["R2"]], T.Const(5))
        self.assertEqual(true_state.pending.target, 17)

        false_state = self.run_one(
            T.State(
                10,
                {
                    T.UREG_CODES["R2"]: T.Const(4),
                    T.UREG_CODES["ASTATX"]: T.Const(0),
                },
            ),
            branch,
        )
        self.assertEqual(false_state.uregs[T.UREG_CODES["R2"]], T.Const(4))
        self.assertIsNone(false_state.pending)
        self.assertEqual(false_state.pc_sw, 13)

        taken, not_taken = T._execute(
            T.State(10, {T.UREG_CODES["R2"]: T.Const(4)}), branch
        )
        self.assertEqual(taken.uregs[T.UREG_CODES["R2"]], T.Const(5))
        self.assertEqual(taken.pending.target, 17)
        self.assertEqual(not_taken.uregs[T.UREG_CODES["R2"]], T.Const(4))
        self.assertIsNone(not_taken.pending)

    def test_type9a_relative_non_delayed_jump_transfers_immediately(self):
        # A regression test for a bug this lane fixed: 9a_rel used to fail
        # closed on any non-delayed (j=0) jump, even though 9a_abs/9b_abs
        # already supported one via _immediate_transfer.
        branch = insn(
            "9a_rel",
            {
                "b": 0,
                "a": 0,
                "cond[4:0]": 31,
                "j": 0,
                "e": 0,
                "ci": 0,
                "compute[22:16]": 0,
                "compute[15:0]": 0,
                "reladdr[5:5]": 0,
                "reladdr[4:0]": 7,
            },
            6,
        )
        advanced = self.run_one(T.State(10), branch)
        self.assertEqual(advanced.pc_sw, 17)
        self.assertIsNone(advanced.pending)

    def test_type9a_relative_la_pops_loop_and_pc_stacks_once(self):
        # SHARC+ Core Programming Reference p.4-44 ("Loop Abort"): see the
        # identical 9a_abs test above for the exact citation.
        branch = insn(
            "9a_rel",
            {
                "b": 0,
                "a": 1,
                "cond[4:0]": 31,
                "j": 0,
                "e": 0,
                "ci": 0,
                "compute[22:16]": 0,
                "compute[15:0]": 0,
                "reladdr[5:5]": 0,
                "reladdr[4:0]": 7,
            },
            6,
        )
        state = T.State(10, call_stack=[0x13, 0x99], loops=[T.Loop(0x13, 0x20, 2, 1)])
        advanced = self.run_one(state, branch)
        self.assertEqual(advanced.pc_sw, 17)
        self.assertEqual(advanced.loops, [])
        self.assertEqual(advanced.call_stack, [0x13])

    def test_type9a_relative_rejects_ci_modifier(self):
        branch = insn(
            "9a_rel",
            {
                "b": 0,
                "a": 0,
                "cond[4:0]": 31,
                "j": 1,
                "e": 0,
                "ci": 1,
                "compute[22:16]": 0,
                "compute[15:0]": 0,
                "reladdr[5:5]": 0,
                "reladdr[4:0]": 7,
            },
            6,
        )
        stopped = self.run_one(T.State(10), branch)
        self.assertEqual(stopped.stopped, "unsupported Type9a CI modifier")

    def test_type2a_not_av_saturate_mrf_preserves_unknown_dependency(self):
        saturate = insn(
            "2a",
            {
                "cond[4:0]": 0x14,
                "compute[22:16]": 0x10,
                "compute[15:0]": 0x072E,
            },
            6,
        )
        executed = self.run_one(
            T.State(10, {T.UREG_CODES["ASTATX"]: T.Const(0)}), saturate
        )
        self.assertIsInstance(executed.uregs[T.UREG_CODES["R7"]], T.Unknown)
        self.assertEqual(executed.trace[-1]["operation"], "saturate-mrf")
        skipped = self.run_one(
            T.State(10, {T.UREG_CODES["ASTATX"]: T.Const(1 << 1)}), saturate
        )
        self.assertNotIn(T.UREG_CODES["R7"], skipped.uregs)
        self.assertEqual(skipped.trace[-1]["action"], "compute-skipped")

    def test_cjump_frame_and_rframe_restore_compiler_frame(self):
        call = insn("25a_direct", {"addr[23:16]": 0, "addr[15:0]": 99}, 4)
        state = self.run_one(
            T.State(
                10,
                {
                    T.UREG_CODES["I6"]: T.Const(0x200),
                    T.UREG_CODES["I7"]: T.Const(0x100),
                },
            ),
            call,
        )
        self.assertEqual(state.uregs[T.UREG_CODES["R2"]], T.Const(0x200))
        self.assertEqual(state.uregs[T.UREG_CODES["I6"]], T.Const(0x100))
        self.assertEqual(state.trace[-2]["action"], "cjump-frame")

        restoring = T.State(
            30,
            {
                T.UREG_CODES["I6"]: T.Const(0x100),
                T.UREG_CODES["I7"]: T.Const(0x80),
            },
            pending=T.Pending(None, slots=2, return_from_call=True),
            call_stack=[40],
            concrete=loader_memory(loader_block(0, 0x100, 4, payload=b"\0" * 4)),
            assume_nw32=True,
        )
        self.assertTrue(T._dm_write(restoring, 0x100, 4, T.Const(0x200)))
        restored = self.run_one(restoring, insn("25c_rframe", {}, 2))
        self.assertEqual(restored.uregs[T.UREG_CODES["I7"]], T.Const(0x100))
        self.assertEqual(restored.uregs[T.UREG_CODES["I6"]], T.Const(0x200))
        self.assertEqual(restored.trace[-1]["action"], "rframe")

    def test_type16_immediate_stores_scale_normal_word_postmodify(self):
        memory = loader_memory(loader_block(0, 0xF8, 16, payload=b"\0" * 16))
        state = T.State(
            1,
            {
                T.UREG_CODES["I7"]: T.Const(0x100),
                T.UREG_CODES["M7"]: T.Const(0xFFFFFFFF),
            },
            concrete=memory,
            assume_nw32=True,
        )
        stored16 = self.run_one(
            state,
            insn(
                "16b",
                {"i[2:0]": 7, "m[2:0]": 7, "g": 0, "data[15:0]": 2},
                4,
            ),
        )
        self.assertEqual(T._dm_read(stored16, 0x100, 4), T.Const(2))
        self.assertEqual(stored16.uregs[T.UREG_CODES["I7"]], T.Const(0xFC))
        stored32 = self.run_one(
            stored16,
            insn(
                "16a",
                {
                    "i[2:0]": 7,
                    "m[2:0]": 7,
                    "g": 0,
                    "sl": 0,
                    "by": 0,
                    "data[31:16]": 0x1234,
                    "data[15:0]": 0x5678,
                },
                6,
            ),
        )
        self.assertEqual(T._dm_read(stored32, 0xFC, 4), T.Const(0x12345678))
        self.assertEqual(stored32.uregs[T.UREG_CODES["I7"]], T.Const(0xF8))

    def test_provisional_stop(self):
        self.assertIn(
            "uncertain",
            self.run_one(T.State(1), insn("19p", {}, kind="uncertain")).stopped,
        )


if __name__ == "__main__":
    unittest.main()
