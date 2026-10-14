"""tools/coldfire/ghidravt.py match summary and arguments (the run itself needs Ghidra)."""

import unittest

from tools.coldfire import ghidravt  # noqa: E402


class SummarizeTest(unittest.TestCase):
    def test_counts_by_correlator_and_status(self):
        matches = [
            {"correlator": "Exact Bytes", "status": "ACCEPTED"},
            {"correlator": "Exact Bytes", "status": "ACCEPTED"},
            {"correlator": "Exact Bytes", "status": "BLOCKED"},
            {"correlator": "Duplicate Function", "status": "AVAILABLE"},
        ]
        self.assertEqual(
            ghidravt.summarize(matches),
            {
                "Exact Bytes": {"ACCEPTED": 2, "BLOCKED": 1},
                "Duplicate Function": {"AVAILABLE": 1},
            },
        )


class ArgsTest(unittest.TestCase):
    def test_run_defaults(self):
        args = ghidravt.parse_args(
            [
                "run",
                "--project",
                "P",
                "--project-name",
                "N",
                "--session",
                "/vt/s",
                "--source",
                "/a",
                "--dest",
                "/b",
            ]
        )
        self.assertEqual(
            (args.heap, args.dupe_min_len, args.no_duplicates, args.replace, args.all),
            ("32G", 10, False, False, False),
        )

    def test_export_has_no_program_arguments(self):
        with self.assertRaises(SystemExit):
            ghidravt.parse_args(
                [
                    "export",
                    "--project",
                    "P",
                    "--project-name",
                    "N",
                    "--session",
                    "/vt/s",
                    "--source",
                    "/a",
                ]
            )


if __name__ == "__main__":
    unittest.main()
