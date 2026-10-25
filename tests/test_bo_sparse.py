"""BSE (``.BO``) sparse-history guard: a tape that answers a long window with a
handful of bars must be reported, not formatted into the report as prices.

Measured 2026-10-25: complete ``.BO`` scrips return 94-95% of the window's
weekdays, a truncated BSE tape returns one latest bar whatever the window, and
the highest ratio any defective case reached was 41.8%. The guard fires below
50%, only for India ``.BO``, only over windows of 20+ weekdays.
"""

import unittest
from unittest import mock

import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.market as yahoo_market
import tradingagents.dataflows.vendors.yahoo.ohlcv as yahoo_ohlcv
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.nse.issuer import Issuer

# A one-year window: 260 weekdays, so the 50% rule needs 130 bars to pass.
START = "2025-10-26"
END = "2026-10-25"


def _frame(days: list[str]) -> pd.DataFrame:
    """A yfinance-shaped frame: DatetimeIndex named ``Date`` plus the OHLCV columns."""
    index = pd.DatetimeIndex([pd.Timestamp(day) for day in days], name="Date")
    rows = len(days)
    return pd.DataFrame(
        {
            "Open": [100.0] * rows,
            "High": [101.0] * rows,
            "Low": [99.0] * rows,
            "Close": [100.5] * rows,
            "Volume": [1_000] * rows,
        },
        index=index,
    )


def _session_days(count: int) -> list[str]:
    """``count`` consecutive weekdays ending on the window's last weekday."""
    days = pd.bdate_range(end="2026-10-23", periods=count)
    return [day.strftime("%Y-%m-%d") for day in days]


