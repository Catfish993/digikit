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


if __name__ == "__main__":
    unittest.main()
