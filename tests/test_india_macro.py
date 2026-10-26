"""India macro vendor: alias routing, Yahoo clipping and formatting, FRED
delegation, the explicit repo-rate gap, and the router self-gate.

Every test is offline: the Yahoo ticker is a fake frame and FRED is patched, so
nothing here needs a network or a key.
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorDeclinedError, VendorUnavailableError
from tradingagents.dataflows.vendors import india_macro

AS_OF = "2025-09-30"


def _frame(rows: dict[str, float]) -> pd.DataFrame:
    """A yfinance-shaped daily frame: tz-aware index, one Close column."""
    index = pd.DatetimeIndex([pd.Timestamp(day, tz="UTC") for day in rows])
    return pd.DataFrame({"Close": list(rows.values())}, index=index)


class _FakeTicker:
    """Stands in for ``yf.Ticker`` and records what the module asked for."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.symbols: list[str] = []
        self.history_calls: list[dict] = []

    def __call__(self, symbol: str) -> _FakeTicker:
        self.symbols.append(symbol)
        return self

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        return self.frame


def _patch_ticker(frame: pd.DataFrame):
    ticker = _FakeTicker(frame)
    return ticker, mock.patch.object(india_macro.yf, "Ticker", ticker)


@pytest.mark.unit
class TestIndiaMacroAliases:
    def test_yahoo_aliases_map_to_the_verified_symbols(self):
        assert {
            alias: series.symbol for alias, series in india_macro.YAHOO_INDIA_SERIES.items()
        } == {
            "usd_inr": "INR=X",
            "india_vix": "^INDIAVIX",
            "nifty_50": "^NSEI",
            "sensex": "^BSESN",
        }

    def test_fred_aliases_map_to_the_verified_series_ids(self):
        assert india_macro.FRED_INDIA_SERIES == {
            "cpi_india": "CPALTT01INM659N",
            "gsec_10y_india": "INDIRLTLT01STM",
            "iip_india": "INDPROINDMISMEI",
            "real_gdp_india": "INDGDPRQPSMEI",
        }

    def test_repo_rate_is_a_named_alias(self):
        assert india_macro.REPO_RATE_INDIA["alias"] == "repo_rate_india"

    def test_alias_normalization_matches_fred(self):
        # fred._resolve_series_id lowercases and folds spaces/hyphens; an alias
        # must read the same however the analyst typed it.
        assert india_macro._normalize("GDP-India") == "gdp_india"
        assert india_macro._normalize("  Nifty 50 ") == "nifty_50"
        assert india_macro._normalize("USD-INR") == "usd_inr"

    def test_separator_insensitive_alias_reaches_the_vendor(self):
        ticker, patched = _patch_ticker(_frame({"2025-09-30": 22000.0}))
        with patched:
            out = india_macro.get_macro_data("Nifty-50", AS_OF)
        assert "Nifty 50" in out
        assert ticker.symbols == ["^NSEI"]


