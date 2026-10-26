# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
import contextlib
import io
import unittest

from tools.coldfire import hwlookup
from tools.coldfire.hwref import (
    resolve_address,
    resolve_dma_channel,
    resolve_vector,
)


class AddressLookupTest(unittest.TestCase):
    def test_resolves_channel_50_citer_using_coldfire_layout(self):
        result = resolve_address(0xFC045654)
        self.assertEqual(result["name"], "EDMA.TCD50.CITER")
        self.assertEqual(result["channel"], 50)
        self.assertEqual(result["width"], 2)


class RoutingLookupTest(unittest.TestCase):
    def test_vector_170_is_dma_50_completion(self):
        result = resolve_vector(170)
        self.assertEqual((result["controller"], result["source"]), ("INTC1", 42))
        self.assertEqual(
            result["assignment"]["description"], "DMA channel 50 transfer complete"
        )
        self.assertEqual(result["assignment"]["clear"], "write EDMA_CINT=50")

    def test_invalid_dma_channel_does_not_invent_tcd_address(self):
        result = resolve_dma_channel(64)
        self.assertFalse(result["valid"])
        self.assertNotIn("tcd_address", result)


class CliTest(unittest.TestCase):
    def test_human_dma_lookup(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hwlookup.main(["dma", "50"])
        text = output.getvalue()
        self.assertIn("SSI0_SR[TFE0]", text)
        self.assertIn("vector 170", text)


if __name__ == "__main__":
    unittest.main()
