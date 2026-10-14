"""Symbolic tracer: pre/post-modify, affine and circular addressing."""

import json
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


class TraceModifyFormsTest(TraceRunMixin, unittest.TestCase):
    """Pre- and post-modify addressing forms."""

    def test_type2a_increment_unconditional_and_affine(self):
        def full(opcode, rn, rx, ry=0):
            return {
                "compute[22:16]": opcode >> 4,
                "compute[15:0]": ((opcode & 0xF) << 12) | (rn << 8) | (rx << 4) | ry,
            }

        concrete = self.run_one(
            T.State(10, {4: T.Const(0x41)}),
            insn("2a", {"cond[4:0]": 0x1F, **full(0x29, 3, 4)}, 6),
        )
        self.assertEqual((concrete.pc_sw, concrete.uregs[3]), (13, T.Const(0x42)))
        self.assertEqual(
            concrete.trace[-1],
            {
                "pc_sw": 10,
                "form": "2a",
                "action": "compute",
                "operation": "increment",
                "result_register": "R3",
                "value": 0x42,
                "condition": 0x1F,
                "predicate_assumption": True,
            },
        )
        affine = self.run_one(
            T.State(10, {4: T.symbol("counter")}),
            insn("2a", {"cond[4:0]": 0x1F, **full(0x29, 3, 4)}, 6),
        )
        self.assertEqual(affine.uregs[3], T.Affine(1, (("counter", 1),)))

    def test_type2a_decrement_unconditional_and_affine(self):
        def full(opcode, rn, rx, ry=0):
            return {
                "compute[22:16]": opcode >> 4,
                "compute[15:0]": ((opcode & 0xF) << 12) | (rn << 8) | (rx << 4) | ry,
            }

        concrete = self.run_one(
            T.State(10, {4: T.Const(0)}),
            insn("2a", {"cond[4:0]": 0x1F, **full(0x2A, 3, 4)}, 6),
        )
        self.assertEqual((concrete.pc_sw, concrete.uregs[3]), (13, T.Const(0xFFFFFFFF)))
        self.assertEqual(
            concrete.trace[-1],
            {
                "pc_sw": 10,
                "form": "2a",
                "action": "compute",
                "operation": "decrement",
                "result_register": "R3",
                "value": 0xFFFFFFFF,
                "condition": 0x1F,
                "predicate_assumption": True,
            },
        )
        affine = self.run_one(
            T.State(10, {4: T.symbol("counter")}),
            insn("2a", {"cond[4:0]": 0x1F, **full(0x2A, 3, 4)}, 6),
        )
        self.assertEqual(affine.uregs[3], T.Affine(-1, (("counter", 1),)))

    def test_type2a_unknown_predicate_forks_execute_and_skip(self):
        fields = {
            "cond[4:0]": 1,
            "compute[22:16]": 2,
            "compute[15:0]": 0x9340,
        }
        executed, skipped = T._execute(
            T.State(10, {4: T.Const(7)}), insn("2a", fields, 6)
        )
        self.assertEqual((executed.pc_sw, skipped.pc_sw), (13, 13))
        self.assertEqual(executed.uregs[3], T.Const(8))
        self.assertNotIn(3, skipped.uregs)
        self.assertEqual(
            (
                executed.trace[-1]["condition"],
                executed.trace[-1]["predicate_assumption"],
            ),
            (1, True),
        )
        self.assertEqual(
            (
                skipped.trace[-1]["action"],
                skipped.trace[-1]["condition"],
                skipped.trace[-1]["predicate_assumption"],
            ),
            ("compute-skipped", 1, False),
        )
        executed.uregs[3] = T.Const(0)
        self.assertNotIn(3, skipped.uregs)

    def test_type2a_status_only_and_unsupported_do_not_write(self):
        compare = self.run_one(
            T.State(10, {0: T.Const(0x55), 2: T.Const(4), 12: T.Const(4)}),
            insn(
                "2a",
                {
                    "cond[4:0]": 0x1F,
                    "compute[22:16]": 0,
                    "compute[15:0]": 0xA0C2,
                },
                6,
            ),
        )
        self.assertEqual(compare.uregs[0], T.Const(0x55))
        self.assertEqual(
            (compare.trace[-1]["operation"], compare.trace[-1]["status_only"]),
            ("compare", True),
        )
        original = {1: T.Const(2)}
        # cu=11 (reserved, "not used by SINGLEFN" -- PRM Table 18-1 p.423)
        # is never matched by any compute branch, fixed-point or float,
        # so it still reaches the final unsupported-opcode raise. (0xF/0x1234
        # used to serve as this test's "definitely unsupported" filler value,
        # but cu=00 opcode-top-nibble-0xF is now the documented float dual
        # add/subtract encoding -- see test_dual_add_subtract_* below.)
        unsupported = self.run_one(
            T.State(10, dict(original)),
            insn(
                "2a",
                {"cond[4:0]": 0x1F, "compute[22:16]": 0x30, "compute[15:0]": 0},
                6,
            ),
        )
        self.assertIn("unsupported full compute", unsupported.stopped)
        self.assertEqual(unsupported.uregs, original)
        empty = self.run_one(
            T.State(10, dict(original)),
            insn(
                "2a",
                {"cond[4:0]": 0x1F, "compute[22:16]": 0, "compute[15:0]": 0},
                6,
            ),
        )
        self.assertEqual(empty.stopped, "empty full compute")
        self.assertEqual(empty.uregs, original)

    def test_type2a_second_call_delay_slot_forks_to_external_call(self):
        call = insn("25a_direct", {"addr[23:16]": 0, "addr[15:0]": 99}, 4)
        first_slot = insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4)
        type2a = insn(
            "2a",
            {"cond[4:0]": 1, "compute[22:16]": 2, "compute[15:0]": 0x9340},
            6,
        )
        state = self.run_one(T.State(10, {4: T.Const(7)}), call)
        state = self.run_one(state, first_slot)
        executed, skipped = T._execute(state, type2a)
        for result, assumed in ((executed, True), (skipped, False)):
            self.assertEqual(result.stopped, "external-call")
            self.assertEqual(
                (result.trace[-1]["return_sw"], result.trace[-1]["target_sw"]), (17, 99)
            )
            self.assertEqual(result.trace[-2]["predicate_assumption"], assumed)
        self.assertEqual(executed.uregs[3], T.Const(8))
        self.assertNotIn(3, skipped.uregs)

    def test_type4a_pre_post_and_type3c(self):
        base = {
            "i[2:0]": 1,
            "g": 0,
            "d": 0,
            "cond[4:0]": 31,
            "data[5:5]": 1,
            "data[4:0]": 0x1F,
            "dreg[3:0]": 2,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        s = self.run_one(
            T.State(1, {17: T.Const(0x100)}), insn("4a", {**base, "u": 0}, 6)
        )
        self.assertEqual(s.trace[0]["address"], 0xFF)
        self.assertEqual(s.uregs[17], T.Const(0x100))
        # The full compute reads R2 before this postmodify load overwrites it.
        computed = {"compute[22:16]": 0x02, "compute[15:0]": 0x1320}
        s = self.run_one(
            T.State(1, {17: T.Const(0x100), 2: T.Const(4)}),
            insn("4a", {**base, **computed, "u": 1}, 6),
        )
        self.assertEqual(s.uregs[3], T.Const(4))
        s = self.run_one(
            T.State(1, {17: T.Const(0x100), 2: T.Const(4)}),
            insn("4a", {**base, "u": 1, "d": 1}, 6),
        )
        self.assertEqual(s.trace[0]["address"], 0x100)
        self.assertEqual(s.trace[0]["value"], 4)
        self.assertEqual(s.uregs[17], T.Const(0xFF))
        self.assertEqual((T.UREG_CODES["I0"], T.UREG_CODES["M0"]), (16, 32))
        s = self.run_one(
            T.State(1, {16: T.Const(0x80), 32: T.Const(3)}),
            insn("3c", {"dmi[2:0]": 0, "dmm[2:0]": 0, "d": 0, "dreg[3:0]": 3}, 2),
        )
        self.assertEqual(
            (s.trace[0]["space"], s.trace[0]["address"], s.uregs[16]),
            ("DM", 0x80, T.Const(0x83)),
        )
        # The Type3c call-slot case selects DAG1 I7 and M7 directly.
        self.assertEqual((T.UREG_NAMES[23], T.UREG_NAMES[39]), ("I7", "M7"))
        s = self.run_one(
            T.State(1, {23: T.Const(0x90), 39: T.Const(4)}),
            insn("3c", {"dmi[2:0]": 7, "dmm[2:0]": 7, "d": 0, "dreg[3:0]": 3}, 2),
        )
        self.assertEqual((s.trace[0]["address"], s.uregs[23]), (0x90, T.Const(0x94)))
        s = self.run_one(
            T.State(
                1,
                {23: T.Const(0x90), 39: T.Const(1)},
                assume_nw32=True,
            ),
            insn("3c", {"dmi[2:0]": 7, "dmm[2:0]": 7, "d": 0, "dreg[3:0]": 3}, 2),
        )
        self.assertEqual((s.trace[0]["address"], s.uregs[23]), (0x90, T.Const(0x94)))

    def test_type16a_store_and_unknown_postmodify(self):
        f = {
            "i[2:0]": 2,
            "m[2:0]": 3,
            "g": 1,
            "sl": 0,
            "by": 0,
            "data[31:16]": 0x1234,
            "data[15:0]": 0x5678,
        }
        s = self.run_one(
            T.State(1, {26: T.Const(0x90), 43: T.Const(4)}), insn("16a", f, 6)
        )
        self.assertEqual(
            (s.trace[0]["space"], s.trace[0]["value"], s.uregs[26]),
            ("PM", 0x12345678, T.Const(0x94)),
        )
        pm_with_nw32_dm = self.run_one(
            T.State(
                1,
                {26: T.Const(0x90), 43: T.Const(4)},
                assume_nw32=True,
            ),
            insn("16a", f, 6),
        )
        self.assertEqual(pm_with_nw32_dm.uregs[26], T.Const(0x94))
        s = self.run_one(T.State(1), insn("16a", f, 6))
        self.assertIsInstance(s.uregs[26], T.Unknown)

    def test_synthetic_prefix_forms(self):
        full_mul = {"compute[22:16]": 0x17, "compute[15:0]": 0x0212}
        move = {
            "srcureghigh[4:0]": 1,
            "srcureglow[1:1]": 0,
            "srcureglow[0:0]": 0,
            "dstureg[6:0]": 13,
            "cond[4:0]": 31,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
        }
        records = [
            insn("5a_move", move, 6),
            insn(
                "15b",
                {"i[2:0]": 0, "g": 0, "d": 1, "l": 1, "ureg[6:0]": 4, "data[6:0]": 0},
            ),
            insn(
                "15b",
                {"i[2:0]": 0, "g": 0, "d": 0, "l": 1, "ureg[6:0]": 14, "data[6:0]": 0},
            ),
            insn("2c", {"compute[11:0]": 0x202}, 2),
            insn("17b", {"ureg[6:0]": 7, "data[15:0]": 1}),
            insn(
                "4a",
                {
                    "i[2:0]": 1,
                    "g": 0,
                    "d": 0,
                    "u": 0,
                    "cond[4:0]": 31,
                    "data[5:5]": 0,
                    "data[4:0]": 0,
                    "dreg[3:0]": 14,
                    "compute[22:16]": 0,
                    "compute[15:0]": 0,
                },
                6,
            ),
            insn(
                "4a",
                {
                    "i[2:0]": 1,
                    "g": 0,
                    "d": 0,
                    "u": 0,
                    "cond[4:0]": 31,
                    "data[5:5]": 0,
                    "data[4:0]": 0,
                    "dreg[3:0]": 4,
                    "compute[22:16]": 0,
                    "compute[15:0]": 0,
                },
                6,
            ),
            insn(
                "5a_move",
                {
                    **move,
                    "srcureghigh[4:0]": 3,
                    "srcureglow[1:1]": 1,
                    "srcureglow[0:0]": 0,
                    "dstureg[6:0]": 4,
                    **full_mul,
                },
                6,
            ),
        ]
        s = T.State(
            1,
            {
                4: T.Const(0x55),
                1: T.Const(6),
                2: T.Const(7),
                16: T.Const(0),
                17: T.Const(0),
            },
        )
        for record in records:
            s = self.run_one(s, record)
        self.assertEqual(s.uregs[13], T.Const(0x55))
        self.assertEqual(s.uregs[2], T.Const(42))
        self.assertIsInstance(s.uregs[14], T.Unknown)
        self.assertEqual(s.uregs[4], s.uregs[14])

    def test_15b_concrete_symbolic_load_and_store(self):
        f = {"i[2:0]": 1, "g": 0, "d": 0, "l": 1, "ureg[6:0]": 2, "data[6:0]": 0x7F}
        s = self.run_one(T.State(1, {17: T.Const(0x100)}), insn("15b", f))
        self.assertEqual(s.trace[0]["address"], 0xFF)
        self.assertIsInstance(s.uregs[2], T.Unknown)
        f["d"] = 1
        s = self.run_one(T.State(1, {2: T.Const(5)}), insn("15b", f))
        self.assertEqual(s.trace[0]["action"], "store")
        self.assertEqual(s.trace[0]["expression"], "I1 + -1")

    def test_normal_word_immediate_offsets_scale_only_when_opted_in(self):
        type4 = {
            "i[2:0]": 1,
            "g": 0,
            "d": 0,
            "cond[4:0]": 31,
            "data[5:5]": 0,
            "data[4:0]": 3,
            "dreg[3:0]": 2,
            "compute[22:16]": 0,
            "compute[15:0]": 0,
            "u": 0,
        }
        state = T.State(1, {17: T.Const(0x100)}, assume_nw32=True)
        s = self.run_one(state, insn("4a", type4, 6))
        self.assertEqual(s.trace[0]["address"], 0x10C)

        type15 = {
            "i[2:0]": 1,
            "g": 0,
            "d": 0,
            "l": 0,
            "ureg[6:0]": 2,
            "data[6:0]": 3,
        }
        state = T.State(1, {17: T.Const(0x100)}, assume_nw32=True)
        s = self.run_one(state, insn("15b", type15))
        self.assertEqual(s.trace[0]["address"], 0x10C)

    def test_type3b_dm_premodify_load_and_pm_postmodify_store(self):
        load = {
            "u": 0,
            "i[2:0]": 1,
            "m[2:0]": 2,
            "g": 0,
            "d": 0,
            "l": 0,
            "x": 1,
            "w": 1,
            "ureg[6:0]": 7,
            "cond[4:0]": 31,
        }
        s = self.run_one(
            T.State(1, {17: T.symbol("buffer"), 34: T.Const(4)}), insn("3b", load)
        )
        event = s.trace[0]
        self.assertEqual(
            (
                event["space"],
                event["expression"],
                event["addressing_mode"],
                event["access_width"],
            ),
            ("DM", "buffer + 0x4", "pre-modify", "normal-word"),
        )
        self.assertEqual(s.uregs[17], T.symbol("buffer"))
        self.assertEqual(s.uregs[7], T.Unknown("memory-address buffer + 0x4"))

        store = {
            **load,
            "u": 1,
            "i[2:0]": 2,
            "m[2:0]": 3,
            "g": 1,
            "d": 1,
            "ureg[6:0]": 4,
        }
        s = self.run_one(
            T.State(1, {26: T.Const(0x90), 43: T.Const(4), 4: T.Const(0x55)}),
            insn("3b", store),
        )
        event = s.trace[0]
        self.assertEqual(
            (
                event["space"],
                event["address"],
                event["value"],
                event["addressing_mode"],
            ),
            ("PM", 0x90, 0x55, "post-modify"),
        )
        self.assertEqual(s.uregs[26], T.Const(0x94))

    def test_type3b_widths_and_rejections_do_not_mutate(self):
        base = {
            "u": 0,
            "i[2:0]": 0,
            "m[2:0]": 0,
            "g": 0,
            "d": 0,
            "ureg[6:0]": 2,
            "cond[4:0]": 31,
        }
        expected = {
            (0, 1, 1): ("normal-word", 0x83),
            (0, 0, 0): ("byte", 0x83),
            (0, 1, 0): ("byte-sign-extended", 0x83),
            (1, 0, 0): ("short-word", 0x86),
            (1, 1, 0): ("short-word-sign-extended", 0x86),
            # (lw) steps in normal-word units (PRM p.6-9, Table 6-2), so
            # unscaled here, where assume_nw32 is off.
            (1, 1, 1): ("long-word", 0x83),
        }
        for (l, x, w), (access_width, address) in expected.items():
            event = self.run_one(
                T.State(1, {16: T.Const(0x80), 32: T.Const(3)}),
                insn("3b", {**base, "l": l, "x": x, "w": w}),
            ).trace[0]
            self.assertEqual(event["access_width"], access_width)
            self.assertEqual(event["address"], address)

        for fields, reason in (
            ({"l": 0, "x": 0, "w": 1}, "unsupported Type3b access width"),
            (
                {"d": 1, "l": 0, "x": 1, "w": 0},
                "unsupported Type3b sign-extended store",
            ),
        ):
            state = self.run_one(
                T.State(1, {16: T.Const(0x80), 32: T.Const(3), 2: T.Const(9)}),
                insn("3b", {**base, **fields}),
            )
            self.assertEqual(state.stopped, reason)
            self.assertEqual(
                state.uregs, {16: T.Const(0x80), 32: T.Const(3), 2: T.Const(9)}
            )

        invalid = T._execute(
            T.State(1, {16: T.Const(0x80), 32: T.Const(3), 2: T.Const(9)}),
            insn("3b", {**base, "cond[4:0]": 1, "l": 0, "x": 0, "w": 1}),
        )
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0].stopped, "unsupported Type3b access width")
        self.assertEqual(
            invalid[0].uregs, {16: T.Const(0x80), 32: T.Const(3), 2: T.Const(9)}
        )

    def test_type3b_unknown_predicate_forks_premodify_load_and_postmodify_store(self):
        load = {
            "u": 0,
            "i[2:0]": 1,
            "m[2:0]": 2,
            "g": 0,
            "d": 0,
            "l": 0,
            "x": 1,
            "w": 1,
            "ureg[6:0]": 7,
            "cond[4:0]": 1,
        }
        executed, skipped = T._execute(
            T.State(10, {17: T.symbol("buffer"), 34: T.Const(4)}),
            insn("3b", load),
        )
        self.assertEqual((executed.pc_sw, skipped.pc_sw), (12, 12))
        self.assertEqual(executed.uregs[7], T.Unknown("memory-address buffer + 0x4"))
        self.assertNotIn(7, skipped.uregs)
        self.assertEqual(executed.uregs[17], T.symbol("buffer"))
        self.assertEqual(skipped.uregs[17], T.symbol("buffer"))
        self.assertEqual(
            (
                executed.trace[-1]["condition"],
                executed.trace[-1]["predicate_assumption"],
            ),
            (1, True),
        )
        self.assertEqual(
            skipped.trace[-1],
            {
                "pc_sw": 10,
                "form": "3b",
                "action": "memory-access-skipped",
                "space": "DM",
                "ureg": "R7",
                "addressing_mode": "pre-modify",
                "access_width": "normal-word",
                "condition": 1,
                "predicate_assumption": False,
            },
        )

        store = {**load, "u": 1, "d": 1, "ureg[6:0]": 4}
        executed, skipped = T._execute(
            T.State(10, {17: T.Const(0x80), 34: T.Const(4), 4: T.Const(7)}),
            insn("3b", store),
        )
        self.assertEqual(executed.uregs[17], T.Const(0x84))
        self.assertEqual(skipped.uregs[17], T.Const(0x80))
        self.assertEqual(executed.trace[-1]["value"], 7)
        self.assertEqual(skipped.trace[-1]["addressing_mode"], "post-modify")
        executed.uregs[4] = T.Const(99)
        executed.uregs[17] = T.Const(0)
        self.assertEqual(
            (skipped.uregs[4], skipped.uregs[17]), (T.Const(7), T.Const(0x80))
        )

    def test_type3b_concretely_false_predicate_does_not_fork_or_store(self):
        # A regression test for a bug this lane fixed: _type_3b forked
        # into (executed, skipped) states -- attempting the memory access
        # on one of them -- even when the predicate resolved to a
        # concrete False, unlike _type_3a's matching fast path. A single-
        # path concrete runner (tools/sharc_dev/sharc_run.py) cannot resolve a
        # 2-state fork from an already-known-False predicate.
        astatx_code = T.UREG_CODES["ASTATX"]
        memory = loader_memory(loader_block(1, 0x80, 4, payload=b"\0" * 4))
        store = {
            "u": 0,
            "i[2:0]": 1,
            "m[2:0]": 2,
            "g": 0,
            "d": 1,
            "l": 0,
            "x": 1,
            "w": 1,
            "ureg[6:0]": 4,
            "cond[4:0]": 0x03,  # AC clear -> False, same as test_type3a's
        }
        state = T.State(
            1,
            {17: T.Const(0x80), 34: T.Const(4), 4: T.Const(7), astatx_code: T.Const(0)},
            concrete=memory,
        )
        results = T._execute(state, insn("3b", store))
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.uregs[17], T.Const(0x80))  # no post-modify
        self.assertIsNone(T._dm_read(result, 0x80, 4))  # store never ran
        self.assertEqual(
            (result.trace[-1]["action"], result.trace[-1]["predicate_assumption"]),
            ("memory-access-skipped", False),
        )

    def test_type3b_dm_postmodify_and_pm_premodify(self):
        store = {
            "i[2:0]": 1,
            "m[2:0]": 2,
            "d": 1,
            "l": 0,
            "x": 1,
            "w": 1,
            "ureg[6:0]": 4,
            "cond[4:0]": 31,
        }
        dm = self.run_one(
            T.State(1, {17: T.symbol("dm"), 34: T.Const(4), 4: T.Const(7)}),
            insn("3b", {**store, "u": 1, "g": 0}),
        )
        self.assertEqual(dm.trace[0]["expression"], "dm")
        self.assertEqual(dm.uregs[17], T.Affine(4, (("dm", 1),)))

        pm = self.run_one(
            T.State(1, {25: T.symbol("pm"), 42: T.Const(4), 4: T.Const(7)}),
            insn("3b", {**store, "u": 0, "g": 1}),
        )
        self.assertEqual(pm.trace[0]["expression"], "pm + 0x4")
        self.assertEqual(pm.uregs[25], T.symbol("pm"))

    def test_type4b_immediate_width_and_postmodify(self):
        memory = loader_memory(
            loader_block(1, L.SW_ALIAS_BASE + 0x80, 4, payload=b"\x80\x7f\0\0")
        )
        fields = {
            "i[2:0]": 1,
            "g": 0,
            "d": 0,
            "u": 1,
            "cond[4:0]": 31,
            "data[5:5]": 0,
            "data[4:0]": 2,
            "dreg[3:0]": 3,
            "l": 0,
            "x": 1,
            "w": 0,
        }
        state = self.run_one(
            T.State(1, {17: T.Const(0x80)}, concrete=memory),
            insn("4b", fields, 4),
        )
        self.assertEqual(state.uregs[3], T.Const(0xFFFFFF80))
        self.assertEqual(state.uregs[17], T.Const(0x82))
        self.assertEqual(state.trace[0]["access_width"], "byte-sign-extended")

        short = self.run_one(
            T.State(1, {17: T.Const(0x80)}),
            insn("4b", {**fields, "u": 0, "l": 1, "x": 1, "w": 0}, 4),
        )
        self.assertEqual(short.trace[0]["address"], 0x84)
        self.assertEqual(short.trace[0]["access_width"], "short-word-sign-extended")

    def test_type3b_unknown_predicate_second_call_delay_slot_preserves_call_target(
        self,
    ):
        call = insn("25a_direct", {"addr[23:16]": 0, "addr[15:0]": 99}, 4)
        type3b = insn(
            "3b",
            {
                "u": 1,
                "i[2:0]": 0,
                "m[2:0]": 0,
                "g": 0,
                "d": 0,
                "l": 0,
                "x": 1,
                "w": 1,
                "ureg[6:0]": 2,
                "cond[4:0]": 1,
            },
        )
        s = self.run_one(T.State(10, {16: T.Const(0x80), 32: T.Const(4)}), call)
        s = self.run_one(s, insn("17b", {"ureg[6:0]": 0, "data[15:0]": 1}, 4))
        executed, skipped = T._execute(s, type3b)
        for result, assumed in ((executed, True), (skipped, False)):
            self.assertEqual(result.stopped, "external-call")
            self.assertEqual(
                (result.trace[-1]["return_sw"], result.trace[-1]["target_sw"]), (16, 99)
            )
            self.assertEqual(result.trace[-2]["predicate_assumption"], assumed)
        self.assertIn(2, executed.uregs)
        self.assertIsInstance(skipped.uregs[2], T.Unknown)


