"""Tests for the bounded exploratory SHARC frontier driver."""

import hashlib
import struct
import unittest
from importlib import import_module
from support.paths import REPO_ROOT


F = import_module("tools.sharc_dev.sharc_frontier")
T = import_module("tools.sharc_dev.sharc_trace")
L = import_module("tools.sharc_dev.sharcldr")


def loader_block(code, address, count, arg=0, payload=b""):
    header = bytearray(struct.pack("<IIII", code | 0xAD000000, address, count, arg))
    header[2] = 0
    checksum = 0
    for byte in header:
        checksum ^= byte
    header[2] = checksum
    return bytes(header) + payload


def memory(payload, start=0x10):
    return L.LoadedMemory.from_stream(
        loader_block(1, L.sw_to_byte(start), len(payload), payload=payload)
    )


def manifest(raw, successor=0x11):
    return {
        "version": 1,
        "start_sw": 0x10,
        "sets": {"R0": 7},
        "restarts": [
            {
                "stop_pc_sw": 0x10,
                "successor_pc_sw": successor,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "assumptions": ["synthetic unsupported parcel has no control-flow effect"],
            }
        ],
        "targets": [{"name": "test", "start": 0x31000000, "end": 0x31000004}],
    }


class FrontierTest(unittest.TestCase):
    def test_optimistic_restart_preserves_state_and_is_nonqualifying(self):
        unsupported = bytes.fromhex("3e02")
        loaded = memory(unsupported + bytes.fromhex("f29f"))
        paths = F.run(
            loaded,
            manifest(unsupported),
            "optimistic-preserve",
            max_islands=1,
            max_steps=1,
        )
        self.assertEqual(len(paths), 1)
        state = paths[0].state
        self.assertEqual(state.uregs[0], T.Const(7))
        self.assertEqual(paths[0].used, (0x10,))
        restart = next(
            event for event in state.trace if event["action"] == "exploratory-restart"
        )
        self.assertFalse(restart["qualifying"])
        self.assertEqual(restart["skipped_bytes"], "3e02")
        summary = F.report(
            paths, manifest(unsupported), "optimistic-preserve", "0" * 64, 1, 1
        )
        self.assertFalse(summary["qualifying"])
        self.assertEqual(summary["state_count"], 1)
        self.assertEqual(summary["endpoints"][0]["restart_count"], 1)

    def test_conservative_restart_clobbers_registers_and_data_memory(self):
        unsupported = bytes.fromhex("3e02")
        loaded = memory(unsupported + bytes.fromhex("f29f"))
        path = F.run(
            loaded,
            manifest(unsupported),
            "conservative-clobber",
            max_islands=1,
            max_steps=1,
        )[0]
        self.assertIsInstance(path.state.uregs[0], T.Unknown)
        self.assertTrue(path.state.data_memory_tainted)
        self.assertIs(path.state.concrete, loaded)

    def test_restart_hash_must_match_exact_loader_bytes(self):
        unsupported = bytes.fromhex("3e02")
        loaded = memory(unsupported + bytes.fromhex("f29f"))
        document = manifest(b"wrong")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            F.run(
                loaded,
                document,
                "optimistic-preserve",
                max_islands=1,
                max_steps=1,
            )

    def test_hard_caps_cannot_be_relaxed(self):
        loaded = memory(bytes.fromhex("f29f"))
        document = {"version": 1, "start_sw": 0x10}
        with self.assertRaisesRegex(ValueError, "between 0 and 3"):
            F.run(loaded, document, "optimistic-preserve", max_islands=4)
        with self.assertRaisesRegex(ValueError, "between 1 and 50000"):
            F.run(loaded, document, "optimistic-preserve", max_steps=50_001)

        oversized = manifest(bytes.fromhex("3e02"), successor=0x10 + 2049)
        with self.assertRaisesRegex(ValueError, "exceeds 4096 bytes"):
            F.run(loaded, oversized, "optimistic-preserve")

    def test_report_bounds_peripheral_access_samples(self):
        state = T.State(0x20, stopped="synthetic")
        state.trace = [
            {
                "pc_sw": 0x10 + index,
                "form": "14a",
                "action": "load",
                "address": 0x31400,
                "concrete_value": index,
            }
            for index in range(20)
        ]
        result = F.report(
            [F.Path(state)],
            {"version": 1, "start_sw": 0x10},
            "optimistic-preserve",
            "0" * 64,
            3,
            50_000,
        )
        accesses = result["endpoints"][0]["peripheral_accesses"]
        self.assertEqual(accesses["count"], 20)
        self.assertEqual(len(accesses["samples"]), F.MAX_ACCESS_SAMPLES)
        self.assertTrue(accesses["truncated"])


if __name__ == "__main__":
    unittest.main()
