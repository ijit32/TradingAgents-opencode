"""The India fundamentals pack: profile, filings, XBRL and shareholding composed.

The transport and each source are tested on their own; this file checks the
composition the analyst actually reads — heading levels, point-in-time filtering,
raw INR, and the "filed but not structured" fallback that keeps a PDF from
turning into invented numbers.
"""

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.nse import fundamentals
from tradingagents.dataflows.vendors.nse.filings import Filing

FIXTURES = Path(__file__).parent / "fixtures" / "india" / "xbrl"


def _filing(subject: str, when: datetime, url: str, has_xbrl: bool = False) -> Filing:
    return Filing(subject=subject, published=when, url=url, has_xbrl=has_xbrl, symbol="RELIANCE")


def _row(**overrides) -> dict:
    row = {
        "type": "Integrated Filing- Financials",
        "type_Sub": "Original",
        "consolidated": "Consolidated",
        "audited": "Audited",
        "qe_Date": "31-MAR-2025",
        "broadcast_Date": "25-Apr-2025 21:57:16",
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/RELIANCE-2025-03-31.xml",
        "pdf_attach": "https://nsearchives.nseindia.com/corporate/RELIANCE-25042025.pdf",
    }
    row.update(overrides)
    return row


@mock.patch.object(fundamentals.filings, "results_filings", return_value=[])
@mock.patch.object(fundamentals.shareholding, "get_shareholding", return_value="## Shareholding\n\nPromoter: 50.44%")
@mock.patch.object(fundamentals.filings, "recent_filings")
@mock.patch.object(fundamentals.filings, "integrated_filings")
@mock.patch.object(fundamentals.client, "get_bytes")
@mock.patch(
    "tradingagents.dataflows.vendors.yahoo.fundamentals.get_fundamentals",
    return_value="## Company Profile\n\nMarket cap: ...",
)
@pytest.mark.unit
class IndiaFundamentalsTests(unittest.TestCase):
    def test_historical_run_appends_filings_and_figures(
        self, _profile, get_bytes, integrated, recent, _shareholding, _results
    ):
        recent.return_value = [
            _filing(
                "Outcome of Board Meeting",
                datetime(2025, 4, 25, 14, 35, tzinfo=UTC),  # 20:05 IST
                "https://nsearchives.nseindia.com/corporate/RELIANCE-25042025.pdf",
            ),
            _filing(
                "Press Release",
                datetime(2025, 4, 24, 4, 0, tzinfo=UTC),  # 09:30 IST
                "https://nsearchives.nseindia.com/corporate/RELIANCE-PR.pdf",
            ),
        ]
        integrated.return_value = [_row()]
        get_bytes.return_value = (FIXTURES / "RELIANCE-2025-03-31-annual.xml").read_bytes()

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-06-02")

        self.assertIn("Historical run dated 2025-06-02", text)
        self.assertIn("2025-04-25 20:05 IST", text)
        self.assertIn("Outcome of Board Meeting", text)
        self.assertIn("RELIANCE-25042025.pdf", text)
        # Annual instance: P&L, balance sheet and cash flow in absolute INR.
        self.assertIn("# Income Statement for RELIANCE.NS (annual)", text)
        self.assertIn("| Revenue | 9,801,360,000,000 | INR |", text)
        self.assertIn("# Balance Sheet for RELIANCE.NS (annual)", text)
        self.assertIn("| Total Assets | 19,501,210,000,000 | INR |", text)
        self.assertIn("# Cash Flow Statement for RELIANCE.NS (annual)", text)
        # The filing instant is shown, and the basis comes from the instance.
        self.assertIn("broadcast 2025-04-25 21:57 IST", text)
        self.assertIn("consolidated", text)
        self.assertIn("Promoter: 50.44%", text)

    def test_the_results_filing_is_kept_when_newer_rows_are_routine(
        self, _profile, _get_bytes, integrated, recent, _shareholding, results
    ):
        recent.return_value = [
            _filing("Updates", datetime(2025, 6, 2, 13, 33, tzinfo=UTC), "https://x/updates.pdf"),
            _filing(
                "Analysts/Institutional Investor Meet/Con. Call Updates",
                datetime(2025, 5, 30, 13, 40, tzinfo=UTC),
                "https://x/meet.pdf",
            ),
            _filing("Press Release", datetime(2025, 5, 29, 10, 0, tzinfo=UTC), "https://x/pr.pdf"),
        ]
        results.return_value = [
            _filing(
                "Outcome of Board Meeting",
                datetime(2025, 4, 25, 14, 35, tzinfo=UTC),
                "https://x/RELIANCE-25042025.pdf",
            )
        ]
        integrated.return_value = []

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-06-02")

        self.assertIn("Outcome of Board Meeting", text)
        self.assertIn("2025-04-25 20:05 IST", text)
        self.assertIn("RELIANCE-25042025.pdf", text)

    def test_quarterly_pnl_is_paired_with_the_latest_annual_balance_sheet(
        self, _profile, get_bytes, integrated, recent, _shareholding, _results
    ):
        recent.return_value = []
        integrated.return_value = [
            _row(
                qe_Date="30-JUN-2025",
                broadcast_Date="18-Jul-2025 19:31:34",
                xbrl="https://nsearchives.nseindia.com/corporate/xbrl/RELIANCE-Q1.xml",
            ),
            _row(),
        ]

        def _bytes(url, **kwargs):
            name = "RELIANCE-2025-06-30-quarter.xml" if "Q1" in url else "RELIANCE-2025-03-31-annual.xml"
            return (FIXTURES / name).read_bytes()

        get_bytes.side_effect = _bytes

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2026-10-25")

        self.assertIn("# Income Statement for RELIANCE.NS (quarterly)", text)
        self.assertIn("| Revenue | 2,486,600,000,000 | INR |", text)
        self.assertIn("period 2025-04-01 to 2025-06-30", text)
        # The balance sheet is annual and labelled with its own period.
        self.assertIn("# Balance Sheet for RELIANCE.NS (annual)", text)
        self.assertIn("period 2024-04-01 to 2025-03-31", text)

    def test_a_filing_after_the_run_date_is_not_visible(
        self, _profile, _get_bytes, integrated, recent, _shareholding, _results
    ):
        recent.return_value = []
        # Broadcast one day after the run date.
        integrated.return_value = [_row(broadcast_Date="26-Apr-2025 10:00:00")]

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-04-25")

        self.assertNotIn("9,801,360,000,000", text)
        self.assertNotIn("2025-04-26", text)
        self.assertIn("No structured results filing is public on or before this date", text)

    def test_a_filing_without_an_instance_is_named_and_linked(
        self, _profile, _get_bytes, integrated, recent, _shareholding, _results
    ):
        recent.return_value = []
        integrated.return_value = [_row(xbrl="")]

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-06-02")

        self.assertIn("carry no structured XBRL", text)
        self.assertIn("RELIANCE-25042025.pdf", text)
        self.assertIn("No figures are stated here", text)
        self.assertNotIn("9,801,360,000,000", text)

    def test_an_unreadable_instance_falls_back_to_the_notice(
        self, _profile, get_bytes, integrated, recent, _shareholding, _results
    ):
        recent.return_value = []
        integrated.return_value = [_row()]
        get_bytes.return_value = b"<html><body>not xbrl</body></html>"

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-06-02")

        self.assertIn("could not be read as XBRL", text)
        self.assertIn("No figures are stated here", text)

    def test_a_quote_outage_does_not_take_the_filings_down(
        self, profile, _get_bytes, integrated, recent, _shareholding, _results
    ):
        profile.side_effect = VendorUnavailableError("Yahoo is throttling")
        recent.return_value = []
        integrated.return_value = []

        text = fundamentals.get_fundamentals("RELIANCE.NS", "2025-06-02")

        self.assertIn("Company profile unavailable right now", text)
        self.assertIn("As filed with NSE", text)




