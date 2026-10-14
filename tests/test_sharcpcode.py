"""Synthetic SHARC p-code measurement command coverage."""

import os
import sys
import unittest
from importlib import import_module
from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(str(REPO_ROOT), "tools"))
P = import_module("sharcpcode")


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


if __name__ == "__main__":
    unittest.main()
