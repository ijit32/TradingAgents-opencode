"""NSE/BSE tickers contain ``&``: ``M&M.NS``, ``J&KBANK.NS`` and friends.

The ampersand is safe in a path component (it enables no traversal, and every
filesystem write in this repo goes through ``pathlib``), so the validator must
accept it while still rejecting separators, whitespace, NUL, and dot-only
values.
"""

import unittest

import pytest

from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component


@pytest.mark.unit
class TestAmpersandTickers(unittest.TestCase):
    def test_accepts_nse_ampersand_symbols(self):
        for ticker in ("M&M.NS", "M&MFIN.NS", "J&KBANK.NS", "L&TFH.NS"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_accepts_bare_ampersand_symbol(self):
        self.assertEqual(safe_ticker_component("M&M"), "M&M")

    def test_ampersand_does_not_relax_traversal_checks(self):
        for bad in ("..", "../x", "a/b", "a b", "a\x00b", ""):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_length_cap_still_applies(self):
        with self.assertRaises(ValueError):
            safe_ticker_component("A" * 33)
        with self.assertRaises(ValueError):
            safe_ticker_component("M&M" + "A" * 30)
        self.assertEqual(safe_ticker_component("A" * 32), "A" * 32)

    def test_normalize_symbol_keeps_ampersand(self):
        self.assertEqual(normalize_symbol("M&M.NS"), "M&M.NS")
        self.assertEqual(normalize_symbol("m&mfin.ns"), "M&MFIN.NS")


if __name__ == "__main__":
    unittest.main()
