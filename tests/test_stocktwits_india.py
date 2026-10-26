"""StockTwits has no NSE/BSE stream: an India ticker must not be requested.

StockTwits keys streams by US cashtags, so ``TCS.NS`` 404s, and stripping the
suffix would fetch the Container Store's stream under the same letters. Either
way the empty answer reads as a quiet Indian market when in fact nothing was
asked. The gate is therefore suffix-only (``markets.is_india``) and runs before
the URL is built, so no request leaves the process for an Indian listing.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from tradingagents.dataflows.vendors import stocktwits


class _JsonResp:
    """Minimal urlopen() context-manager stub returning a JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _stream():
    return _JsonResp({"messages": [
        {"body": "long TCS", "created_at": "2026-01-09T15:00:00Z",
         "user": {"username": "u"}, "entities": {"sentiment": {"basic": "Bullish"}}},
        {"body": "short TCS", "created_at": "2026-01-09T14:00:00Z",
         "user": {"username": "v"}, "entities": {"sentiment": {"basic": "Bearish"}}},
    ]})


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["TCS.NS", "TCS.BO", "reliance.ns"])
def test_india_listing_returns_placeholder_without_a_request(ticker):
    urlopen = Mock()
    with patch.object(stocktwits, "urlopen", urlopen):
        out = stocktwits.fetch_stocktwits_messages(ticker)
    assert out == stocktwits.INDIA_NOT_APPLICABLE
    urlopen.assert_not_called()


@pytest.mark.unit
def test_bare_tcs_is_a_us_ticker_and_is_fetched():
    # A bare symbol carries no exchange, so it is the Container Store: gating it
    # as India would silently swap one company's chatter for another's.
    urlopen = Mock(return_value=_stream())
    with patch.object(stocktwits, "urlopen", urlopen):
        out = stocktwits.fetch_stocktwits_messages("TCS")
    urlopen.assert_called_once()
    assert "Total: 2 most-recent messages" in out


@pytest.mark.unit
def test_crypto_pair_still_reaches_the_stream():
    # The gate reads the suffix only, so a dashed crypto pair is not India.
    urlopen = Mock(return_value=_stream())
    with patch.object(stocktwits, "urlopen", urlopen):
        stocktwits.fetch_stocktwits_messages("BTC-USD")
    urlopen.assert_called_once()
    assert "/symbol/BTC.X.json" in urlopen.call_args.args[0].full_url


@pytest.mark.unit
def test_placeholder_is_not_a_coverage_gap():
    # "unavailable" and "no StockTwits messages" both claim the market was read
    # and found empty; an inapplicable source made no claim at all.
    placeholder = stocktwits.INDIA_NOT_APPLICABLE
    assert "unavailable" not in placeholder
    assert "no StockTwits messages" not in placeholder
