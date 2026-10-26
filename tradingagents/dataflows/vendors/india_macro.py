"""India macro vendor: market quotes from Yahoo, vintage-pinned macro from FRED.

The news analyst needs Indian macro context — the rupee, the volatility index,
the two headline equity indices, and the macro series that move them. Two free
sources cover most of it, and this module is the seam between them:

* **Yahoo** (``INR=X``, ``^INDIAVIX``, ``^NSEI``, ``^BSESN``) is a *live* series.
  A historical run therefore clips the returned history to ``as_of_date`` and
  discards anything after it; the quote itself is today's, not a stored vintage.
  That is honest for a price (a close is not revised) but it must never be read
  as point-in-time for an index constituent list or a fundamentals figure.
* **FRED** mirrors the OECD/World Bank India series and, crucially, records the
  realtime vintage of every observation. Delegating to ``fred.get_macro_data``
  reuses that pinning, so a backtest sees the CPI or GDP that was actually
  published by ``as_of_date``, not a later revision.

The RBI repo rate has no free point-in-time source wired in. The RBI's page is
not an archived feed, so a historical run would silently read today's rate; the
series is reported as an explicit gap rather than estimated.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import NamedTuple

import pandas as pd
import requests
import yfinance as yf

from tradingagents.dataflows.errors import VendorDeclinedError, VendorError
from tradingagents.dataflows.vendors import fred
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

logger = logging.getLogger(__name__)

# Default trailing window, matching FRED: a year captures a trend and the
# year-over-year base of the monthly series in the same report.
DEFAULT_LOOKBACK_DAYS = 365

# Daily index/FX series would otherwise flood the analyst's context over a long
# window; the recent tail is what a decision uses.
MAX_ROWS = 40


class YahooSeries(NamedTuple):
    """One Yahoo symbol plus how its values should be read and rendered."""

    symbol: str
    label: str
    units: str
    decimals: int
    # An index level's window change is a return, not a quantity: calling it a
    # "change" invites the analyst to treat index points as currency.
    trailing_return: bool = False


# Curated aliases -> Yahoo symbols. Yahoo is used rather than an Indian vendor
# here because these four quotes are the ones Yahoo serves without a key and
# with a history endpoint that can be clipped to the analysis date.
YAHOO_INDIA_SERIES = {
    "usd_inr": YahooSeries("INR=X", "USD/INR", "INR per USD", 4),
    "india_vix": YahooSeries("^INDIAVIX", "India VIX", "Index points", 2),
    "nifty_50": YahooSeries("^NSEI", "Nifty 50", "Index points", 2, trailing_return=True),
    "sensex": YahooSeries("^BSESN", "BSE Sensex", "Index points", 2, trailing_return=True),
}

# FRED mirrors the OECD India series under these IDs, which is what buys the
# realtime vintage pin. They need FRED_API_KEY (free), so they are advertised as
# available but reported as missing coverage when the key is absent — never as a
# flat reading.
FRED_INDIA_SERIES = {
    "cpi_india": "CPALTT01INM659N",  # CPI, growth rate same period previous year
    "gsec_10y_india": "INDIRLTLT01STM",  # 10-year g-sec yield, percent
    "iip_india": "INDPROINDMISMEI",  # industrial production index, 2015=100
    "real_gdp_india": "INDGDPRQPSMEI",  # real GDP, growth rate same period previous year
}

# The alias is an explicit gap, not an unimplemented alias: no free source
# publishes the RBI's rate history as an archived, date-addressable feed.
REPO_RATE_INDIA = {
    "alias": "repo_rate_india",
    "label": "RBI policy repo rate",
    "units": "percent",
    "frequency": "policy announcement",
}


def _normalize(indicator: str) -> str:
    """Lowercase and turn spaces/hyphens into underscores.

    Same normalization as ``fred._resolve_series_id`` so an alias reads the same
    whichever vendor the analyst is talking to ("USD-INR" -> "usd_inr").
    """
    return indicator.strip().lower().replace(" ", "_").replace("-", "_")


def _format_value(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def _not_a_flat_reading(label: str, as_of_date: str) -> str:
    """The sentence every gap shares: no number is not a value of zero."""
    return (
        f"This is missing coverage, not a flat reading: no observation is "
        f"available for {label} on or before {as_of_date}."
    )


def _yahoo_header(series: YahooSeries, start_date: str, as_of_date: str) -> str:
    return (
        f"## Yahoo Finance: {series.label} ({series.symbol})\n"
        f"- Units: {series.units}\n"
        f"- Frequency: Daily (exchange close)\n"
        f"- Window: {start_date} to {as_of_date}\n"
    )


def _yahoo_gap_report(
    series: YahooSeries, start_date: str, as_of_date: str, reason: str
) -> str:
    return (
        _yahoo_header(series, start_date, as_of_date)
        + f"\n{reason} "
        + _not_a_flat_reading(f"{series.label} ({series.symbol})", as_of_date)
        + "\n"
    )


def _points_from_frame(frame: pd.DataFrame, as_of_date: str) -> list[tuple[str, float]]:
    """Daily closes on or before ``as_of_date``, oldest first.

    yfinance treats ``end`` as exclusive, so the request already stops after
    ``as_of_date``; the explicit date filter is defence in depth. A vendor that
    ignores the end bound (or a cached response) must not put a future close into
    a historical report.
    """
    points: list[tuple[str, float]] = []
    for when, close in zip(frame.index, frame["Close"], strict=True):
        if pd.isna(close):
            continue
        stamp = pd.Timestamp(when)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        day = stamp.strftime("%Y-%m-%d")
        if day > as_of_date:
            continue
        points.append((day, float(close)))
    points.sort(key=lambda item: item[0])
    return points


def _yahoo_summary(series: YahooSeries, points: list[tuple[str, float]]) -> str:
    first_date, first_val = points[0]
    last_date, last_val = points[-1]
    latest = _format_value(last_val, series.decimals)
    if len(points) == 1:
        return f"\n**Latest:** {latest} ({last_date})\n"

    delta = last_val - first_val
    pct = f"{delta / first_val * 100:+.2f}%" if first_val else "n/a (base is zero)"
    if series.trailing_return:
        return (
            f"\n**Latest:** {latest} ({last_date}) | "
            f"**Trailing return from {first_date} to {last_date}:** {pct}, "
            f"from {_format_value(first_val, series.decimals)}\n"
        )
    return (
        f"\n**Latest:** {latest} ({last_date}) | "
        f"**Change from {first_date} to {last_date}:** "
        f"{delta:+,.{series.decimals}f} ({pct}), "
        f"from {_format_value(first_val, series.decimals)}\n"
    )


def _yahoo_report(
    series: YahooSeries, start_date: str, as_of_date: str
) -> str:
    """Fetch one Yahoo series and format it, or name the gap explicitly."""
    # end is EXCLUSIVE in yfinance, so ask for the day after as_of_date to keep
    # the as-of row itself (the same rule as the price path, #986/#987).
    end_exclusive = (
        datetime.strptime(as_of_date, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    try:
        frame = yf_retry(
            lambda: yf.Ticker(series.symbol).history(
                start=start_date, end=end_exclusive, interval="1d"
            )
        )
    except (VendorError, requests.RequestException) as exc:
        logger.warning("Yahoo Finance unavailable for %s: %s", series.symbol, exc)
        return _yahoo_gap_report(
            series,
            start_date,
            as_of_date,
            f"Yahoo Finance could not be fetched for {series.symbol} between "
            f"{start_date} and {as_of_date}: {exc}.",
        )

    # yf_retry returns None when Yahoo answers that it has nothing; an empty
    # frame is the same answer. No network health probe: a failure already raised.
    if frame is None or frame.empty or "Close" not in frame:
        return _yahoo_gap_report(
            series,
            start_date,
            as_of_date,
            f"Yahoo Finance returned no observations for {series.symbol} between "
            f"{start_date} and {as_of_date}.",
        )

    frame = frame.copy()
    if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
        frame.index = frame.index.tz_localize(None)

    points = _points_from_frame(frame, as_of_date)
    if not points:
        return _yahoo_gap_report(
            series,
            start_date,
            as_of_date,
            f"Yahoo Finance returned no observations for {series.symbol} between "
            f"{start_date} and {as_of_date}.",
        )

    shown = points
    note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"

    table = (
        "\n| Date | Value |\n| --- | --- |\n"
        + "\n".join(
            f"| {day} | {_format_value(value, series.decimals)} |" for day, value in shown
        )
        + "\n"
    )

    return (
        _yahoo_header(series, start_date, as_of_date)
        + _yahoo_summary(series, points)
        + note
        + table
    )


def _fred_gap_report(alias: str, series_id: str, as_of_date: str, reason: str) -> str:
    return (
        f"## FRED: {alias} ({series_id})\n\n"
        f"{reason} This is missing coverage, not a flat reading: no value is "
        f"available for {alias} as of {as_of_date}.\n"
    )


def _fred_report(
    alias: str, series_id: str, as_of_date: str, look_back_days: int
) -> str:
    """Delegate a mapped India series to FRED so its vintage pinning is reused."""
    if not os.getenv("FRED_API_KEY"):
        return _fred_gap_report(
            alias,
            series_id,
            as_of_date,
            "FRED_API_KEY is not set, so this series was not fetched.",
        )

    try:
        return fred.get_macro_data(series_id, as_of_date, look_back_days)
    except (VendorError, requests.RequestException) as exc:
        # Returning a notice instead of raising matters for the default
        # ``india_macro,fred`` chain: the router would otherwise retry FRED with
        # the ALIAS, which FRED does not know, and report a 400 rather than the
        # real gap.
        logger.warning("FRED India series %s could not be fetched: %s", series_id, exc)
        return _fred_gap_report(
            alias,
            series_id,
            as_of_date,
            f"FRED series {series_id} could not be fetched: {exc}.",
        )


def _repo_rate_notice(as_of_date: str) -> str:
    return (
        f"## India macro: {REPO_RATE_INDIA['label']} ({REPO_RATE_INDIA['alias']})\n"
        f"- Units: {REPO_RATE_INDIA['units']}\n"
        f"- Frequency: {REPO_RATE_INDIA['frequency']}\n"
        f"- Window: policy decision in force on {as_of_date}\n\n"
        f"There is no free point-in-time source wired in for the RBI repo rate. "
        f"The RBI page is not an archived feed, so a historical run would read "
        f"today's rate rather than the rate in force on {as_of_date}. The series "
        f"is not estimated: report it as not covered instead of substituting a "
        f"current value.\n"
    )


def get_macro_data(
    indicator: str,
    as_of_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch an India macro indicator as a formatted markdown report.

    Args:
        indicator: An India alias (``usd_inr``, ``india_vix``, ``nifty_50``,
            ``sensex``, or a FRED-backed ``cpi_india``, ``gsec_10y_india``,
            ``iip_india``, ``real_gdp_india``). ``repo_rate_india`` answers with
            an explicit gap notice.
        as_of_date: The as-of date (yyyy-mm-dd). It bounds the trailing window
            and, for Yahoo, clips the history: a historical run must not see a
            later close. FRED-backed aliases additionally pin the data vintage
            through ``fred.get_macro_data``.
        look_back_days: Trailing window length; ``None`` uses
            DEFAULT_LOOKBACK_DAYS.

    Raises:
        VendorDeclinedError: when ``indicator`` is not an India macro alias.
            This is the self-gate that lets the router's ``india_macro,fred``
            chain fall through for US aliases such as ``cpi``: this vendor never
            checked them, so the router must not turn the decline into a
            ``NO_DATA_AVAILABLE`` verdict that would mask FRED's real answer.
    """
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_date = (end_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    key = _normalize(indicator)

    series = YAHOO_INDIA_SERIES.get(key)
    if series is not None:
        return _yahoo_report(series, start_date, as_of_date)

    series_id = FRED_INDIA_SERIES.get(key)
    if series_id is not None:
        return _fred_report(key, series_id, as_of_date, look_back_days)

    if key == REPO_RATE_INDIA["alias"]:
        return _repo_rate_notice(as_of_date)

    raise VendorDeclinedError(f"{indicator!r} is not an India macro alias")
