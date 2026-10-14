"""tools/coldfire/rttiscan.py on a hand-built image with two classes."""

import struct
import unittest


from tools.coldfire import rttiscan  # noqa: E402

BASE = 0x1000
CODE = [(0x1000, 0x1100)]
F1, F2, F3 = 0x1010, 0x1020, 0x1030


def build():
    data = bytearray(0x100)

    def put(blob):
        while len(data) % 4:
            data.append(0)
        addr = BASE + len(data)
        data.extend(blob)
        return addr

    s_class = put(b'N10__cxxabiv117__class_type_infoE\0')
    s_si = put(b'N10__cxxabiv120__si_class_type_infoE\0')
    s_base = put(b'4Base\0')
    s_derived = put(b'7Derived\0')
    s_run = put(b'Derived::run\0')
    kt_class = put(struct.pack('>II', 0, s_class))
    kt_si = put(struct.pack('>II', 0, s_si))
    kv_class = put(struct.pack('>III', 0, kt_class, 0))
    kv_si = put(struct.pack('>III', 0, kt_si, 0))
    tb = put(struct.pack('>II', kv_class + 8, s_base))
    td = put(struct.pack('>III', kv_si + 8, s_derived, tb))
    vb = put(struct.pack('>IIIII', 0, tb, F1, F2, 0))
    vd = put(struct.pack('>IIIII', 0, td, F1, F3, 0))
    # move.l #vd+8,(a0); pea.l s_run.l; rts
    data[0:14] = (bytes.fromhex('20bc') + struct.pack('>I', vd + 8)
                  + bytes.fromhex('4879') + struct.pack('>I', s_run) + bytes.fromhex('4e75'))
    return bytes(data), {'tb': tb, 'td': td, 'vb': vb, 'vd': vd, 's_run': s_run}


def strip_length(names):
    return {n: n.lstrip('0123456789') for n in names}


class RttiscanTest(unittest.TestCase):
    def setUp(self):
        data, self.at = build()
        self.report = rttiscan.scan(data, BASE, CODE, demangle=strip_length)

    def test_typeinfos(self):
        by_name = {ti['name']: ti for ti in self.report['typeinfos']}
        self.assertEqual(set(by_name), {'Base', 'Derived'})
        self.assertEqual(by_name['Derived']['kind'], 'si_class')
        self.assertEqual(by_name['Derived']['bases'], [self.at['tb']])

    def test_vtables_and_owners(self):
        vtables = {vt['addr']: vt for vt in self.report['vtables']}
        self.assertEqual(set(vtables), {self.at['vb'], self.at['vd']})
        self.assertEqual(vtables[self.at['vd']]['slots'], [F1, F3])
        self.assertEqual(vtables[self.at['vd']]['owners'], ['Base', 'Derived'])

    def test_code_references(self):
        self.assertEqual(self.report['vtable_refs'],
                         [{'site': 0x1000, 'vtable': self.at['vd'], 'class': 'Derived'}])
        self.assertEqual(self.report['qualified_strings'],
                         [{'addr': self.at['s_run'], 'text': 'Derived::run', 'sites': [0x1006]}])


if __name__ == '__main__':
    unittest.main()
