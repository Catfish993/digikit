"""Rule suggestion, grade and score of tools/coldfire/atlas.py catalog, on synthetic inputs."""

import unittest

from tools.coldfire import atlas


class SuggestRuleTest(unittest.TestCase):
    def test_unique_hash(self) -> None:
        self.assertEqual(
            atlas.suggest_rule(0x100, "h", [0x100], False), ("cf_func", None)
        )

    def test_few_same_hash_uses_address_order(self) -> None:
        group = [0x100, 0x200, 0x300]
        self.assertEqual(atlas.suggest_rule(0x300, "h", group, False), ("cf_func", 2))
        self.assertEqual(atlas.suggest_rule(0x100, "h", group, True), ("cf_func", 0))

    def test_many_same_hash(self) -> None:
        group = [0x100, 0x200, 0x300, 0x400, 0x500]
        self.assertEqual(
            atlas.suggest_rule(0x200, "h", group, True), ("vtable_slot", None)
        )
        self.assertEqual(atlas.suggest_rule(0x200, "h", group, False), ("none", None))


class GradeScoreTest(unittest.TestCase):
    def test_grade_needs_every_condition(self) -> None:
        self.assertEqual(atlas.catalog_grade("cf_func", 1, "strict", 1), 1)
        self.assertEqual(atlas.catalog_grade("cf_func", 1, "vtable", 1), 1)
        self.assertEqual(atlas.catalog_grade("none", 1, "strict", 1), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", 0, "strict", 1), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", None, "strict", 1), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", 1, "callee", 1), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", 1, None, 1), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", 1, "strict", 0), 0)
        self.assertEqual(atlas.catalog_grade("cf_func", 1, "strict", None), 0)

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


class NumberGroupsTest(unittest.TestCase):
    def test_order_by_size_then_address_and_singletons(self) -> None:
        got = atlas.number_groups(
            [{0x50}, {0x30, 0x40}, {0x10, 0x20, 0x60}, {0x05, 0x06}]
        )
        self.assertEqual(
            got,
            {0x10: 0, 0x20: 0, 0x60: 0, 0x05: 1, 0x06: 1, 0x30: 2, 0x40: 2, 0x50: -1},
        )


if __name__ == "__main__":
    unittest.main()
