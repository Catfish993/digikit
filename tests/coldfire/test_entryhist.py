"""tools/coldfire/entryhist.py on a hand-made function list."""

import json
import os
import tempfile
import unittest


from tools.coldfire import entryhist  # noqa: E402

BASE = 0x40000400
# (entry, end): two entries in bucket 0, one in bucket 1, none in 2-3, one in 4.
FUNCS = [(0x40000410, 0x4000045a), (0x40008000, 0x40008010),
         (0x40010400, 0x40010420), (0x40040500, 0x40040588)]


class EntryhistTest(unittest.TestCase):
    def test_histogram(self):
        self.assertEqual(entryhist.histogram(FUNCS, BASE, 0x10000), {0: 2, 1: 1, 4: 1})

    def test_ranges(self):
        self.assertEqual(entryhist.ranges(FUNCS, BASE, 0x10000), [
            {'aligned': (0x40000400, 0x40020400), 'exact': (0x40000410, 0x40010420),
             'functions': 3},
            {'aligned': (0x40040400, 0x40050400), 'exact': (0x40040500, 0x40040588),
             'functions': 1}])

    def test_min_drops_sparse_buckets(self):
        self.assertEqual(entryhist.ranges(FUNCS, BASE, 0x10000, minimum=2), [
            {'aligned': (0x40000400, 0x40010400), 'exact': (0x40000410, 0x40008010),
             'functions': 2}])

    def test_a_long_body_joins_the_runs_it_reaches(self):
        funcs = [(0x40000410, 0x40020500), (0x40020600, 0x40020610)]
        self.assertEqual(entryhist.ranges(funcs, BASE, 0x10000), [
            {'aligned': (0x40000400, 0x40030400), 'exact': (0x40000410, 0x40020610),
             'functions': 2}])

    def test_load_takes_the_end_from_the_body_ranges(self):
        rec = {'entry': '0x40000410',
               'ranges': [['0x40000410', '0x40000420'], ['0x40000500', '0x40000509']]}
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps(rec) + '\n\n')
        try:
            self.assertEqual(entryhist.load(f.name), [(0x40000410, 0x4000050a)])
        finally:
            os.unlink(f.name)


if __name__ == '__main__':
    unittest.main()
