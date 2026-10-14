"""Tests for tools/test_outcomes.py (no firmware needed)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import test_outcomes  # noqa: E402


def case(classname: str, name: str, child: str = "") -> str:
    if not child:
        return f'<testcase classname="{classname}" name="{name}"/>'
    return f'<testcase classname="{classname}" name="{name}">{child}</testcase>'


def skip(message: str) -> str:
    return f'<skipped message="{message}">x</skipped>'


class OutcomesTest(unittest.TestCase):
    def run_cmp(self, before: list[str], after: list[str], *extra: str) -> int:
        with TemporaryDirectory() as tmp:
            paths = []
            for label, cases in (("b", before), ("a", after)):
                p = Path(tmp) / f"{label}.xml"
                body = "".join(cases)
                p.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
                paths.append(str(p))
            return test_outcomes.main([*paths, *extra])

    def test_same(self) -> None:
        cases = [case("tests.test_a.T", "test_x"), case("tests.test_a", "test_y")]
        self.assertEqual(self.run_cmp(cases, cases), 0)

    def test_passed_to_skipped(self) -> None:
        b = [case("tests.test_a", "test_x")]
        a = [case("tests.test_a", "test_x", skip("tests/test_a.py:1: no"))]
        self.assertEqual(self.run_cmp(b, a), 1)

    def test_rename(self) -> None:
        b = [case("tests.test_a.T", "test_x")]
        a = [case("tests.sharc.test_a.T", "test_x")]
        self.assertEqual(self.run_cmp(b, a), 1)
        rename = "tests/test_a.py=tests/sharc/test_a.py"
        self.assertEqual(self.run_cmp(b, a, "--rename", rename), 0)

    def test_skip_prefix_ignored(self) -> None:
        b = [case("tests.test_a", "test_x", skip("tests/test_a.py:42: no image"))]
        a = [case("tests.test_a", "test_x", skip("tests/s/test_a.py:7: no image"))]
        self.assertEqual(self.run_cmp(b, a), 0)

    def test_reason_change(self) -> None:
        b = [case("tests.test_a", "test_x", skip("tests/test_a.py:42: no image"))]
        a = [case("tests.test_a", "test_x", skip("tests/test_a.py:42: bad path"))]
        self.assertEqual(self.run_cmp(b, a), 1)

    def test_missing(self) -> None:
        b = [case("tests.test_a", "test_x"), case("tests.test_a", "test_y")]
        a = [case("tests.test_a", "test_x")]
        self.assertEqual(self.run_cmp(b, a), 1)

    def test_allow_new(self) -> None:
        b = [case("tests.test_a", "test_x")]
        a = [*b, case("tests.test_a", "test_new")]
        self.assertEqual(self.run_cmp(b, a), 1)
        self.assertEqual(self.run_cmp(b, a, "--allow-new"), 0)

    def test_error_wins(self) -> None:
        b = [case("tests.test_a", "test_x")]
        a = [case("tests.test_a", "test_x", "<error message='m'/>")]
        self.assertEqual(self.run_cmp(b, a), 1)

    def test_unreadable(self) -> None:
        self.assertEqual(test_outcomes.main(["/nonexistent/a", "/nonexistent/b"]), 2)


if __name__ == "__main__":
    unittest.main()
