"""tools/ghidradump.py helpers (the Ghidra parts need a project)."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(
    str(REPO_ROOT), 'tools'))

import ghidradump  # noqa: E402


def fact(name, stem, refs=()):
    return {'name': name, 'namespace': 'Global', 'size': 2,
            'signature': 'void %s(void)' % name, 'ranges': [(0, 1)],
            'stem': stem, 'refs': list(refs)}


def ref(site, to, kind, call=False, flow=False):
    return {'site': site, 'to': to, 'kind': kind, 'call': call, 'flow': flow}


A, B, C = 0x40001000, 0x40002000, 0x40003000
NOWHERE, STRING, MMIO, GLOBAL = 0x40009999, 0x40250000, 0xfc0a4000, 0x4094e4f4

FUNCS = {
    A: fact('A', '40001000_A', [
        ref(A + 2, B, 'UNCONDITIONAL_CALL', call=True, flow=True),
        ref(A + 6, NOWHERE, 'UNCONDITIONAL_CALL', call=True, flow=True),
        ref(A + 10, A + 2, 'CONDITIONAL_JUMP', flow=True),
        ref(A + 14, STRING, 'DATA'),
        ref(A + 18, MMIO, 'WRITE'),
        ref(A + 22, GLOBAL, 'READ'),
    ]),
    B: fact('Class::b', '40002000_Class__b', [
        ref(B + 4, C, 'UNCONDITIONAL_JUMP', flow=True),
    ]),
    C: fact('C', '40003000_C'),
}
TARGETS = {
    STRING: {'label': 's_hello', 'block': 'ram', 'string': 'hello'},
    MMIO: {'label': 'MCF5441X_MMIO::DSPI2_PUSHR', 'block': 'MCF5441X_fc0a4000', 'string': None},
    GLOBAL: {'label': 'DAT_4094e4f4', 'block': 'ram', 'string': None},
}


class FileStemTest(unittest.TestCase):
    def test_namespace_separator(self):
        self.assertEqual(ghidradump.file_stem(0x4002d652, 'VoiceConfig::updateMirror'),
                         '4002d652_VoiceConfig__updateMirror')

    def test_template_characters(self):
        self.assertEqual(ghidradump.file_stem(0x4002d652, 'Value<unsigned int>::f'),
                         '4002d652_Value_unsigned_int___f')

    def test_long_names_are_capped(self):
        stem = ghidradump.file_stem(0x4002d652, 'x' * 1000)
        self.assertEqual(len(stem), ghidradump.MAX_STEM)
        self.assertTrue(stem.startswith('4002d652_x'))

    def test_empty_name(self):
        self.assertEqual(ghidradump.file_stem(0x4002d652, ''), '4002d652_unnamed')


class DisasmLineTest(unittest.TestCase):
    def test_instruction_column_is_fixed(self):
        short = ghidradump.disasm_line(0x4002d652, bytes.fromhex('4e75'), 'rts')
        longest = ghidradump.disasm_line(0x4002d654, bytes(10), 'move.l #0x0,(0x0).l')
        self.assertEqual(short[36:], 'rts')
        self.assertEqual(longest[36:], 'move.l #0x0,(0x0).l')
        self.assertEqual(short.split()[:2], ['4002d652', '4e75'])

    def test_bytes_are_grouped_in_words(self):
        line = ghidradump.disasm_line(0, bytes.fromhex('4e560000'), 'link.w A6,#0x0')
        self.assertEqual(line.split()[1:3], ['4e56', '0000'])


class RowsTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.data_refs = ghidradump.build_rows(FUNCS, TARGETS)

    def test_calls_include_unresolved_targets_and_tail_calls(self):
        self.assertEqual(self.calls, [
            (A, B, B, A + 2, 'UNCONDITIONAL_CALL'),
            (A, None, NOWHERE, A + 6, 'UNCONDITIONAL_CALL'),
            (B, C, C, B + 4, 'UNCONDITIONAL_JUMP'),
        ])

    def test_data_refs_carry_label_and_block(self):
        self.assertEqual(self.data_refs, [
            (A + 14, A, STRING, 'DATA', 's_hello', 'ram'),
            (A + 18, A, MMIO, 'WRITE', 'MCF5441X_MMIO::DSPI2_PUSHR', 'MCF5441X_fc0a4000'),
            (A + 22, A, GLOBAL, 'READ', 'DAT_4094e4f4', 'ram'),
        ])

    def test_records_split_strings_mmio_and_globals(self):
        status = ghidradump.decomp_status(FUNCS, {A: '40001000_A'}, {})
        records = {r['entry']: r for r in ghidradump.function_records(
            FUNCS, self.calls, self.data_refs, TARGETS, status)}
        a = records['0x40001000']
        self.assertEqual(a['callees'], [{'entry': '0x40002000', 'name': 'Class::b'}])
        self.assertEqual(a['strings'], [{'addr': '0x40250000', 'text': 'hello'}])
        self.assertEqual([m['addr'] for m in a['mmio']], ['0xfc0a4000'])
        self.assertEqual([g['addr'] for g in a['globals']], ['0x4094e4f4'])
        self.assertEqual(a['decomp'], 'decomp/40001000_A.c')
        self.assertEqual(a['disasm'], 'disasm/40001000_A.s')
        self.assertEqual(records['0x40003000']['callers'],
                         [{'entry': '0x40002000', 'name': 'Class::b'}])
        json.dumps(list(records.values()))


class DecompFilesTest(unittest.TestCase):
    def test_existing_files_and_staleness(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('40001000_A.c', '40002000_OldName.c', 'notes.txt', '4000300_short.c'):
                open(os.path.join(d, name), 'w').close()
            files = ghidradump.existing_decomp(d)
            self.assertEqual(files, {A: '40001000_A', B: '40002000_OldName'})
            status = ghidradump.decomp_status(FUNCS, files, {C: 'timeout'})
        self.assertEqual(status[A], {'path': 'decomp/40001000_A.c', 'error': None, 'stale': False})
        self.assertTrue(status[B]['stale'])
        self.assertEqual(status[C], {'path': None, 'error': 'timeout', 'stale': False})

    def test_missing_directory(self):
        self.assertEqual(ghidradump.existing_decomp('/nonexistent/decomp'), {})


class PriorTest(unittest.TestCase):
    def test_refuses_a_directory_that_is_not_a_dump(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, 'something'), 'w').close()
            with self.assertRaises(SystemExit):
                ghidradump.read_prior(d, partial=False)

    def test_partial_needs_an_existing_dump(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ghidradump.read_prior(d, partial=False))
            with self.assertRaises(SystemExit):
                ghidradump.read_prior(d, partial=True)

    def test_reads_the_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            ghidradump.write_json(os.path.join(d, 'manifest.json'), {'program': '/x'})
            self.assertEqual(ghidradump.read_prior(d, partial=True), {'program': '/x'})


class SqliteTest(unittest.TestCase):
    def test_round_trip(self):
        calls, data_refs = ghidradump.build_rows(FUNCS, TARGETS)
        status = ghidradump.decomp_status(FUNCS, {A: '40001000_A'}, {})
        rtti = {'typeinfos': [{'addr': 0x40210000, 'kind': '__class_type_info', 'name': 'X',
                               'mangled': '1X', 'name_addr': 0x40210010, 'bases': []}],
                'vtables': [{'addr': 0x40220000, 'class': 'X', 'typeinfo': 0x40210000,
                             'offset_to_top': 0, 'slots': [A, B], 'owners': ['X', 'X']}]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'xrefs.sqlite')
            ghidradump.write_sqlite(path, FUNCS, status, calls, data_refs,
                                    [(STRING, 'hello')], [(A, 'A', 'Global', 'Function', 'DEFAULT', 1)],
                                    [('ram', 0x40000000, 0x4fffffff, 1, 'DEFAULT')], rtti)
            self.assertFalse(os.path.exists(path + '.tmp'))
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute(
                    'SELECT f.name FROM data_refs d JOIN functions f ON f.entry = d.func '
                    'WHERE d.to_addr BETWEEN 0x4094e4f0 AND 0x4094e4ff').fetchall(), [('A',)])
                self.assertEqual(db.execute(
                    'SELECT count(*) FROM calls WHERE to_func IS NULL').fetchone(), (1,))
                self.assertEqual(db.execute(
                    'SELECT slot FROM vtables WHERE target = ?', (B,)).fetchall(), [(1,)])
                self.assertEqual(db.execute(
                    'SELECT decomp, decomp_error FROM functions WHERE entry = ?', (C,)).fetchone(),
                    (None, None))
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
