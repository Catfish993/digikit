"""Tests for tools/devtools/test_outcomes.py (no firmware needed)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.devtools import test_outcomes  # noqa: E402


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

    def test_split_class_change(self) -> None:
        b = [case("tests.test_a.TraceTest", n) for n in ("test_a", "test_b")]
        a = [
            case("tests.test_n1.AClass", "test_a"),
            case("tests.test_n2.BClass", "test_b"),
        ]
        split = "tests/test_a.py=tests/test_n1.py,tests/test_n2.py"
        self.assertEqual(self.run_cmp(b, a), 1)
        self.assertEqual(self.run_cmp(b, a, "--split", split), 0)

    def test_split_missing(self) -> None:
        b = [case("tests.test_a.TraceTest", n) for n in ("test_a", "test_b")]
        a = [case("tests.test_n1.AClass", "test_a")]
        split = "tests/test_a.py=tests/test_n1.py,tests/test_n2.py"
        self.assertEqual(self.run_cmp(b, a, "--split", split), 1)

    def test_split_duplicate_name_by_class(self) -> None:
        y = case("tests.test_n2.Y", "test_d", skip("s"))
        b = [
            case("tests.test_a.X", "test_d"),
            case("tests.test_a.Y", "test_d", skip("s")),
        ]
        a = [y, case("tests.test_n1.X", "test_d")]
        split = "tests/test_a.py=tests/test_n1.py,tests/test_n2.py"
        self.assertEqual(self.run_cmp(b, a, "--split", split), 0)
        a2 = [case("tests.test_n1.Z", "test_d"), y]
        self.assertEqual(self.run_cmp(b, a2, "--split", split), 1)

    def test_unreadable(self) -> None:
        self.assertEqual(test_outcomes.main(["/nonexistent/a", "/nonexistent/b"]), 2)


class RunModeTest(unittest.TestCase):
    def test_slug_and_dotted(self) -> None:
        self.assertEqual(
            test_outcomes.slug_of("tests/sharc/test_a.py"), "tests__sharc__test_a"
        )
        self.assertEqual(
            test_outcomes.dotted_of("tests/sharc/test_a.py"), "tests.sharc.test_a"
        )

    def test_expand_skips_support(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("tests/test_b.py", "tests/test_a.py", "tests/x/test_c.py",
                        "tests/support/test_s.py", "tests/helper.py"):  # fmt: skip
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("")
            got = test_outcomes.expand_paths(root, ["tests", "tests/test_a.py"])
            self.assertEqual(
                got, ["tests/test_a.py", "tests/test_b.py", "tests/x/test_c.py"]
            )

    def test_end_to_end(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            out = Path(tmp) / "out"
            root.mkdir()
            (root / "test_ok.py").write_text("def test_a():\n    pass\n")
            (root / "test_skip.py").write_text(
                "import pytest\n\n\ndef test_s():\n    pytest.skip('no')\n"
            )
            (root / "test_hang.py").write_text(
                "import time\n\n\ndef test_h():\n    time.sleep(30)\n"
            )
            rc = test_outcomes.main(
                ["run", "--out", str(out), "--root", str(root), "--timeout", "2", "."]
            )
            self.assertEqual(rc, 0)
            all_xml = out / "all.xml"
            self.assertTrue(all_xml.exists())
            self.assertTrue((out / "summary.tsv").exists())
            self.assertEqual(test_outcomes.main([str(all_xml), str(all_xml)]), 0)
            res = test_outcomes.read_outcomes(all_xml)
            self.assertEqual(res[("test_hang.py", "::__file__")][0], "error")
            self.assertEqual(res[("test_ok.py", "::test_a")][0], "passed")
            self.assertEqual(res[("test_skip.py", "::test_s")][0], "skipped")
            self.assertIn("TIMEOUT", all_xml.read_text())

    def test_check(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "test_ok.py").write_text("def test_a():\n    pass\n")
            args = ["--root", str(root), "--check", "."]
            rc = test_outcomes.main(["run", "--out", str(Path(tmp) / "o1"), *args])
            self.assertEqual(rc, 0)
            (root / "test_bad.py").write_text("def test_b():\n    assert False\n")
            rc = test_outcomes.main(["run", "--out", str(Path(tmp) / "o2"), *args])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
