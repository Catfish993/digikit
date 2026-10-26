"""Score and working names of tools/coldfire/atlas.py catalog, on synthetic inputs."""

import unittest

from tools.coldfire import atlas


class ScoreTest(unittest.TestCase):
    def test_score_adds_terms(self) -> None:
        s = atlas.catalog_score(1, "strict", 32, 2, ["near:cf.menu", "role:model"], [])
        self.assertAlmostEqual(s, 4 + 2 + 2 + 1.5 + 1 + 0.5)

    def test_score_caps_ui_callers_and_penalises_anchored(self) -> None:
        s = atlas.catalog_score(0, None, 1000, 0, [], ["cf.menu.x"])
        self.assertAlmostEqual(s, 64 / 16 - 3)


class WorkNameTest(unittest.TestCase):
    def test_named_function_has_no_work_name(self) -> None:
        self.assertEqual(atlas.work_name("Foo::bar", {"C": 3}, "hint", ["x"]), "")

    def test_single_class_with_two_callers(self) -> None:
        self.assertEqual(
            atlas.work_name("FUN_0012abcd", {"Menu": 2}, "hint", ["x"]),
            "Menu::helper_abcd",
        )
        self.assertEqual(
            atlas.work_name("thunk_FUN_0012ABCD", {"Menu": 5}, "", []),
            "Menu::helper_abcd",
        )

    def test_one_caller_or_two_classes_fall_through_to_hint(self) -> None:
        text = "A very long UI string"
        want = "str:A very long UI s"[: 4 + atlas.HINT_NAME_CHARS]
        self.assertEqual(atlas.work_name("FUN_00000010", {"A": 1}, text, ["x"]), want)
        self.assertEqual(
            atlas.work_name("FUN_00000010", {"A": 2, "B": 2}, text, ["x"]), want
        )

    def test_callee_then_empty(self) -> None:
        self.assertEqual(
            atlas.work_name("FUN_00000010", {}, "", ["cf.menu.open", "y"]),
            "calls:cf.menu.open",
        )
        self.assertEqual(atlas.work_name("FUN_00000010", {}, "", []), "")


if __name__ == "__main__":
    unittest.main()