class TraceAffineCircularTest(TraceRunMixin, unittest.TestCase):
    """Affine canonicalization and circular buffers."""

    def test_affine_canonicalization_and_arithmetic(self):
        value = T.Affine(0x1_0000_0001, (("z", 2), ("a", 1), ("z", -2), ("a", -1)))
        self.assertEqual((value.constant, value.terms), (1, ()))
        self.assertEqual(T._affine(3, (("z", 1),)), T.Affine(3, (("z", 1),)))
        self.assertEqual(T._affine(3, ()), T.Const(3))
        receive = T.symbol("receive_buffer")
        self.assertEqual(
            T._subtract(T._add(receive, T.Const(4), "ignored"), T.Const(5), "ignored"),
            T.Affine(0xFFFFFFFF, (("receive_buffer", 1),)),
        )
        self.assertEqual(
            T._multiply(T.Const(2), T.symbol("track_index"), "ignored"),
            T.Affine(0, (("track_index", 2),)),
        )
        rejected = T._multiply(
            receive, T.symbol("track_index"), "receive_buffer * track_index"
        )
        self.assertIsInstance(rejected, T.Unknown)
        self.assertIn("non-affine", rejected.reason)
        with self.assertRaises(ValueError):
            T.symbol("not-valid")

    def test_affine_compute_and_memory_events(self):
        short = lambda opcode, rn, rx: {"compute[11:0]": (opcode << 8) | (rn << 4) | rx}
        state = T.State(1, {1: T.symbol("receive_buffer"), 2: T.Const(0x94)})
        state = self.run_one(state, insn("2c", short(0, 1, 2), 2))
        self.assertEqual(state.uregs[1], T.Affine(0x94, (("receive_buffer", 1),)))
        self.assertEqual(T._render(state.uregs[1]), "receive_buffer + 0x94")
        state.uregs[3] = T.symbol("track_index")
        state.uregs[4] = T.Const(2)
        full = {"compute[22:16]": 0x17, "compute[15:0]": 0x0534}
        state.uregs[5] = T._compute(full, False, state.uregs)[1]
        state = self.run_one(state, insn("2c", short(0, 1, 5), 2))
        self.assertEqual(
            T._render(state.uregs[1]),
            "receive_buffer + 2*track_index + 0x94",
        )
        store = {"i[2:0]": 1, "g": 0, "d": 1, "l": 1, "ureg[6:0]": 2, "data[6:0]": 0x14}
        event = self.run_one(
            T.State(1, {17: state.uregs[1], 2: T.Const(5)}), insn("15b", store)
        ).trace[0]
        self.assertEqual(event["expression"], "receive_buffer + 2*track_index + 0xa8")
        self.assertEqual(
            event["address"],
            {
                "affine": {
                    "constant": 0xA8,
                    "terms": [["receive_buffer", 1], ["track_index", 2]],
                }
            },
        )
        self.assertEqual(json.loads(json.dumps(event))["address"], event["address"])

    def test_affine_type19_and_seed_handling(self):
        f = {"g": 0, "idis[2:0]": 2, "is[2:0]": 1, "data[31:16]": 0, "data[15:0]": 0x94}
        state = self.run_one(
            T.State(1, {17: T.symbol("receive_buffer")}), insn("19a", f, 6)
        )
        self.assertEqual(T._render(state.uregs[19]), "receive_buffer + 0x94")
        seeded = T.trace(b"", 0, 0, {"R1": 7}, max_steps=1)[0]
        self.assertEqual(seeded.uregs[1], T.Const(7))
        symbolic = T.trace(b"", 0, 0, {"R1": "@receive_buffer"}, max_steps=1)[0]
        self.assertEqual(symbolic.uregs[1], T.symbol("receive_buffer"))
        with self.assertRaises(ValueError):
            T.trace(b"", 0, 0, {"R1": "@"}, max_steps=1)

    def test_scaled_type19_circular_modify_of_bare_symbol_yields_fresh_symbol(self):
        # B7 not concrete (the ordinary case when a function is traced from
        # its own entry with every register seeded as its own named
        # symbol), L7 concrete (tools/sharc_dev/sharcwriters.py's own
        # GLOBAL_CONSTANT_SEEDS): the old behaviour collapsed straight to
        # Unknown("scaled circular modify I7"). It should now recognise
        # I7's bare entry symbol and mint a FRESH symbol for the result --
        # never reuse "I7e" itself, which would falsely assert the result
        # equals I7's own entry value (see the "different sites" test
        # below for why that would be unsound).
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        state = self.run_one(
            T.State(0x10, {i7: T.symbol("I7e"), l7: T.Const(0x1FD)}),
            insn("19a_scaled", fields, length=6),
        )
        result = state.uregs[i7]
        self.assertIsInstance(result, T.Affine)
        self.assertEqual(result.constant, 0)
        self.assertEqual(len(result.terms), 1)
        name, coefficient = result.terms[0]
        self.assertEqual(coefficient, 1)
        self.assertTrue(name.startswith(T.CIRC_SYMBOL_PREFIX))
        self.assertNotEqual(name, "I7e")  # never the input symbol itself
        self.assertTrue(state.trace[-1]["circular"])

    def test_scaled_type19_different_modify_sites_yield_different_symbols(self):
        # Two circular MODIFYs of I7's bare entry symbol at two different
        # program counters must NOT be asserted equal: reusing one name for
        # both (the bug this replaces) would let the Affine algebra cancel
        # "site A's result - site B's result" to a spurious 0, aliasing two
        # provably-different stack frames. Distinct PCs must mint distinct
        # symbol names.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        seed = {i7: T.symbol("I7e"), l7: T.Const(0x1FD)}
        at_a = self.run_one(
            T.State(0x10, dict(seed)), insn("19a_scaled", fields, length=6)
        )
        at_b = self.run_one(
            T.State(0x20, dict(seed)), insn("19a_scaled", fields, length=6)
        )
        self.assertNotEqual(at_a.uregs[i7], at_b.uregs[i7])
        # Re-running the SAME site is deterministic (same name each time),
        # which is what lets a store reached along two predicate-fork paths
        # from the same PC still compare equal -- only different sites
        # differ.
        again_a = self.run_one(
            T.State(0x10, dict(seed)), insn("19a_scaled", fields, length=6)
        )
        self.assertEqual(at_a.uregs[i7], again_a.uregs[i7])

    def test_scaled_type19_circular_modify_chains_through_a_prior_circ_symbol(self):
        # A value already bounded by an EARLIER circular MODIFY (a
        # CIRC_SYMBOL_PREFIX symbol, not a raw entry seed) also qualifies:
        # the second MODIFY still yields a fresh symbol, not Unknown.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        prior = T.Affine(0, ((T.CIRC_SYMBOL_PREFIX + "7_10", 1),))
        state = self.run_one(
            T.State(0x20, {i7: prior, l7: T.Const(0x1FD)}),
            insn("19a_scaled", fields, length=6),
        )
        self.assertIsInstance(state.uregs[i7], T.Affine)
        self.assertTrue(state.trace[-1]["circular"])

    def test_scaled_type19_circular_modify_of_symbol_with_small_offset_still_bounded(
        self,
    ):
        # PRM p.6-23's single +-byte_length correction still recovers a
        # value within one buffer length of a symbol's own bound when the
        # input carries a small existing offset (e.g. after a push/pop),
        # not just when it is perfectly bare.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        adjusted = T._add(T.symbol("I7e"), T.Const(-8), "I7e - 8")
        state = self.run_one(
            T.State(0x10, {i7: adjusted, l7: T.Const(0x1FD)}),
            insn("19a_scaled", fields, length=6),
        )
        self.assertIsInstance(state.uregs[i7], T.Affine)
        self.assertTrue(state.trace[-1]["circular"])

    def test_scaled_type19_circular_modify_of_symbol_with_large_offset_is_unknown(self):
        # An offset at or beyond one buffer length (0x1fd * 4 = 0x7f4) is
        # no longer provably "within one buffer length" of the symbol's
        # own bound, so the conservative Unknown fallback still applies.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        adjusted = T._add(T.symbol("I7e"), T.Const(0x800), "I7e + 0x800")
        state = self.run_one(
            T.State(0x10, {i7: adjusted, l7: T.Const(0x1FD)}),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(state.uregs[i7], T.Unknown("scaled circular modify I7"))

    def test_scaled_type19_circular_modify_of_unrecognized_symbol_is_unknown(self):
        # A bare symbol that does not match the recognised entry-seed or
        # circ_ naming convention (e.g. a loop-count or unrelated symbol
        # that happens to reach this register) is not assumed bounded.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        l7 = T.UREG_CODES["L7"]
        state = self.run_one(
            T.State(0x10, {i7: T.symbol("loop_count"), l7: T.Const(0x1FD)}),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(state.uregs[i7], T.Unknown("scaled circular modify I7"))

    def test_scaled_type19_circular_modify_without_concrete_length_is_unknown(self):
        # L7 itself not concrete: no numeric byte_length is available to
        # bound the accepted input offset against, so this falls back to
        # Unknown even for a bare, recognised symbol.
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        state = self.run_one(
            T.State(0x10, {i7: T.symbol("I7e")}),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(state.uregs[i7], T.Unknown("scaled circular modify I7"))

    def test_stack_bounded_symbol_helper(self):
        self.assertEqual(T._stack_bounded_symbol(T.symbol("I7e")), ("I7e", 0))
        self.assertEqual(
            T._stack_bounded_symbol(T._add(T.symbol("I7e"), T.Const(4), "x")),
            ("I7e", 4),
        )
        self.assertEqual(
            T._stack_bounded_symbol(T.symbol(T.CIRC_SYMBOL_PREFIX + "7_10")),
            (T.CIRC_SYMBOL_PREFIX + "7_10", 0),
        )
        self.assertIsNone(T._stack_bounded_symbol(T.Const(5)))
        self.assertIsNone(T._stack_bounded_symbol(T.symbol("loop_count")))
        two_terms = T.Affine(0, (("I7e", 1), ("M7e", 1)))
        self.assertIsNone(T._stack_bounded_symbol(two_terms))
        scaled = T.Affine(0, (("I7e", 2),))
        self.assertIsNone(T._stack_bounded_symbol(scaled))

    def test_scaled_type19_normal_word_modify_and_circular_wrap(self):
        fields = {
            "w": 1,
            "g": 0,
            "idis[2:0]": 0,
            "is[2:0]": 7,
            "data[31:16]": 0xFFFF,
            "data[15:0]": 0xFFFE,
        }
        i7 = T.UREG_CODES["I7"]
        b7 = T.UREG_CODES["B7"]
        l7 = T.UREG_CODES["L7"]

        byte_space = self.run_one(
            T.State(
                0xB893E2,
                {
                    i7: T.Const(0x26F7EE),
                    b7: T.Const(0x26F000),
                    l7: T.Const(0x1FD),
                },
                assume_nw32=True,
            ),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(byte_space.uregs[i7], T.Const(0x26F7E6))
        self.assertEqual(byte_space.uregs[b7], T.Const(0x26F000))
        self.assertEqual(byte_space.uregs[l7], T.Const(0x1FD))
        self.assertEqual(byte_space.trace[-1]["offset"], -8)

        normal_space = self.run_one(
            T.State(
                0x10,
                {i7: T.Const(0x100), b7: T.Const(0), l7: T.Const(0)},
            ),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(normal_space.uregs[i7], T.Const(0xFE))

        wrapped = self.run_one(
            T.State(
                0x10,
                {i7: T.Const(0x104), b7: T.Const(0x100), l7: T.Const(4)},
                assume_nw32=True,
            ),
            insn("19a_scaled", fields, length=6),
        )
        self.assertEqual(wrapped.uregs[i7], T.Const(0x10C))
        self.assertTrue(wrapped.trace[-1]["circular"])


if __name__ == "__main__":
    unittest.main()
