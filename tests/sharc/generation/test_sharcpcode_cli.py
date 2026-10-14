"""Synthetic SHARC p-code measurement command coverage."""

import os
import sqlite3
import sys
import unittest
from importlib import import_module

from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(str(REPO_ROOT), "tools"))
P = import_module("sharcpcode")
from support.sharc_encode import encode  # noqa: E402
from support.sharc_pcode_build import branch  # noqa: E402

import sharcpcode  # noqa: E402


class SharcImportArgumentsTest(unittest.TestCase):
    def test_dt2_115c_image_uses_loader_entry_base(self):
        spec = P.IMAGES["dt2-1.15C"]
        self.assertEqual(spec["blob"], "out/sections/dt2-1.15C/section_7_BLOB.bin")
        self.assertEqual(spec["region"], "out/sharc/dt2-1.15C-main.bin")
        self.assertEqual(spec["base_sw"], 0x1C1338)
        self.assertEqual(spec["label_tables"], ["16b8a4:4"])

    def test_dt2_label_table_is_configured_and_forwarded(self):
        spec = P.IMAGES["dt2-1.16"]
        self.assertEqual(spec["label_tables"], ["16b8ac:4"])
        argv = P.sharc_import_args("blob", "dt2_SHARC", "project", spec)
        self.assertEqual(
            argv,
            [
                sys.executable,
                os.path.join(P.TOOLS, "sharc_import.py"),
                "blob",
                "--name",
                "dt2_SHARC",
                "--project",
                "project",
                "--project-name",
                P.PROJECT_NAME,
                "--label-table",
                "16b8ac:4",
                "--seed-calls",
                "--analyze",
                "--overwrite",
            ],
        )

    def test_dn2_import_arguments_adds_pcg_label_table(self):
        argv = P.sharc_import_args("blob", "dn2_SHARC", "project", P.IMAGES["dn2-1.11"])
        position = argv.index("--label-table")
        self.assertEqual(argv[position + 1], "16e9e8:4")


def image_record(**lift):
    rec = {
        "region_sha256": "a",
        "lift": {
            "aligned": 10,
            "decoded": 10,
            "undecoded": {},
            "length_mismatch": {},
            "conditional_without_fallthrough": {},
            "forms": {},
            "ops_total": 0,
            "instructions_per_second": 1000,
        },
    }
    rec["lift"].update(lift)
    return rec


class Compare(unittest.TestCase):
    def test_same_run_has_no_regression(self):
        self.assertEqual(
            sharcpcode.compare_image(image_record(), image_record(), 0.25)[0], []
        )

    def test_fewer_decoded_instructions(self):
        reg, _ = sharcpcode.compare_image(image_record(), image_record(decoded=9), 0.25)
        self.assertEqual(len(reg), 1)

    def test_new_conditional_flow_without_fallthrough(self):
        new = image_record(
            conditional_without_fallthrough={"11c": {"count": 2, "examples": []}}
        )
        reg, _ = sharcpcode.compare_image(image_record(), new, 0.25)
        self.assertEqual(len(reg), 1)

    def test_slower_lifting_beyond_tolerance(self):
        reg, _ = sharcpcode.compare_image(
            image_record(), image_record(instructions_per_second=700), 0.25
        )
        self.assertEqual(len(reg), 1)
        reg, notes = sharcpcode.compare_image(
            image_record(), image_record(instructions_per_second=900), 0.25
        )
        self.assertEqual(reg, [])

    def test_different_image_counts_are_notes(self):
        new = image_record(decoded=5)
        new["region_sha256"] = "b"
        self.assertEqual(sharcpcode.compare_image(image_record(), new, 0.25)[0], [])

    def test_probe_that_stops_passing(self):
        g = {
            "probes": [{"name": "p", "ok": True, "instructions": 3}],
            "decompile_failed": {},
        }
        old, new = image_record(), image_record()
        old["ghidra"] = g
        new["ghidra"] = dict(g, probes=[{"name": "p", "ok": False, "instructions": 3}])
        reg, _ = sharcpcode.compare_image(old, new, 0.25)
        self.assertEqual(reg, ['probe "p" stopped passing'])

    def test_lint_regressions(self):
        old = {
            "compiler": "pypcode",
            "returncode": 0,
            "compile": {"returncode": 0},
            "counts": {"nop": 47},
            "slaspec_sha256": "x",
        }
        new = dict(old, counts={"nop": 40, "dead_temp": 1}, slaspec_sha256="y")
        reg, notes = sharcpcode.compare_lint(old, new, 0.25)
        self.assertEqual(reg, ["dead_temp 0 -> 1"])
        self.assertIn("nop 47 -> 40", notes)


class Dump(unittest.TestCase):
    def test_field_value_assembles_and_sign_extends(self):
        self.assertEqual(
            sharcpcode.field_value({"addr[23:16]": 0x1C, "addr[15:0]": 0x1400}, "addr"),
            0x1C1400,
        )
        self.assertEqual(
            sharcpcode.field_value(
                {"reladdr[5:5]": 1, "reladdr[4:0]": 0x1F}, "reladdr", signed=True
            ),
            -1,
        )
        self.assertEqual(sharcpcode.field_value({"cond[4:0]": 8}, "cond"), 8)
        self.assertIsNone(sharcpcode.field_value({"b": 1}, "addr"))

    def test_decoder_rows(self):
        data = (
            encode("17b")
            + branch("8a_abs", cond=sharcpcode.COND_TRUE, b=0, target=0x1002)
            + encode("17b")
        )
        db = sqlite3.connect(":memory:")
        db.executescript(sharcpcode.SCHEMA)
        sharcpcode.write_decoder(db, None, data, 0x1000, min_depth=1)
        rows = dict(db.execute("SELECT sw, form FROM decoder WHERE aligned = 1"))
        self.assertEqual(rows, {0x1000: "17b", 0x1002: "8a_abs", 0x1005: "17b"})
        target, target_aligned, cond = db.execute(
            "SELECT target_sw, target_aligned, cond FROM decoder WHERE sw = 0x1002"
        ).fetchone()
        self.assertEqual(
            (target, target_aligned, cond), (0x1002, 1, sharcpcode.COND_TRUE)
        )


if __name__ == "__main__":
    unittest.main()
