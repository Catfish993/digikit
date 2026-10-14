"""tools/sharc_dev/dspmap.py regions and caller trees (the map needs an image and a dump)."""

import unittest


from tools.sharc_dev import dspmap  # noqa: E402

PROF = {'counter': 0x40001000, 'gate': 0x40002004, 'countdown': 0x40002000,
        'mode': 0x40002008, 'stop': 0x40001ffc,
        'tables': ((0x80003340, 0x9a, 16, 'track_9a'), (0x80005348, 0x802, 1, 'tx_frame'))}


class RegionTest(unittest.TestCase):
    def test_locate(self):
        regs = dspmap.regions(PROF)
        self.assertEqual(dspmap.locate(regs, 0x80003340 + 2 * 0x9a + 5), ('track_9a', 2, 5))
        self.assertEqual(dspmap.locate(regs, 0x80005358), ('tx_frame', 0, 0x10))
        self.assertEqual(dspmap.locate(regs, 0x40002004), ('gate', 0, 0))
        self.assertIsNone(dspmap.locate(regs, 0x80003340 + 16 * 0x9a))
        self.assertIsNone(dspmap.locate(regs, 0x80000000))


class CallerTest(unittest.TestCase):
    def test_two_levels_and_update_mirror(self):
        names = {0: 'a', 1: 'Kit::updateMirror', 2: 'b', 3: 'c'}
        callers = {3: {2}, 2: {1}, 1: {0}}
        tree = dspmap.caller_tree(3, names, callers, 2)
        self.assertEqual(tree, [{'entry': '0x00000002', 'name': 'b', 'callers': [
            {'entry': '0x00000001', 'name': 'Kit::updateMirror', 'callers': []}]}])
        self.assertEqual(dspmap.mirror_names('c', tree), ['Kit::updateMirror'])
        self.assertEqual(dspmap.caller_tree(None, names, callers, 2), [])

    def test_summarize(self):
        sites = [{'function': {'entry': '0x00000010', 'name': 'f'}, 'region': 'gate',
                  'update_mirror': []},
                 {'function': {'entry': '0x00000010', 'name': 'f'}, 'region': 'mode',
                  'update_mirror': ['Kit::updateMirror']},
                 {'function': None, 'region': 'stop', 'update_mirror': []}]
        self.assertEqual(dspmap.summarize(sites), [
            {'entry': None, 'name': None, 'sites': 1, 'regions': ['stop'], 'update_mirror': []},
            {'entry': '0x00000010', 'name': 'f', 'sites': 2, 'regions': ['gate', 'mode'],
             'update_mirror': ['Kit::updateMirror']}])


if __name__ == '__main__':
    unittest.main()
