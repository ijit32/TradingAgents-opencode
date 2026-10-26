"""The India query builder: name resolution, ambiguity and the date-clause shape.

These are the decisions that make a search find the company rather than a
homonym: ``M&M`` is music equipment, ``"Mahindra & Mahindra"`` is the issuer.
Every test is pure or mocks the single issuer-master call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from tradingagents.dataflows.vendors.india_news import query
from tradingagents.dataflows.vendors.nse.issuer import Issuer

_NAME_FOR_ISSUER = "tradingagents.dataflows.vendors.india_news.query.issuer.issuer_for"


def _issuer(name: str, symbol: str = "RELIANCE") -> Issuer:
    return Issuer(symbol=symbol, name=name, isin="INE002A01018", series="EQ")


@pytest.mark.unit
class TestDisplayName:
    def test_strips_the_registered_suffix(self):
        assert query.display_name("Reliance Industries Limited") == "Reliance Industries"
        assert query.display_name("Larsen & Toubro Ltd.") == "Larsen & Toubro"

    def test_keeps_an_embedded_limited(self):
        assert query.display_name("Limited Editions India") == "Limited Editions India"

    def test_ampersand_spacing_is_left_for_the_fold(self):
        assert query.title_key("Mahindra & Mahindra") == query.title_key("Mahindra and Mahindra")
        assert query.title_key("M & M") == query.title_key("M&M")


@pytest.mark.unit
class TestTitleKey:
    def test_folds_punctuation_and_case(self):
        assert query.title_key("Reliance Q2 net profit rises 9%!") == "reliance q2 net profit rises 9"

    def test_google_publisher_suffix_is_removed(self):
        assert (
            query.strip_publisher("Reliance Q2 profit rises - Business Standard", "Business Standard")
            == "Reliance Q2 profit rises"
        )

    def test_a_suffix_that_is_not_the_publisher_stays(self):
        title = "Reliance Q2 profit rises - Explained"
        assert query.strip_publisher(title, "Business Standard") == title


@pytest.mark.unit
class TestNameResolution:
    def test_uses_the_exchange_master(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=_issuer("Reliance Industries Limited")):
            assert query.name_for("RELIANCE.NS") == "Reliance Industries"

    def test_missing_issuer_is_empty_not_an_error(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=None):
            assert query.name_for("UNKNOWN.NS") == ""

    def test_us_ticker_never_asks_nse(self):
        with mock.patch(_NAME_FOR_ISSUER) as issuer_for:
            assert query.name_for("AAPL") == ""
        issuer_for.assert_not_called()


@pytest.mark.unit
class TestAmbiguity:
    @pytest.mark.parametrize("symbol,expected", [("M&M", True), ("M&MFIN", True), ("AB", True)])
    def test_punctuated_and_short_symbols_are_ambiguous(self, symbol, expected):
        assert query.is_ambiguous(symbol) is expected

    @pytest.mark.parametrize("symbol", ["RELIANCE", "ITC", "TCS", "SUNPHARMA"])
    def test_named_symbols_are_not_ambiguous(self, symbol):
        assert not query.is_ambiguous(symbol)


@pytest.mark.unit
class TestGoogleQueries:
    WINDOW = ("2025-06-01", "2025-06-07")

    def test_name_symbol_and_exchange_terms(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=_issuer("Reliance Industries Limited")):
            queries = query.google_queries("RELIANCE.NS", *self.WINDOW)
        assert queries[0] == (
            '"Reliance Industries" (RELIANCE OR NSE OR BSE) '
            "after:2025-06-01 before:2025-06-08"
        )
        assert queries[1] == '"Reliance Industries" after:2025-06-01 before:2025-06-08'

    def test_ambiguous_symbol_falls_back_to_the_quoted_name(self):
        with mock.patch(
            _NAME_FOR_ISSUER, return_value=_issuer("Mahindra & Mahindra Limited", "M&M")
        ):
            queries = query.google_queries("M&M.NS", *self.WINDOW)
        assert queries == ['"Mahindra & Mahindra" after:2025-06-01 before:2025-06-08']

    def test_without_a_name_the_symbol_still_searches(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=None):
            queries = query.google_queries("TCS.NS", *self.WINDOW)
        assert queries == ["TCS (NSE OR BSE) after:2025-06-01 before:2025-06-08"]

    def test_the_after_clause_crosses_a_month_boundary(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=None):
            queries = query.google_queries("TCS.NS", "2025-01-30", "2025-01-31")
        assert "after:2025-01-30 before:2025-02-01" in queries[0]

    def test_gdelt_query_is_the_quoted_name(self):
        with mock.patch(_NAME_FOR_ISSUER, return_value=_issuer("Reliance Industries Limited")):
            assert query.gdelt_query("RELIANCE.NS") == '"Reliance Industries"'
        with mock.patch(_NAME_FOR_ISSUER, return_value=None):
            assert query.gdelt_query("TCS.NS") == "TCS NSE"


@pytest.mark.unit
def test_article_is_frozen():
    article = query.Article(
        title="Reliance Q2", publisher="BS", link="https://example.test/a",
        published=datetime(2025, 6, 2, tzinfo=UTC),
    )
    with pytest.raises(AttributeError):
        article.title = "changed"  # type: ignore[misc]