@pytest.mark.unit
class TestIndiaMacroYahoo:
    def test_nifty_report_names_latest_units_and_trailing_return(self):
        ticker, patched = _patch_ticker(
            _frame(
                {
                    "2025-01-01": 21000.00,
                    "2025-09-29": 22000.50,
                    AS_OF: 22345.60,
                }
            )
        )
        with patched:
            out = india_macro.get_macro_data("nifty_50", AS_OF)

        assert "## Yahoo Finance: Nifty 50 (^NSEI)" in out
        assert "Units: Index points" in out
        assert "Frequency: Daily (exchange close)" in out
        assert "**Latest:** 22,345.60 (2025-09-30)" in out
        assert "Trailing return" in out
        assert "| 2025-09-30 | 22,345.60 |" in out
        assert ticker.symbols == ["^NSEI"]

    def test_historical_run_never_shows_a_future_row(self):
        ticker, patched = _patch_ticker(
            _frame(
                {
                    "2025-09-29": 22000.50,
                    AS_OF: 22345.60,
                    "2025-12-31": 99999.99,  # after the analysis date
                }
            )
        )
        with patched:
            out = india_macro.get_macro_data("nifty_50", AS_OF)

        assert "2025-12-31" not in out
        assert "99,999.99" not in out and "99999" not in out
        # yfinance's end is exclusive, so the as-of row needs as_of + 1.
        assert ticker.history_calls[0]["end"] == "2025-10-01"
        assert ticker.history_calls[0]["interval"] == "1d"

    def test_usd_inr_renders_four_decimals(self):
        _, patched = _patch_ticker(
            _frame({"2025-09-01": 83.1000, AS_OF: 84.5678})
        )
        with patched:
            out = india_macro.get_macro_data("usd_inr", AS_OF)

        assert "Units: INR per USD" in out
        assert "84.5678" in out
        assert "| 2025-09-30 | 84.5678 |" in out

    def test_empty_frame_is_an_explicit_gap_not_a_zero(self):
        _, patched = _patch_ticker(pd.DataFrame())
        with patched:
            out = india_macro.get_macro_data("india_vix", AS_OF)

        assert "no observations" in out.casefold()
        assert "missing coverage" in out.casefold()
        assert "not a flat reading" in out.casefold()
        assert "**Latest:**" not in out

    def test_none_frame_is_the_same_gap(self):
        # yf_retry returns None when Yahoo answers that it has nothing.
        _, patched = _patch_ticker(None)
        with patched:
            out = india_macro.get_macro_data("sensex", AS_OF)
        assert "no observations" in out.casefold()
        assert "**Latest:**" not in out

    def test_request_failure_is_an_explicit_string_not_an_exception(self):
        with mock.patch.object(
            india_macro,
            "yf_retry",
            side_effect=VendorUnavailableError("Yahoo Finance rate limited after 3 retries"),
        ):
            out = india_macro.get_macro_data("nifty_50", AS_OF)

        assert "could not be fetched" in out
        assert "missing coverage" in out.casefold()
        assert "**Latest:**" not in out

    def test_rows_are_capped_but_summary_uses_the_full_window(self):
        days = pd.bdate_range(end=AS_OF, periods=50)
        rows = {day.strftime("%Y-%m-%d"): 20000.0 + i for i, day in enumerate(days)}
        _, patched = _patch_ticker(_frame(rows))
        with patched:
            out = india_macro.get_macro_data("nifty_50", AS_OF)

        assert f"showing the most recent {india_macro.MAX_ROWS} of 50" in out
        table_rows = [line for line in out.splitlines() if line.startswith("| 20")]
        assert len(table_rows) == india_macro.MAX_ROWS
        # The trailing return still spans the first observation, not the cap.
        assert "**Trailing return from 2025-07-23 to 2025-09-30:**" in out


