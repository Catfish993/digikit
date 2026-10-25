"""tools/sharc_dev/sharc_accesses.py: a step's loads and stores from the runner's events."""

import unittest
from importlib import import_module

T = import_module("tools.sharc_dev.sharc_trace")
A = import_module("tools.sharc_dev.sharc_accesses")
from support.sharc_trace_build import (  # noqa: E402
    TraceRunMixin,
    insn,
    loader_block,
    loader_memory,
)


def _type15a(g, d, i, ureg, displacement):
    return insn(
        "15a",
        {
            "g": g,
            "i[2:0]": i,
            "d": d,
            "l": 0,
            "ureg[6:0]": ureg,
            "addr[31:16]": 0,
            "addr[15:0]": displacement,
        },
        6,
    )


class StepAccessesTest(TraceRunMixin, unittest.TestCase):
    def test_a_dm_store_is_one_four_byte_write(self):
        memory = loader_memory(loader_block(1, 0x6000, 4, payload=b"\0" * 4))
        state = T.State(
            1,
            {16: T.Const(0x2000), 5: T.Const(0xCAFEBABE)},
            concrete=memory,
            assume_nw32=True,
        )
        A.enable(state)
        stored = self.run_one(state, _type15a(0, 1, 0, 5, 0x1000))
        accesses = list(A.step_accesses(stored.trace))
        self.assertEqual(
            [(a.kind, a.space, a.address, a.width, a.companion) for a in accesses],
            [("w", "DM", 0x6000, 4, False)],
        )
        self.assertEqual(T._dm_read(stored, 0x6000, 4), T.Const(0xCAFEBABE))

    def test_a_pm_load_names_its_bus(self):
        state = T.State(1, {T.UREG_CODES["I11"]: T.Const(5)})
        A.enable(state)
        loaded = self.run_one(state, _type15a(1, 0, 3, 0, 0x10))
        accesses = list(A.step_accesses(loaded.trace))
        self.assertEqual([(a.kind, a.space) for a in accesses], [("r", "PM")])

    def test_events_that_move_no_data_are_skipped(self):
        events = [
            {"action": "compute", "pc_sw": 1},
            {"action": "store", "space": "DM", "address": "I0 + 4", "pc_sw": 2},
            {"action": "load-pey", "space": "DM", "address": 0x100, "pc_sw": 3},
            {
                "action": "load",
                "space": "DM",
                "address": 0x200,
                "long_word": True,
                "pc_sw": 4,
            },
            {
                "action": "load",
                "space": "DM",
                "address": 0x300,
                "access_width": "short-word",
                "pc_sw": 5,
            },
        ]
        got = [
            (a.kind, a.address, a.width, a.companion) for a in A.step_accesses(events)
        ]
        self.assertEqual(
            got, [("r", 0x100, 4, True), ("r", 0x200, 8, False), ("r", 0x300, 2, False)]
        )


if __name__ == "__main__":
    unittest.main()
