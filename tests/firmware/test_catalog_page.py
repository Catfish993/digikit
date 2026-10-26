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


if __name__ == "__main__":
    unittest.main()
