"""Market resolution is pure syntax: the suffix decides, and an unknown one guesses nothing."""

import pytest

from tradingagents.dataflows.markets import (
    INDIA_INDEX_SYMBOLS,
    india_exchange,
    india_ticker,
    is_india,
    market_for,
    report_currency,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "symbol",
    ["RELIANCE.NS", "reliance.ns", "  RELIANCE.NS  ", "RELIANCE.BO", "reliance.bo"],
)
def test_india_suffix_is_india(symbol):
    assert market_for(symbol) == "IN"
    assert is_india(symbol)


@pytest.mark.unit
def test_ampersand_symbol_is_india():
    """``M&M.NS`` is a real NSE scrip: the suffix decides, not the ticker text."""
    assert market_for("M&M.NS") == "IN"
    assert india_ticker("m&m.ns") == "M&M"


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["TCS", "AAPL", "GC=F", "BTC-USD", "^GSPC"])
def test_a_symbol_with_no_suffix_is_us(symbol):
    assert market_for(symbol) == "US"
    assert not is_india(symbol)


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["RELIANCE.L", "7203.T", "SHOP.TO", "600519.SS"])
def test_an_unmodelled_suffix_is_other_not_us(symbol):
    assert market_for(symbol) == "OTHER"
    assert not is_india(symbol)


@pytest.mark.unit
@pytest.mark.parametrize("symbol", sorted(INDIA_INDEX_SYMBOLS))
def test_indian_indices_are_india(symbol):
    """An index has no exchange suffix; it is India because it is named as such."""
    assert market_for(symbol) == "IN"
    assert is_india(symbol.lower())


@pytest.mark.unit
def test_bare_tcs_and_tcs_ns_are_different_markets():
    """``TCS`` is the US Container Store; ``TCS.NS`` is Tata Consultancy."""
    assert market_for("TCS") == "US"
    assert market_for("TCS.NS") == "IN"
    assert india_ticker("TCS.NS") == "TCS"
    with pytest.raises(ValueError, match="not an NSE/BSE listing"):
        india_ticker("TCS")


@pytest.mark.unit
def test_a_plain_stock_with_a_dot_is_neither_india_nor_a_crash():
    """``BRK.B``'s dot is part of its ticker, not an exchange suffix."""
    assert market_for("BRK.B") == "OTHER"
    assert not is_india("BRK.B")
    assert india_exchange("BRK.B") == ""
    with pytest.raises(ValueError, match="not an NSE/BSE listing"):
        india_ticker("BRK.B")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "exchange"),
    [
        ("RELIANCE.NS", "NSE"),
        ("reliance.bo", "BSE"),
        ("^NSEI", ""),
        ("TCS", ""),
        ("BRK.B", ""),
    ],
)
def test_india_exchange(symbol, exchange):
    assert india_exchange(symbol) == exchange


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["RELIANCE.L", "7203.T", "AAPL", "^GSPC"])
def test_india_ticker_refuses_a_non_india_symbol(symbol):
    with pytest.raises(ValueError, match="not an NSE/BSE listing"):
        india_ticker(symbol)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "currency"),
    [
        ("RELIANCE.NS", "INR"),
        ("reliance.bo", "INR"),
        ("^NSEI", "INR"),
        ("AAPL", "USD"),
        ("GC=F", "USD"),
        ("RELIANCE.L", ""),
        ("7203.T", ""),
        ("BRK.B", ""),
    ],
)
def test_report_currency(symbol, currency):
    assert report_currency(symbol) == currency


@pytest.mark.unit
def test_an_unverified_currency_is_empty_not_guessed():
    """``.L`` lines quote in GBp as often as GBP, so the caller is told nothing."""
    assert report_currency("VOD.L") == ""
