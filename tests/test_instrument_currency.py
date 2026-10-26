"""The reporting-currency line in the instrument context every agent sees.

A report on an NSE/BSE listing must state its figures in INR; a US prompt must
stay byte-identical to the one it had before this line existed, and a market
whose quote currency is unverified must not be assigned one.
"""

import unittest

import pytest

from tradingagents.agents.context import build_instrument_context
from tradingagents.agents.schemas import PortfolioDecision, TraderProposal


@pytest.mark.unit
class ReportingCurrencyContextTests(unittest.TestCase):
    def test_nse_listing_is_quoted_in_inr(self):
        context = build_instrument_context("RELIANCE.NS")
        self.assertIn("quoted in INR", context)
        self.assertIn("report every price", context)
        self.assertNotIn("quoted in USD", context)

    def test_bse_listing_is_quoted_in_inr(self):
        context = build_instrument_context("RELIANCE.BO")
        self.assertIn("quoted in INR", context)
        self.assertNotIn("quoted in USD", context)

    def test_indian_index_is_quoted_in_inr(self):
        # An index carries no exchange suffix; only the market map knows it.
        context = build_instrument_context("^NSEI")
        self.assertIn("quoted in INR", context)
        self.assertNotIn("quoted in USD", context)

    def test_us_listing_keeps_the_currency_free_wording(self):
        identity = {
            "company_name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "exchange": "NMS",
        }
        expected = (
            "The instrument to analyze is `AAPL`. The tools serve this instrument; "
            "refer to it by this exact ticker in every report and recommendation, "
            "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`). "
            "Resolved identity: Company: Apple Inc.; Business classification: "
            "Technology / Consumer Electronics; Exchange: NMS. Do not substitute a "
            "different company or ticker unless a tool result explicitly disproves "
            "this resolved identity."
        )
        context = build_instrument_context("AAPL", "stock", identity)
        self.assertEqual(context, expected)
        self.assertNotIn("quoted in", context)
        self.assertNotIn("report every price", context)

    def test_unverified_currency_gets_no_claim(self):
        # `BRK.B` is a US ticker whose dot is part of the symbol; `.L` quotes in
        # pence as often as pounds. Neither may be labelled or defaulted to USD.
        # (`USD` does occur in the base sentence's `-USD` suffix example, so the
        # assertion is on the currency claim, not the raw token.)
        for ticker in ("BRK.B", "HSBA.L"):
            with self.subTest(ticker=ticker):
                context = build_instrument_context(ticker)
                self.assertNotIn("quoted in", context)
                self.assertNotIn("quoted in USD", context)
                self.assertNotIn("report every price", context)

    def test_inr_line_is_appended_to_the_identity_details(self):
        context = build_instrument_context(
            "RELIANCE.NS",
            "stock",
            {
                "company_name": "Reliance Industries Limited",
                "sector": "Energy",
                "industry": "Oil & Gas Refining & Marketing",
                "exchange": "NSI",
            },
        )
        self.assertIn("Company: Reliance Industries Limited", context)
        self.assertIn("Energy / Oil & Gas Refining & Marketing", context)
        self.assertIn("Exchange: NSI", context)
        self.assertIn("quoted in INR", context)
        self.assertLess(context.index("Resolved identity"), context.index("quoted in INR"))


@pytest.mark.unit
class PriceFieldCurrencyTests(unittest.TestCase):
    def test_price_descriptions_are_currency_agnostic(self):
        descriptions = [
            TraderProposal.model_fields["entry_price"].description,
            TraderProposal.model_fields["stop_loss"].description,
            PortfolioDecision.model_fields["price_target"].description,
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertIn("quote currency", description)
                self.assertNotIn("USD", description)
                self.assertNotIn("$", description)


if __name__ == "__main__":
    unittest.main()