@pytest.mark.unit
class TestIndiaMacroFred:
    def test_missing_key_names_the_variable_and_the_gap(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        with mock.patch.object(india_macro.fred, "get_macro_data") as delegated:
            out = india_macro.get_macro_data("cpi_india", AS_OF)

        delegated.assert_not_called()
        assert "FRED_API_KEY" in out
        assert "missing coverage" in out.casefold()
        assert "not a flat reading" in out.casefold()

    def test_with_a_key_the_mapped_id_and_window_are_delegated(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "test-key")
        with mock.patch.object(
            india_macro.fred, "get_macro_data", return_value="FRED CPI REPORT"
        ) as delegated:
            out = india_macro.get_macro_data("cpi_india", AS_OF, 90)

        assert out == "FRED CPI REPORT"
        delegated.assert_called_once_with("CPALTT01INM659N", AS_OF, 90)

    def test_default_lookback_is_a_year(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "test-key")
        with mock.patch.object(
            india_macro.fred, "get_macro_data", return_value="FRED REPORT"
        ) as delegated:
            india_macro.get_macro_data("real_gdp_india", AS_OF)
        delegated.assert_called_once_with(
            "INDGDPRQPSMEI", AS_OF, india_macro.DEFAULT_LOOKBACK_DAYS
        )

    def test_fred_error_becomes_a_gap_not_a_raw_alias_retry(self, monkeypatch):
        # The router's default chain is india_macro,fred: raising here would
        # hand FRED the alias "cpi_india", which it cannot resolve.
        monkeypatch.setenv("FRED_API_KEY", "test-key")
        with mock.patch.object(
            india_macro.fred,
            "get_macro_data",
            side_effect=VendorUnavailableError("FRED returned HTTP 503"),
        ):
            out = india_macro.get_macro_data("cpi_india", AS_OF)

        assert "could not be fetched" in out
        assert "missing coverage" in out.casefold()
        assert "CPALTT01INM659N" in out


@pytest.mark.unit
class TestIndiaMacroGaps:
    def test_repo_rate_is_an_explicit_uncovered_series(self):
        out = india_macro.get_macro_data("repo_rate_india", AS_OF)

        assert "no free point-in-time source" in out
        assert "not an archived feed" in out
        assert "would read today's rate" in out
        assert "is not estimated" in out
        assert "**Latest:**" not in out

    @pytest.mark.parametrize("indicator", ["cpi", "usd_jpy", "bank of japan rate"])
    def test_non_india_alias_declines_so_the_chain_can_fall_through(self, indicator):
        # A decline is not "no data": the router must keep looking rather than
        # record a verdict about an alias this vendor never checked.
        with pytest.raises(VendorDeclinedError, match="not an India macro alias"):
            india_macro.get_macro_data(indicator, AS_OF)


@pytest.mark.unit
class TestIndiaMacroRouting:
    def test_registered_for_macro_indicators_only(self):
        assert "india_macro" in router.VENDOR_METHODS["get_macro_indicators"]
        assert router.VENDOR_METHODS["get_macro_indicators"]["india_macro"] is (
            india_macro.get_macro_data
        )
        for method, vendors in router.VENDOR_METHODS.items():
            if method != "get_macro_indicators":
                assert "india_macro" not in vendors

    def test_us_alias_falls_through_to_fred(self):
        # The self-gate is what makes the default india_macro,fred chain work:
        # a vendor that claims every alias would answer a US CPI question with
        # an India error instead of letting FRED serve it.
        fred_calls: list[tuple] = []

        def _fred(*args, **kwargs):
            fred_calls.append(args)
            return "FRED CPI"

        with (
            mock.patch.object(router, "get_vendor", return_value="india_macro,fred"),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {
                    "get_macro_indicators": {
                        "india_macro": india_macro.get_macro_data,
                        "fred": _fred,
                    }
                },
                clear=False,
            ),
        ):
            out = router.route_to_vendor("get_macro_indicators", "cpi", AS_OF, 365)

        assert out == "FRED CPI"
        assert fred_calls == [("cpi", AS_OF, 365)]

    def test_a_declined_us_alias_does_not_mask_fred_missing_its_key(self):
        # Regression: with india_macro first, a NoMarketDataError decline made
        # the router report "no market data for cpi" and hide the real cause
        # (no FRED_API_KEY). A decline must leave the chain's error visible.
        def _unconfigured(*args, **kwargs):
            raise india_macro.fred.FredNotConfiguredError("FRED_API_KEY is not set")

        with (
            mock.patch.object(router, "get_vendor", return_value="india_macro,fred"),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {
                    "get_macro_indicators": {
                        "india_macro": india_macro.get_macro_data,
                        "fred": _unconfigured,
                    }
                },
                clear=False,
            ),
        ):
            out = router.route_to_vendor("get_macro_indicators", "cpi", AS_OF, 365)

        assert "DATA_UNAVAILABLE" in out
        assert "FRED_API_KEY" in out
        assert "NO_DATA_AVAILABLE" not in out

    def test_india_alias_is_served_without_reaching_fred(self):
        _, patched = _patch_ticker(_frame({AS_OF: 84.5678}))
        fred_mock = mock.Mock(return_value="FRED")
        with (
            patched,
            mock.patch.object(router, "get_vendor", return_value="india_macro,fred"),
            mock.patch.dict(
                router.VENDOR_METHODS,
                {
                    "get_macro_indicators": {
                        "india_macro": india_macro.get_macro_data,
                        "fred": fred_mock,
                    }
                },
                clear=False,
            ),
        ):
            out = router.route_to_vendor("get_macro_indicators", "usd_inr", AS_OF, 365)

        assert "USD/INR" in out
        fred_mock.assert_not_called()
