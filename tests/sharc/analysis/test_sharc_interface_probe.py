import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.sharc_dev import (
    sharc_interface_probe as P,  # pyright: ignore[reportMissingImports]
)

BLOB = pathlib.Path("out/sections/dt2-1.16/section_7_BLOB.bin")


class SharcInterfaceProbeTest(unittest.TestCase):
    def test_extract_machine_types(self):
        frame = bytearray(0x802)
        expected = [0, 1, 6, 0x1234] + list(range(4, 16))
        for track, value in enumerate(expected):
            start = P.MACHINE_OFFSET + 2 * track
            frame[start : start + 2] = value.to_bytes(2, "big")
        self.assertEqual(P.extract_machine_types(bytes(frame)), expected)

    def test_extract_machine_types_rejects_short_frame(self):
        for size in (P.TX_FRAME_BYTES - 1, P.TX_FRAME_BYTES + 1):
            with (
                self.subTest(size=size),
                self.assertRaisesRegex(ValueError, "expected exactly 2050"),
            ):
                P.extract_machine_types(bytes(size))

    def test_build_report_rejects_wrong_image_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            blob = pathlib.Path(directory) / "section_7_BLOB.bin"
            blob.write_bytes(b"not the qualifying image")
            with self.assertRaisesRegex(ValueError, "wrong SHARC image"):
                P.build_report(blob)

    def test_descriptor_probe_rejects_nonconcrete_template_write(self):
        specification = P.DMA_DESCRIPTOR_PROBES[0]
        state = SimpleNamespace(
            stopped="breakpoint",
            trace=[
                {
                    "action": "store",
                    "address": specification["base"],
                    "concrete_write": False,
                }
            ],
        )
        with (
            mock.patch.object(P.trace, "trace", return_value=[state]),
            self.assertRaisesRegex(ValueError, "non-concrete descriptor-template"),
        ):
            P._probe_dma_descriptor_list(mock.sentinel.memory, specification)

    @unittest.skipUnless(BLOB.exists(), "DT2 1.16 SHARC loader is not available")
    def test_build_report_recovers_four_ping_pong_descriptor_lists(self):
        report = P.build_report(BLOB)
        probes = report["calibrated_dma_descriptor_probes"]
        self.assertEqual(
            [item["buffer_bytes"] for item in probes], [256, 256, 2048, 2048]
        )
        self.assertEqual(
            [item["list_head"] for item in probes],
            [0x2620C8, 0x262100, 0x264138, 0x264170],
        )
        self.assertEqual(
            [descriptor["start_address"] for descriptor in probes[2]["descriptors"]],
            [0x262138, 0x262938],
        )
        self.assertTrue(
            all(item["calibration"]["seeded_values"]["R11"] == 0 for item in probes)
        )
        self.assertTrue(all(item["qualifying"] is False for item in probes))
        self.assertEqual(
            report["interface_boundaries"]["dma_descriptor_lists"]["dma10_selection"],
            "not proven",
        )
        marker = report["calibrated_marker_writer_probe"]
        self.assertEqual(marker["store_address"], 0x262138)
        self.assertEqual(marker["stored_value"], 0x7FFFFFFF)
        self.assertIn("not proven", marker["coldfire_marker_join"])


if __name__ == "__main__":
    unittest.main()