@pytest.mark.unit
class IndiaFundamentalsBseTests(unittest.TestCase):
    def test_a_bse_listing_without_an_alias_is_refused(self):
        # The filing adapters are real here: the refusal must come from symbol
        # resolution, before any NSE request or any profile metric.
        with mock.patch(
            "tradingagents.dataflows.vendors.yahoo.fundamentals.get_fundamentals",
            return_value="## Company Profile\n",
        ), mock.patch(
            "tradingagents.dataflows.vendors.nse.client.get_json"
        ) as get_json, self.assertRaises(NoMarketDataError) as ctx:
            fundamentals.get_fundamentals("RELIANCE.BO", "2025-06-02")

        self.assertIn("NSE symbol", ctx.exception.detail)
        get_json.assert_not_called()


@pytest.mark.unit
class IndiaFundamentalsGateTests(unittest.TestCase):
    def test_a_us_symbol_is_refused_before_any_profile_call(self):
        with mock.patch(
            "tradingagents.dataflows.vendors.yahoo.fundamentals.get_fundamentals"
        ) as profile, self.assertRaises(NoMarketDataError) as ctx:
            fundamentals.get_fundamentals("AAPL", "2025-06-02")

        self.assertIn("not an NSE/BSE listing", ctx.exception.detail)
        profile.assert_not_called()

    def test_a_malformed_date_is_rejected(self):
        with self.assertRaises(ValueError):
            fundamentals.get_fundamentals("RELIANCE.NS", "02-06-2025")


@pytest.mark.unit
class RouterRegistrationTests(unittest.TestCase):
    def test_nse_serves_fundamentals_and_insider_transactions(self):
        from tradingagents.dataflows.router import VENDOR_METHODS

        self.assertIn("nse", VENDOR_METHODS["get_fundamentals"])
        self.assertIn("nse", VENDOR_METHODS["get_insider_transactions"])
