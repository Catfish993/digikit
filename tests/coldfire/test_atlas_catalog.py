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


if __name__ == "__main__":
    unittest.main()
