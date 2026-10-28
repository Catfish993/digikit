"""Rule suggestion, grade and group numbering of tools/firmware/catalog.py, on synthetic inputs."""

import unittest

from tools.firmware import catalog


class SuggestRuleTest(unittest.TestCase):
    def test_unique_hash(self) -> None:
        self.assertEqual(catalog.suggest_rule(0x100, [0x100], False), ("cf_func", None))

    def test_few_same_hash_uses_address_order(self) -> None:
        group = [0x100, 0x200, 0x300]
        self.assertEqual(catalog.suggest_rule(0x300, group, False), ("cf_func", 2))
        self.assertEqual(catalog.suggest_rule(0x100, group, True), ("cf_func", 0))

    def test_many_same_hash(self) -> None:
        group = [0x100, 0x200, 0x300, 0x400, 0x500]
        self.assertEqual(
            catalog.suggest_rule(0x200, group, True), ("cf_vtable_slot", None)
        )
        self.assertEqual(catalog.suggest_rule(0x200, group, False), ("none", None))

    def test_sharc_func_kind(self) -> None:
        group = [0x100, 0x200, 0x300]
        self.assertEqual(
            catalog.suggest_rule(0x100, [0x100], False, "sharc_func"),
            ("sharc_func", None),
        )
        self.assertEqual(
            catalog.suggest_rule(0x200, group, False, "sharc_func"), ("sharc_func", 1)
        )
        self.assertEqual(
            catalog.suggest_rule(0x200, group * 2, False, "sharc_func"), ("none", None)
        )


class RenderEntryTest(unittest.TestCase):
    RULE = {"kind": "cf_func", "hash": "ab" * 16, "offset": 0x1C, "len": 4}

    def test_shared_rule(self) -> None:
        text = catalog.render_entry(
            "cf.draw.site.x",
            "site",
            "cf",
            {"dt2": self.RULE, "dn2": dict(self.RULE)},
            "A site.",
            "O",
        )
        self.assertEqual(
            text,
            '# A site.\n[[site]]\nname = "cf.draw.site.x"\ncpu = "cf"\n'
            f'rule = {{ kind = "cf_func", hash = "{"ab" * 16}", offset = 0x1c, len = 4 }}\n'
            'mark = "O"\n',
        )

    def test_rule_per_device(self) -> None:
        other = {**self.RULE, "ordinal": 1}
        text = catalog.render_entry(
            "cf.draw.site.x", "site", "cf", {"dt2": self.RULE, "dn2": other}, None, "O"
        )
        self.assertNotIn("\nrule = ", text)
        self.assertIn("rule.dn2 = {", text)
        self.assertIn("ordinal = 1", text)
        self.assertLess(text.index("rule.dn2"), text.index("rule.dt2"))

    def test_one_device(self) -> None:
        text = catalog.render_entry(
            "dsp.fx.fn.y", "fn", "sharc", {"dt2": self.RULE}, None, "O"
        )
        self.assertIn('devices = ["dt2"]\nrule = {', text)

    def test_insert_keeps_blocks_in_name_order(self) -> None:
        old = '# h1\n# h2\n\n[[fn]]\nname = "a.b.fn.a"\n\n# c\n[[fn]]\nname = "a.b.fn.c"\n'
        new = catalog.insert_entry(old, "a.b.fn.b", '[[fn]]\nname = "a.b.fn.b"\n')
        self.assertEqual(
            new,
            old.replace("# c\n", '[[fn]]\nname = "a.b.fn.b"\n\n# c\n'),
        )
        self.assertTrue(
            catalog.insert_entry(
                old, "a.b.fn.z", '[[fn]]\nname = "a.b.fn.z"\n'
            ).endswith('name = "a.b.fn.c"\n\n[[fn]]\nname = "a.b.fn.z"\n')
        )


class GradeTest(unittest.TestCase):
    def test_grade_needs_every_condition(self) -> None:
        self.assertEqual(catalog.catalog_grade("cf_func", 1, "strict", 1), 1)
        self.assertEqual(catalog.catalog_grade("sharc_func", 1, "strict", 1), 1)
        self.assertEqual(catalog.catalog_grade("cf_func", 1, "vtable", 1), 1)
        self.assertEqual(catalog.catalog_grade("none", 1, "strict", 1), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", 0, "strict", 1), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", None, "strict", 1), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", 1, "callee", 1), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", 1, None, 1), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", 1, "strict", 0), 0)
        self.assertEqual(catalog.catalog_grade("cf_func", 1, "strict", None), 0)


class NumberGroupsTest(unittest.TestCase):
    def test_order_by_size_then_address_and_singletons(self) -> None:
        got = catalog.number_groups(
            [{0x50}, {0x30, 0x40}, {0x10, 0x20, 0x60}, {0x05, 0x06}]
        )
        self.assertEqual(
            got,
            {0x10: 0, 0x20: 0, 0x60: 0, 0x05: 1, 0x06: 1, 0x30: 2, 0x40: 2, 0x50: -1},
        )


class ParseCountsTest(unittest.TestCase):
    def test_class_names_may_hold_commas(self) -> None:
        self.assertEqual(
            catalog.parse_counts("A<int,char>:2,B:1"), [("A<int,char>", 2), ("B", 1)]
        )
        self.assertEqual(catalog.parse_counts(""), [])


if __name__ == "__main__":
    unittest.main()
