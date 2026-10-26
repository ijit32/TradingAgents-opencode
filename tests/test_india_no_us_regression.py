"""An AAPL run must not change because India vendors joined the chains.

The India vendors are registered in the same ``router.VENDOR_METHODS`` the US
path already uses, so the only thing keeping a US run byte-identical is that
each India vendor declines a US symbol or alias *before* it makes a request.
This file pins that contract end to end:

* the market map resolves ``AAPL`` to US/USD and an Indian listing to IN/INR;
* the NSE fundamentals and SEBI PIT entry points, and the India news vendor,
  raise for ``AAPL`` with the shared transport patched to fail if touched;
* ``india_macro.get_macro_data("cpi", ...)`` is a ``VendorDeclinedError``, not a
  ``NoMarketDataError`` — a decline says "not mine", so the router keeps
  looking instead of recording a no-data verdict;
* with the shipped default ``data_vendors`` config, an ``AAPL`` news or
  fundamentals call runs the real India vendor first and still reaches Yahoo,
  and a US ``cpi`` call still reaches FRED with no ``NO_DATA_AVAILABLE``.

Statements are the one gap: NSE has no separate ``get_income_statement`` /
``get_balance_sheet`` / ``get_cashflow`` entry point (they are composed inside
``nse.fundamentals.get_fundamentals``, whose gate is tested here), so there is
nothing to patch one layer lower. That absence is asserted instead of invented.

Every test is offline: the NSE transport and each US vendor are mocks, so the
suite's autouse ``_no_network`` fixture stays authoritative.
"""

from __future__ import annotations

from unittest import mock

import pytest

import tradingagents.default_config as default_config
from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorDeclinedError
from tradingagents.dataflows.markets import market_for, report_currency
from tradingagents.dataflows.vendors import india_macro
from tradingagents.dataflows.vendors.india_news import (
    gdelt,
    get_news as india_news_get_news,
    google,
    publishers,
)
from tradingagents.dataflows.vendors.nse import client as nse_client, filings as nse_filings
from tradingagents.dataflows.vendors.nse.fundamentals import (
    get_fundamentals as nse_get_fundamentals,
)
from tradingagents.dataflows.vendors.nse.pit import (
    get_insider_transactions as nse_get_insider_transactions,
)

US = "AAPL"
AS_OF = "2025-06-02"
WINDOW = ("2025-06-01", "2025-06-07")

# Any India vendor that reaches this has already made the regression possible:
# the self-gate exists so the transport is never asked about a US symbol.
_REQUEST_FORBIDDEN = "an India vendor requested NSE data for a US symbol"


def _no_request(*args, **kwargs):
    raise AssertionError(f"{_REQUEST_FORBIDDEN}: {args} {kwargs}")


@pytest.mark.unit
class TestMarketResolution:
    def test_aapl_is_us_and_reports_dollars(self):
        assert market_for(US) == "US"
        assert report_currency(US) == "USD"

    def test_an_indian_listing_differs(self):
        assert market_for("RELIANCE.NS") == "IN"
        assert report_currency("RELIANCE.NS") == "INR"


@pytest.mark.unit
class TestIndiaVendorsDeclineUsSymbols:
    """Each vendor refuses before its first request, so the chain can move on."""

    def test_nse_fundamentals_declines_aapl(self):
        with (
            mock.patch.object(nse_client, "get_json", side_effect=_no_request) as get_json,
            mock.patch.object(nse_client, "get_bytes", side_effect=_no_request) as get_bytes,
            pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
        ):
            nse_get_fundamentals(US, AS_OF)
        get_json.assert_not_called()
        get_bytes.assert_not_called()

    def test_nse_insider_transactions_declines_aapl(self):
        # PIT resolves the NSE symbol first, so the gate is in filing_symbol().
        with (
            mock.patch.object(nse_client, "get_json", side_effect=_no_request) as get_json,
            pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
        ):
            nse_get_insider_transactions(US, AS_OF)
        get_json.assert_not_called()

    def test_india_news_declines_aapl(self):
        with (
            mock.patch.object(nse_client, "get_json", side_effect=_no_request) as get_json,
            mock.patch.object(nse_client, "get_bytes", side_effect=_no_request) as get_bytes,
            mock.patch.object(google, "fetch") as google_fetch,
            mock.patch.object(gdelt, "fetch") as gdelt_fetch,
            mock.patch.object(publishers, "fetch") as publishers_fetch,
            mock.patch.object(nse_filings, "announcements") as announcements,
            pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
        ):
            india_news_get_news(US, *WINDOW)
        get_json.assert_not_called()
        get_bytes.assert_not_called()
        google_fetch.assert_not_called()
        gdelt_fetch.assert_not_called()
        publishers_fetch.assert_not_called()
        announcements.assert_not_called()

    def test_nse_registers_no_statement_entry_point(self):
        # NSE statements are composed inside get_fundamentals (gated above), not
        # exposed as separate callables, so an AAPL statement call has no India
        # vendor in its chain to begin with. Pin that absence: adding one here
        # without a self-gate is exactly the regression this file guards.
        for method in ("get_income_statement", "get_balance_sheet", "get_cashflow"):
            assert "nse" not in router.VENDOR_METHODS[method]


