"""Row selection of tools/firmware/catalog_page.py, on synthetic inputs."""

import unittest

from tools.firmware import catalog_page


def row(addr: int, score: float, anchors: str = "") -> dict[str, str]:
    return {"addr": hex(addr), "score": str(score), "anchors": anchors}


class PickRowsTest(unittest.TestCase):
    def test_labelled_first_then_score_skipping_anchored(self) -> None:
        members = {
            1: [
                row(0x10, 1.0),
                row(0x20, 5.0),
                row(0x30, 9.0, anchors="cf.x"),
                row(0x40, 3.0),
            ],
            2: [row(0x50, 2.0)],
        }
        picked = catalog_page.pick_rows(members, {0x10}, 3)
        self.assertEqual([r["addr"] for r in picked], ["0x10", "0x20", "0x40", "0x50"])

    def test_labelled_rows_are_kept_over_the_cap(self) -> None:
        members = {1: [row(0x10, 1.0), row(0x20, 2.0), row(0x30, 3.0)]}
        picked = catalog_page.pick_rows(members, {0x10, 0x20}, 1)
        self.assertEqual([r["addr"] for r in picked], ["0x20", "0x10"])


class SelectGroupsTest(unittest.TestCase):
    def test_small_groups_kept_only_when_labelled(self) -> None:
        groups = [
            {"id": "1", "size": "10"},
            {"id": "2", "size": "3"},
            {"id": "3", "size": "3"},
        ]
        keep, small = catalog_page.select_groups(groups, {3}, 10)
        self.assertEqual([g["id"] for g in keep], ["1", "3"])
        self.assertEqual(small, {3})


if __name__ == "__main__":
    unittest.main()
