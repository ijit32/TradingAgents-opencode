"""Global news follows the run's market: an NSE/BSE listing searches India-phrased
macro headlines, while a US listing and a run with no instrument keep the
configured global list unchanged.
"""

from datetime import UTC, datetime
from unittest import mock

import pytest

import tradingagents.dataflows.vendors.yahoo.news as ynews
from tradingagents.agents import tools

AS_OF = "2026-10-23"
US_QUERIES = [
    "Federal Reserve interest rates inflation",
    "S&P 500 earnings GDP economic outlook",
]
INDIA_QUERIES = [
    "RBI repo rate monetary policy inflation India",
    "NSE Nifty Sensex earnings results quarterly",
    "rupee USD/INR FII foreign institutional flows",
    "SEBI corporate governance India regulatory",
]


def _epoch(date_str: str) -> int:
    """Epoch seconds for UTC midnight of ``date_str`` (host-timezone independent)."""
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _fake_search(monkeypatch):
    """Yahoo Search recording every query and answering one in-window article."""
    searched = []

    class FakeSearch:
        def __init__(self, query, **kwargs):
            searched.append(query)
            self.news = [{
                "title": f"HEADLINE for {query}",
                "publisher": "P",
                "link": "l",
                "providerPublishTime": _epoch("2026-10-20"),
            }]

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    return searched


def _config(monkeypatch, **overrides):
    config = {
        "global_news_lookback_days": 7,
        "global_news_article_limit": 10,
        "global_news_queries": list(US_QUERIES),
        "global_news_queries_by_market": {"IN": list(INDIA_QUERIES)},
    }
    config.update(overrides)
    monkeypatch.setattr(ynews, "get_config", lambda: config)


@pytest.mark.unit
def test_an_nse_listing_searches_the_india_queries(monkeypatch):
    searched = _fake_search(monkeypatch)
    _config(monkeypatch)

    out = ynews.get_global_news_yfinance(AS_OF, ticker="RELIANCE.NS")

    assert searched == INDIA_QUERIES
    assert "HEADLINE" in out


@pytest.mark.unit
def test_a_bse_listing_searches_the_india_queries(monkeypatch):
    searched = _fake_search(monkeypatch)
    _config(monkeypatch)

    ynews.get_global_news_yfinance(AS_OF, ticker="RELIANCE.BO")

    assert searched == INDIA_QUERIES


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["AAPL", ""])
def test_a_us_run_searches_only_the_global_queries(monkeypatch, ticker):
    searched = _fake_search(monkeypatch)
    _config(monkeypatch)

    ynews.get_global_news_yfinance(AS_OF, ticker=ticker)

    # The US path is byte-for-byte the configured list, in order.
    assert searched == US_QUERIES


@pytest.mark.unit
def test_no_ticker_at_all_is_the_unchanged_us_path(monkeypatch):
    searched = _fake_search(monkeypatch)
    _config(monkeypatch)

    ynews.get_global_news_yfinance(AS_OF)

    assert searched == US_QUERIES


@pytest.mark.unit
@pytest.mark.parametrize("overlay", [{}, {"IN": []}, None])
def test_an_empty_or_missing_india_overlay_falls_back_to_the_global_queries(monkeypatch, overlay):
    searched = _fake_search(monkeypatch)
    config_kwargs = {} if overlay is None else {"global_news_queries_by_market": overlay}
    _config(monkeypatch, **config_kwargs)
    if overlay is None:
        monkeypatch.setattr(ynews, "get_config", lambda: {
            "global_news_lookback_days": 7,
            "global_news_article_limit": 10,
            "global_news_queries": list(US_QUERIES),
        })

    ynews.get_global_news_yfinance(AS_OF, ticker="RELIANCE.NS")

    # Not zero queries: a misconfigured overlay still runs the global list.
    assert searched == US_QUERIES


@pytest.mark.unit
def test_the_tool_routes_the_run_instrument_to_the_vendor(monkeypatch):
    with mock.patch.object(tools, "route_to_vendor", return_value="ok") as routed:
        tools.get_global_news.func(curr_date=AS_OF, ticker="RELIANCE.NS")

    assert routed.call_args.args == ("get_global_news", AS_OF, None, None, "RELIANCE.NS")


@pytest.mark.unit
def test_the_ticker_argument_is_hidden_from_the_model():
    properties = tools.get_global_news.tool_call_schema.model_json_schema()["properties"]
    assert "ticker" not in properties
    assert "trade_date" not in properties