@pytest.mark.unit
class TestIndiaMacroDeclinesUsAlias:
    def test_cpi_is_a_decline_not_a_no_data_verdict(self):
        # The distinction is the whole Phase 5 fix: a decline skips silently,
        # a no-data verdict would win the chain and tell the analyst that US CPI
        # has no market data.
        with pytest.raises(VendorDeclinedError, match="not an India macro alias") as caught:
            india_macro.get_macro_data("cpi", AS_OF)

        assert not isinstance(caught.value, NoMarketDataError)


@pytest.mark.unit
class TestDefaultChainsKeepUsPath:
    """Route through the shipped default ``data_vendors``, not a hand-built chain."""

    def test_shipped_defaults_still_name_the_us_vendors(self):
        vendors = default_config.DEFAULT_CONFIG["data_vendors"]
        assert vendors["news_data"] == "nse,yfinance"
        assert vendors["fundamental_data"] == "sec_edgar,nse,yfinance"
        assert vendors["macro_data"] == "india_macro,fred"

    def test_aapl_news_reaches_yahoo(self):
        yahoo = mock.Mock(return_value="YAHOO NEWS SENTINEL")
        with (
            run_config({}),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {"get_news": {"nse": india_news_get_news, "yfinance": yahoo}},
                clear=False,
            ),
            mock.patch.object(nse_client, "get_json", side_effect=_no_request),
            mock.patch.object(nse_client, "get_bytes", side_effect=_no_request),
        ):
            out = router.route_to_vendor("get_news", US, *WINDOW)

        assert out == "YAHOO NEWS SENTINEL"
        assert yahoo.call_args.args == (US, *WINDOW)

    def test_aapl_fundamentals_reaches_yahoo(self):
        # sec_edgar is left out of the patched mapping so the chain test needs no
        # network: the router filters the configured list by what it can call,
        # keeping nse then yfinance. The real nse gate must decline first.
        yahoo = mock.Mock(return_value="YAHOO FUNDAMENTALS SENTINEL")
        with (
            run_config({}),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {"get_fundamentals": {"nse": nse_get_fundamentals, "yfinance": yahoo}},
                clear=False,
            ),
            mock.patch.object(nse_client, "get_json", side_effect=_no_request) as get_json,
            mock.patch.object(nse_client, "get_bytes", side_effect=_no_request),
        ):
            out = router.route_to_vendor("get_fundamentals", US, AS_OF)

        assert out == "YAHOO FUNDAMENTALS SENTINEL"
        assert yahoo.call_args.args == (US, AS_OF)
        get_json.assert_not_called()

    def test_us_cpi_skips_the_decline_and_returns_fred(self):
        fred = mock.Mock(return_value="FRED CPI SENTINEL")
        with (
            run_config({}),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {
                    "get_macro_indicators": {
                        "india_macro": india_macro.get_macro_data,
                        "fred": fred,
                    }
                },
                clear=False,
            ),
        ):
            out = router.route_to_vendor("get_macro_indicators", "cpi", AS_OF, 365)

        assert out == "FRED CPI SENTINEL"
        assert "NO_DATA_AVAILABLE" not in out
        fred.assert_called_once_with("cpi", AS_OF, 365)


@pytest.mark.unit
class TestUsPromptText:
    def test_aapl_context_makes_no_currency_claim(self):
        # The INR line is Phase 6's only prompt change; a US prompt must not gain
        # one. The byte-exact AAPL prompt and the reflection defaults are owned by
        # tests/test_instrument_currency.py and tests/test_reflection_currency.py.
        context = build_instrument_context(US)
        assert "`AAPL`" in context
        assert "quoted in" not in context
        assert "report every price" not in context
