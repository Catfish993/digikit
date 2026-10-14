"""tools/coldfire/vtcheck.py on hand-made matches, functions and images."""

import unittest


from tools.coldfire import vtcheck  # noqa: E402

BASE = 0x40000400


def func(name, lo, hi, callees=()):
    return {'name': name, 'ranges': [(lo, hi)], 'callees': set(callees)}


class AssociationsTest(unittest.TestCase):
    def test_accepted_functions_merge_correlators(self):
        report = {'matches': [
            {'type': 'Function', 'status': 'ACCEPTED', 'correlator': 'B',
             'source': '0x40000400', 'dest': '0x40000500'},
            {'type': 'Function', 'status': 'ACCEPTED', 'correlator': 'A',
             'source': '0x40000400', 'dest': '0x40000500'},
            {'type': 'Function', 'status': 'BLOCKED', 'correlator': 'A',
             'source': '0x40000404', 'dest': '0x40000504'},
            {'type': 'Data', 'status': 'ACCEPTED', 'correlator': 'A',
             'source': '0x40000408', 'dest': '0x40000508'}]}
        self.assertEqual(vtcheck.associations(report), {(0x40000400, 0x40000500): ['A', 'B']})


class CheckTest(unittest.TestCase):
    def setUp(self):
        # source: f at 0x400 (4 bytes) calls g at 0x404; dest: f at 0x408 calls g at 0x40c.
        self.src_image = bytes.fromhex('4e754e71' '4e750000')
        self.dst_image = bytes.fromhex('00000000' '00000000' '4e754e71' '4e750001')
        self.src = {0x40000400: func('Kit::vfunc_1', 0x40000400, 0x40000403, [0x40000404]),
                    0x40000404: func('FUN_40000404', 0x40000404, 0x40000407)}
        self.dst = {0x40000408: func('Kit::vfunc_1', 0x40000408, 0x4000040b, [0x4000040c]),
                    0x4000040c: func('FUN_4000040c', 0x4000040c, 0x4000040f)}
        self.fwd = {0x40000400: {0x40000408}, 0x40000404: {0x4000040c}}

    def run_check(self, key):
        return vtcheck.check(key, self.src, self.dst, self.src_image, self.dst_image, BASE,
                             self.fwd)

    def test_equal_bytes_agreeing_names_and_callees(self):
        r = self.run_check((0x40000400, 0x40000408))
        self.assertEqual((r['bytes'], r['names'], r['callees']), ('equal', 'agree', 1.0))

    def test_same_size_with_default_name(self):
        r = self.run_check((0x40000404, 0x4000040c))
        self.assertEqual((r['bytes'], r['diff_bytes'], r['names'], r['callees']),
                         ('same-size', 1, 'n/a', None))

    def test_switch_labels_are_default_names(self):
        self.assertTrue(vtcheck.is_default('switchD_4003a00c::caseD_a'))
        self.assertFalse(vtcheck.is_default('Kit::vfunc_1'))

    def test_missing_function(self):
        self.assertEqual(self.run_check((0x40000400, 0x40000500))['bytes'], 'no-function')


class SampleAndIndexTest(unittest.TestCase):
    def test_sample_takes_from_every_correlator_set(self):
        assoc = {(i, i): ['A'] for i in range(10)}
        assoc[(100, 100)] = ['B']
        picked = vtcheck.stratified_sample(assoc, 2, seed=1)
        self.assertIn((100, 100), picked)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked, vtcheck.stratified_sample(assoc, 2, seed=1))

    def test_containing(self):
        funcs = {0x100: {'ranges': [(0x100, 0x10f), (0x200, 0x203)]},
                 0x110: {'ranges': [(0x110, 0x11f)]}}
        index = vtcheck.range_index(funcs)
        self.assertEqual(vtcheck.containing(index, 0x10f), 0x100)
        self.assertEqual(vtcheck.containing(index, 0x110), 0x110)
        self.assertEqual(vtcheck.containing(index, 0x202), 0x100)
        self.assertIsNone(vtcheck.containing(index, 0x150))


if __name__ == '__main__':
    unittest.main()