class _FakeYFinance:
    """Stands in for the ``yf`` module: every Ticker answers the same frame."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.requested: list[tuple[str, str, str]] = []

    def Ticker(self, symbol):  # noqa: N802 - mirrors yfinance's API
        outer = self

        class _Ticker:
            def history(self, start, end, *args, **kwargs):
                outer.requested.append((symbol, start, end))
                return outer.frame.copy()

        return _Ticker()


@pytest.mark.unit
class BoSparseGuardTests(unittest.TestCase):
    def _call(self, symbol: str, frame: pd.DataFrame, start=START, end=END):
        fake = _FakeYFinance(frame)
        with mock.patch.object(yahoo_market, "yf", fake):
            result = yahoo_market.get_YFin_data_online(symbol, start, end)
        return result, fake

    def test_sparse_bo_raises_with_counts_and_nse_hint(self):
        issuer = Issuer(
            symbol="RELIANCE", name="RELIANCE INDUSTRIES LIMITED",
            isin="INE002A01018", series="EQ",
        )
        with mock.patch(
            "tradingagents.dataflows.vendors.nse.issuer.issuer_for", return_value=issuer
        ), self.assertRaises(NoMarketDataError) as ctx:
            self._call("RELIANCE.BO", _frame(["2026-10-23"]))

        detail = ctx.exception.detail
        self.assertIn("only 1 bar between 2025-10-26 and 2026-10-25", detail)
        self.assertIn("0% of 260 sessions", detail)
        self.assertIn("sparse", detail)
        self.assertIn("'RELIANCE.NS'", detail)
        self.assertIn("request that ticker for full history", detail)

    def test_sparse_bo_without_an_nse_row_raises_without_a_hint(self):
        with mock.patch(
            "tradingagents.dataflows.vendors.nse.issuer.issuer_for", return_value=None
        ), self.assertRaises(NoMarketDataError) as ctx:
            self._call("TCS.BO", _frame(["2026-10-23"]))

        detail = ctx.exception.detail
        self.assertIn("only 1 bar between 2025-10-26 and 2026-10-25", detail)
        self.assertIn("0% of 260 sessions", detail)
        self.assertNotIn("NSE lists", detail)
        self.assertNotIn(".NS", detail)

    def test_a_failing_master_lookup_still_yields_the_sparse_error(self):
        # The hint is best effort: an NSE that throttled us must not replace the
        # reason the prices are missing with "vendor unavailable".
        with mock.patch(
            "tradingagents.dataflows.vendors.nse.issuer.issuer_for",
            side_effect=VendorUnavailableError("NSE is throttling"),
        ), self.assertRaises(NoMarketDataError) as ctx:
            self._call("RELIANCE.BO", _frame(["2026-10-23"]))

        detail = ctx.exception.detail
        self.assertIn("only 1 bar between 2025-10-26 and 2026-10-25", detail)
        self.assertIn("sparse", detail)
        self.assertNotIn("NSE lists", detail)

    def test_a_complete_bo_frame_passes_through(self):
        with mock.patch(
            "tradingagents.dataflows.vendors.nse.issuer.issuer_for"
        ) as lookup:
            result, _ = self._call("RELIANCE.BO", _frame(_session_days(245)))

        self.assertIn("# Total records: 245", result)
        self.assertIn("# Stock data for RELIANCE.BO", result)
        # The hint costs a request only when a gap was already found.
        lookup.assert_not_called()

    def test_ns_with_the_same_sparse_frame_is_unaffected(self):
        result, _ = self._call("RELIANCE.NS", _frame(["2026-10-23"]))

        self.assertIn("# Total records: 1", result)
        self.assertIn("# Stock data for RELIANCE.NS", result)

    def test_a_us_symbol_with_the_same_sparse_frame_is_unaffected(self):
        result, _ = self._call("NVDA", _frame(["2026-10-23"]))

        self.assertIn("# Total records: 1", result)
        self.assertIn("# Stock data for NVDA", result)

    def test_a_short_window_is_never_judged(self):
        # Ten weekdays, one bar: below the 20-session floor, so no guard.
        result, _ = self._call("RELIANCE.BO", _frame(["2026-10-23"]), "2026-10-12", END)

        self.assertIn("# Total records: 1", result)

    def test_an_empty_frame_keeps_raise_for_empty_behaviour(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        def _empty(symbol, canonical, what):
            raise NoMarketDataError(symbol, canonical, f"no {what}")

        with mock.patch.object(
            yahoo_market, "raise_for_empty", side_effect=_empty
        ) as guard, self.assertRaises(NoMarketDataError) as ctx:
            self._call("RELIANCE.BO", empty)

        guard.assert_called_once()
        self.assertEqual(ctx.exception.detail, f"no rows between {START} and {END}")


@pytest.mark.unit
class BoTruncatedTapeTests(unittest.TestCase):
    """The indicator/snapshot path, where ``load_ohlcv`` sees no window.

    A one-bar tape still has a current row, so the staleness guard passes and
    stockstats would render a "50 SMA" from that single close. Depth is the only
    signal available here, and five bars is the floor below which no indicator
    can mean anything.
    """

    def _load(self, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        with mock.patch.object(yahoo_ohlcv, "yf", _FakeYFinance(frame)):
            return yahoo_ohlcv.load_ohlcv(symbol, END)

    def test_bo_tape_with_one_bar_is_rejected_for_indicators(self):
        issuer = Issuer(
            symbol="THIN", name="THIN INDUSTRIES LIMITED",
            isin="INE000A01001", series="EQ",
        )
        with mock.patch(
            "tradingagents.dataflows.vendors.nse.issuer.issuer_for", return_value=issuer
        ), self.assertRaises(NoMarketDataError) as ctx:
            self._load("THIN.BO", _frame(["2026-10-23"]))

        detail = ctx.exception.detail
        self.assertIn("only 1 bar of price history", detail)
        self.assertIn("sparse", detail)
        self.assertIn("'THIN.NS'", detail)

    def test_bo_tape_at_the_floor_is_served(self):
        data = self._load("TRUNC1.BO", _frame(_session_days(5)))

        self.assertEqual(len(data), 5)

    def test_ns_tape_with_one_bar_is_unaffected(self):
        data = self._load("TRUNCATED.NS", _frame(["2026-10-23"]))

        self.assertEqual(len(data), 1)

    def test_us_symbol_with_one_bar_is_unaffected(self):
        data = self._load("TESTUS", _frame(["2026-10-23"]))

        self.assertEqual(len(data), 1)


if __name__ == "__main__":
    unittest.main()
