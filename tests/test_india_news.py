"""What the India news tool returns: articles, filings, and named gaps.

The adapters are tested on their own; this file pins the composition the analyst
reads — section order, dedupe across sources, the filings block from the same
window, and the rule that a source which did not answer is a gap rather than an
absence of news. Every test is offline: the four source functions are patched at
the package boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.india_news import gdelt, get_news, google, publishers
from tradingagents.dataflows.vendors.india_news.query import Article
from tradingagents.dataflows.vendors.nse import filings
from tradingagents.dataflows.vendors.nse.filings import Filing

_WINDOW = ("2025-06-01", "2025-06-07")


def _article(title: str, day: int, publisher: str = "Business Standard") -> Article:
    return Article(
        title=title,
        publisher=publisher,
        link=f"https://example.test/{day}",
        published=datetime(2025, 6, day, 9, 30, tzinfo=UTC),
    )


def _filing(subject: str, day: int, url: str = "https://nsearchives.test/a.pdf") -> Filing:
    return Filing(
        subject=subject,
        published=datetime(2025, 6, day, 14, 35, tzinfo=UTC),  # 20:05 IST
        url=url,
        has_xbrl=False,
        symbol="RELIANCE",
    )


def _sources(
    articles: list[Article] | None = None,
    filings_rows: list[Filing] | None = None,
    gdelt_rows: list[Article] | None = None,
    publisher_rows: list[Article] | None = None,
):
    """Patch all four sources; Google is the one the caller varies most."""
    return (
        mock.patch.object(google, "fetch", return_value=(articles or [], [])),
        mock.patch.object(gdelt, "fetch", return_value=(gdelt_rows or [], [])),
        mock.patch.object(publishers, "fetch", return_value=(publisher_rows or [], [])),
        mock.patch.object(filings, "announcements", return_value=filings_rows or []),
    )


@pytest.mark.unit
class TestIndiaNewsComposition:
    def test_us_ticker_is_refused_before_any_request(self):
        with (
            mock.patch.object(google, "fetch") as google_fetch,
            mock.patch.object(gdelt, "fetch") as gdelt_fetch,
            mock.patch.object(publishers, "fetch") as publishers_fetch,
            mock.patch.object(filings, "announcements") as announcements,
            pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
        ):
            get_news("AAPL", *_WINDOW)
        google_fetch.assert_not_called()
        gdelt_fetch.assert_not_called()
        publishers_fetch.assert_not_called()
        announcements.assert_not_called()

    def test_articles_then_filings_newest_first(self):
        patches = _sources(
            articles=[_article("Older story", 2), _article("Newer story", 5)],
            filings_rows=[_filing("Outcome of Board Meeting", 4)],
        )
        with patches[0], patches[1], patches[2], patches[3]:
            text = get_news("RELIANCE.NS", *_WINDOW)

        assert text.startswith("## RELIANCE.NS news, 2025-06-01 to 2025-06-07")
        assert text.index("Newer story") < text.index("Older story")
        assert "## Exchange filings (NSE), 2025-06-01 to 2025-06-07" in text
        assert "Outcome of Board Meeting" in text
        assert "2025-06-04 20:05 IST" in text
        assert text.index("Older story") < text.index("Exchange filings (NSE)")

    def test_one_story_syndicated_twice_is_kept_once(self):
        with (
            mock.patch.object(google, "fetch", return_value=([_article("Reliance Q2 profit rises", 3)], [])),
            mock.patch.object(
                gdelt,
                "fetch",
                return_value=([_article("Reliance Q2 profit rises", 3, "reuters.com")], []),
            ),
            mock.patch.object(publishers, "fetch", return_value=([], [])),
            mock.patch.object(filings, "announcements", return_value=[]),
        ):
            text = get_news("RELIANCE.NS", *_WINDOW)
        assert text.count("Reliance Q2 profit rises") == 1

    def test_google_failure_is_a_note_not_a_lost_pack(self):
        with (
            mock.patch.object(
                google, "fetch", side_effect=VendorUnavailableError("Google News returned HTTP 429")
            ),
            mock.patch.object(gdelt, "fetch", return_value=([_article("GDELT story", 3)], [])),
            mock.patch.object(publishers, "fetch", return_value=([], [])),
            mock.patch.object(filings, "announcements", return_value=[_filing("Filing", 2)]),
        ):
            text = get_news("RELIANCE.NS", *_WINDOW)

        assert "GDELT story" in text
        assert "Filing" in text
        assert "<india_news unavailable for 2025-06-01..2025-06-07: Google News" in text
        assert "not an absence of news" in text

    def test_every_source_failing_is_not_an_absence(self):
        failing = mock.patch.object(google, "fetch", side_effect=VendorUnavailableError("blocked"))
        with (
            failing,
            mock.patch.object(gdelt, "fetch", side_effect=VendorUnavailableError("blocked")),
            mock.patch.object(publishers, "fetch", side_effect=VendorUnavailableError("blocked")),
            mock.patch.object(filings, "announcements", side_effect=VendorUnavailableError("blocked")),
        ):
            text = get_news("RELIANCE.NS", *_WINDOW)

        assert "No news source answered" in text
        assert "No filing feed answered" in text
        assert text.count("<india_news unavailable") == 4

    def test_answered_empty_window_reads_as_an_absence(self):
        patches = _sources()
        with patches[0], patches[1], patches[2], patches[3]:
            text = get_news("RELIANCE.NS", *_WINDOW)
        assert "No news article was found" in text
        assert "No exchange filing was published" in text
        assert "<india_news unavailable" not in text

    def test_bse_only_scrip_keeps_articles_and_names_the_filing_gap(self):
        with (
            mock.patch.object(google, "fetch", return_value=([_article("BSE story", 3)], [])),
            mock.patch.object(gdelt, "fetch", return_value=([], [])),
            mock.patch.object(publishers, "fetch", return_value=([], [])),
            mock.patch.object(
                filings,
                "announcements",
                side_effect=NoMarketDataError("XYZ.BO", "XYZ", "BSE-only scrip with no NSE symbol"),
            ),
        ):
            text = get_news("XYZ.BO", *_WINDOW)
        assert "BSE story" in text
        assert "Exchange filings (NSE)" in text
        assert "No filing feed answered" in text
        assert "BSE-only scrip" in text

    def test_article_cap_comes_from_config(self):
        many = [_article(f"Story {day}", day) for day in range(1, 8)]
        patches = _sources(articles=many)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            mock.patch(
                "tradingagents.dataflows.vendors.india_news.get_config",
                return_value={"news_article_limit": 3},
            ),
        ):
            text = get_news("RELIANCE.NS", *_WINDOW)
        assert text.count("### Story") == 3

    def test_filings_are_capped_and_linked(self):
        rows = [_filing(f"Filing {day}", 1, f"https://nsearchives.test/{day}.pdf") for day in range(1, 13)]
        patches = _sources(filings_rows=rows)
        with patches[0], patches[1], patches[2], patches[3]:
            text = get_news("RELIANCE.NS", *_WINDOW)
        assert text.count("| Filing") == 10
        assert "https://nsearchives.test/" in text

    def test_reversed_window_is_rejected(self):
        with pytest.raises(ValueError, match="ends before it starts"):
            get_news("RELIANCE.NS", "2025-06-07", "2025-06-01")

    def test_source_notes_are_printed_below_the_filings(self):
        with (
            mock.patch.object(
                google,
                "fetch",
                return_value=([_article("Story", 3)], ["Google News: the symbol query returned no article."]),
            ),
            mock.patch.object(gdelt, "fetch", return_value=([], [])),
            mock.patch.object(publishers, "fetch", return_value=([], [])),
            mock.patch.object(filings, "announcements", return_value=[]),
        ):
            text = get_news("RELIANCE.NS", *_WINDOW)
        assert text.index("Story") < text.index("the symbol query returned no article")
        assert text.index("Exchange filings (NSE)") < text.index("the symbol query returned no article")


@pytest.mark.unit
class TestIndiaNewsRouting:
    """The default chain is ``nse,yfinance``; a US ticker must reach Yahoo unchanged."""

    def _chain(self, nse, yfinance):
        return (
            mock.patch.dict(
                router.VENDOR_METHODS,
                {"get_news": {"nse": nse, "yfinance": yfinance}},
                clear=False,
            ),
            mock.patch.object(router, "get_vendor", return_value="nse,yfinance"),
        )

    def test_us_ticker_falls_through_to_yahoo(self):
        yfinance = mock.Mock(return_value="YAHOO NEWS")
        methods, vendors = self._chain(get_news, yfinance)
        with methods, vendors:
            assert router.route_to_vendor("get_news", "AAPL", *_WINDOW) == "YAHOO NEWS"
        assert yfinance.call_args.args[0] == "AAPL"

    def test_india_ticker_never_reaches_yahoo(self):
        nse = mock.Mock(return_value="INDIA NEWS")
        yfinance = mock.Mock(return_value="YAHOO NEWS")
        methods, vendors = self._chain(nse, yfinance)
        with methods, vendors:
            assert router.route_to_vendor("get_news", "RELIANCE.NS", *_WINDOW) == "INDIA NEWS"
        yfinance.assert_not_called()

