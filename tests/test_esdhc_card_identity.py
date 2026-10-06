"""emu/esdhc.py card identities: EXT_CSD/CID bytes, the firmware whitelist rule,
and short backing images. Mirrors native/card/tests/card.rs."""

import struct
import unittest

import emu.esdhc as esdhc

# (mfr, name) -> (enh_size, wp_group, erase_group, capacity_mlc, capacity_slc)
WHITELIST = {
    (0x11, b"004GE0"): (0x1D8, 1, 8, 0x760000, 0x3B0000),
    (0x15, b"4FTE4R"): (0xE9, 0x10, 1, 0x748000, 0x3A4000),
    (0x70, b"TX2932"): (0x4DB, 0x10, 1, 0x3A4A000, 0x2E1A000),
    (0x70, b"TS0A32"): (0x4DB, 0x10, 1, 0x3A4A000, 0x2E1A000),
}


def _cid_identity(card):
    raw = b"".join(struct.pack(">I", w) for w in card.cid)
    return raw[13], raw[8:12] + raw[4:6]


def _capacity(card):
    sec_count = struct.unpack_from(">I", card.ext_csd, 0xD4)[0]
    if sec_count:
        return sec_count
    rsp1, rsp2 = card.csd[1], card.csd[2]
    c_size = ((rsp2 & 3) << 10) | (rsp1 >> 22)
    c_size_mult = (rsp1 >> 7) & 7
    read_bl_len = (rsp2 >> 8) & 0xF
    return ((c_size + 1) << (c_size_mult + 2) << read_bl_len) >> 9


def whitelist_accepts(card):
    row = WHITELIST.get(_cid_identity(card))
    if row is None:
        return False
    ext = card.ext_csd
    enh = int.from_bytes(ext[0x9C:0x9F], "big")
    if (enh, ext[0xDE], ext[0xE3]) != row[:3]:
        return False
    capacity = _capacity(card)
    if ext[0x98] == 0:
        return capacity == row[3]
    if ext[0x98] == 1:
        return capacity == row[4]
    return True


class CardIdentityTest(unittest.TestCase):
    def test_default_is_pslc_32g(self):
        card = esdhc.Card(identity=None)
        self.assertEqual(card.identity, "slc32g")
        self.assertEqual(card.blocks, 0x2E1A000)
        self.assertEqual(card.cid, [0, 0x33320000, 0x54583239, 0x00700000])
        self.assertEqual(_cid_identity(card), (0x70, b"TX2932"))
        ext = card.ext_csd
        self.assertEqual(ext[0x98], 1)
        self.assertEqual(ext[0x9C:0x9F], bytes([0x00, 0x04, 0xDB]))
        self.assertEqual(ext[0xDE], 0x10)
        self.assertEqual(ext[0xE3], 0x01)
        self.assertEqual(ext[0xD4:0xD8], bytes([0x02, 0xE1, 0xA0, 0x00]))
        self.assertEqual((ext[0xAF], ext[0xB7], ext[0xB9]), (1, 1, 1))

    def test_legacy_identities(self):
        mlc = esdhc.Card(identity="mlc4g")
        self.assertEqual(mlc.cid, [0, 0x45300000, 0x30303447, 0x00110000])
        self.assertEqual(mlc.blocks, 0x760000)
        self.assertEqual(mlc.ext_csd[0x98], 0)
        self.assertEqual(mlc.ext_csd[0xD4:0xD8], bytes([0, 0x76, 0, 0]))
        small = esdhc.Card(identity="small")
        self.assertEqual(small.blocks, 0x3B0000)
        self.assertEqual(small.ext_csd[0x98], 1)
        self.assertEqual(small.ext_csd[0xD4:0xD8], bytes(4))

    def test_every_identity_passes_the_whitelist(self):
        for name in esdhc.IDENTITIES:
            with self.subTest(identity=name):
                card = esdhc.Card(identity=name)
                self.assertTrue(whitelist_accepts(card))
                self.assertEqual(_capacity(card), card.blocks)
        self.assertGreater(esdhc.Card(identity="slc32g").blocks, 0x5EE180)

    def test_env_selects_identity(self):
        self.assertEqual(esdhc.identity_from_env({}), "slc32g")
        self.assertEqual(
            esdhc.identity_from_env({esdhc.IDENTITY_ENV: "mlc4g"}), "mlc4g"
        )
        with self.assertRaises(ValueError):
            esdhc.identity_from_env({esdhc.IDENTITY_ENV: "bogus"})

    def test_short_image_from_file(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "short.img")
            with open(path, "wb") as fh:
                fh.write(b"\x07" * 512)
            card = esdhc.Card.from_file(path, identity="slc32g")
            self.assertEqual(card.blocks, 0x2E1A000)
            self.assertEqual(bytes(card.image[:2]), b"\x07\x07")
            with self.assertRaises(ValueError):
                small = os.path.join(tmp, "big.img")
                with open(small, "wb") as fh:
                    fh.truncate(0x3B0001 * 512)
                esdhc.Card.from_file(small, identity="small")


if __name__ == "__main__":
    unittest.main()
