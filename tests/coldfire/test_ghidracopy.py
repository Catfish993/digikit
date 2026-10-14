"""tools/coldfire/ghidracopy.py path splitting (the copy itself needs Ghidra projects)."""

import unittest


from tools.coldfire import ghidracopy  # noqa: E402


class SplitPathTest(unittest.TestCase):
    def test_nested(self):
        self.assertEqual(ghidracopy.split_path('/dt2-1.15C/section_3_MAIN_OS.bin'),
                         ('/dt2-1.15C', 'section_3_MAIN_OS.bin'))

    def test_top_level(self):
        self.assertEqual(ghidracopy.split_path('/a.bin'), ('/', 'a.bin'))
        self.assertEqual(ghidracopy.split_path('a.bin'), ('/', 'a.bin'))

    def test_folder_without_a_name(self):
        with self.assertRaises(ValueError):
            ghidracopy.split_path('/dt2-1.16/')


if __name__ == '__main__':
    unittest.main()
