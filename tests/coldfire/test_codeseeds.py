"""tools/coldfire/codeseeds.py on a hand-assembled image."""

import unittest


from tools.coldfire import codeseeds  # noqa: E402

BASE = 0x40000400
IMAGE = bytes.fromhex(
    '203c40000420'   # 0x400 move.l #$40000420,d0
    '23c0400002fc'   # 0x406 move.l d0,$400002fc.l  (vector 191)
    '4eb940000420'   # 0x40c jsr $40000420.l
    '6100000c'       # 0x412 bsr.w $40000420
    '4e75'           # 0x416 rts
    '0000000000000000'
    '4e75'           # 0x420 rts
)


class CodeseedsTest(unittest.TestCase):
    def test_calls_and_vector(self):
        report = codeseeds.scan(IMAGE, BASE, [(BASE, BASE + len(IMAGE))])
        self.assertEqual(report['vectors'],
                         [{'site': 0x40000406, 'handler': 0x40000420, 'vector': 191}])
        self.assertEqual(report['calls'],
                         [{'site': 0x4000040c, 'target': 0x40000420},
                          {'site': 0x40000412, 'target': 0x40000420}])

    def test_target_outside_code_is_ignored(self):
        report = codeseeds.scan(IMAGE, BASE, [(BASE, 0x40000418)])
        self.assertEqual(report['calls'], [])
        self.assertEqual(report['vectors'], [])


if __name__ == '__main__':
    unittest.main()
