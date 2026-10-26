"""Google News RSS: parsing, the IST window boundary, dedupe and query fallback.

Every test is offline: the transport seam every NSE adapter shares
(``nse.client.get_bytes``) is mocked, and the fixtures are hand-written RSS
documents shaped like Google's, with the timestamps the point-in-time rules turn
on — an item on the window's last UTC day that is already the next IST day, and
an undated item that the window cannot place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.ist import today_ist
from tradingagents.dataflows.vendors.india_news import google

_REPO = Path(__file__).resolve().parents[1]
_NEWS_DIR = _REPO / "tests" / "fixtures" / "india" / "news"
_GET_BYTES = "tradingagents.dataflows.vendors.nse.client.get_bytes"
_NAME_FOR = "tradingagents.dataflows.vendors.india_news.query.name_for"

_WINDOW = ("2025-10-06", "2025-10-09")


def _fixture(name: str) -> bytes:
    return (_NEWS_DIR / name).read_bytes()


@pytest.mark.unit
def test_parses_the_in_window_item_and_drops_the_next_ist_day():
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("two_items.xml")) as get_bytes,
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        articles, notes = google.fetch("RELIANCE.NS", *_WINDOW)

    assert [article.title for article in articles] == [
        "Reliance Industries posts record Q2 profit"
    ]
    article = articles[0]
    assert article.publisher == "Business Standard"
    assert article.published == datetime(2025, 10, 6, 7, 30, tzinfo=UTC)
    assert article.published.utcoffset() == timedelta(0)
    assert article.link.startswith("https://news.google.com/rss/articles/")
    # 09 Oct 19:30 UTC is 01:00 IST on 10 Oct — the day after the window's last
    # IST day — so it is the IST comparison, not the UTC date, that drops it.
    assert articles[0].title != "Reliance arm plans EV plant in Pune"
    assert notes == []
    get_bytes.assert_called_once()


@pytest.mark.unit
def test_escaped_html_is_tolerated_and_a_missing_source_gets_the_fallback_publisher():
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("no_source.xml")),
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        articles, _ = google.fetch("RELIANCE.NS", *_WINDOW)

    # The description carries escaped markup and the item carries no <source>.
    assert [article.publisher for article in articles] == ["Google News"]
    assert [article.title for article in articles] == [
        "Reliance Industries signs battery deal"
    ]
    assert articles[0].published == datetime(2025, 10, 7, 4, 15, tzinfo=UTC)


@pytest.mark.unit
def test_the_same_story_from_two_publishers_is_kept_once():
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("duplicate.xml")),
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        articles, _ = google.fetch("RELIANCE.NS", *_WINDOW)

    assert len(articles) == 1
    assert articles[0].title == "Reliance Q2 profit rises 12%"


@pytest.mark.unit
def test_the_company_name_only_query_is_tried_when_the_symbol_query_finds_nothing():
    documents = [_fixture("out_of_window.xml"), _fixture("two_items.xml")]
    with (
        mock.patch(_GET_BYTES, side_effect=documents) as get_bytes,
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        articles, notes = google.fetch("RELIANCE.NS", *_WINDOW)

    assert get_bytes.call_count == 2
    first_key, second_key = (call.kwargs["key"] for call in get_bytes.call_args_list)
    assert "(RELIANCE OR NSE OR BSE)" in first_key
    assert "(RELIANCE OR NSE OR BSE)" not in second_key
    assert '"Reliance Industries"' in second_key
    assert [article.title for article in articles] == [
        "Reliance Industries posts record Q2 profit"
    ]
    assert any("company-name-only" in note for note in notes)
    assert any("symbol query returned no article" in note for note in notes)


@pytest.mark.unit
@pytest.mark.parametrize("document", ["block_page.html", "not_rss.xml"])
def test_a_document_that_is_not_rss_is_a_vendor_failure(document):
    with (
        mock.patch(_GET_BYTES, return_value=_fixture(document)),
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
        pytest.raises(VendorUnavailableError, match="Google News returned a document"),
    ):
        google.fetch("RELIANCE.NS", *_WINDOW)


@pytest.mark.unit
def test_no_in_window_item_is_an_empty_list_with_a_note_not_an_exception():
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("out_of_window.xml")) as get_bytes,
        mock.patch(_NAME_FOR, return_value=""),
    ):
        articles, notes = google.fetch("RELIANCE.NS", *_WINDOW)

    assert articles == []
    assert isinstance(notes, list)
    assert any("issuer master resolved no company name" in note for note in notes)
    # Neither the old story nor the undated one may be counted, and a symbol-only
    # search is one query, so the feed is asked once.
    get_bytes.assert_called_once()


@pytest.mark.unit
def test_the_request_names_the_indian_edition_and_keys_the_cache_by_query():
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("empty.xml")) as get_bytes,
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        google.fetch("RELIANCE.NS", *_WINDOW)

    url = get_bytes.call_args.args[0]
    assert url.startswith("https://news.google.com/rss/search?")
    params = parse_qs(urlparse(url).query)
    assert params["hl"] == ["en-IN"]
    assert params["gl"] == ["IN"]
    assert params["ceid"] == ["IN:en"]
    search = params["q"][0]
    assert '"Reliance Industries"' in search
    assert "after:2025-10-06" in search and "before:2025-10-10" in search
    assert get_bytes.call_args.kwargs["bucket"] == "google_news"
    assert get_bytes.call_args.kwargs["namespace"] == "india_news"
    assert get_bytes.call_args.kwargs["key"] == f"google:{search}"


@pytest.mark.unit
def test_the_ttl_is_daily_for_a_recent_window_and_weekly_for_an_old_one():
    today = today_ist()
    with (
        mock.patch(_GET_BYTES, return_value=_fixture("empty.xml")) as get_bytes,
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
    ):
        google.fetch(
            "RELIANCE.NS",
            (today - timedelta(days=3)).isoformat(),
            today.isoformat(),
        )
        assert get_bytes.call_args.kwargs["ttl_seconds"] == 24 * 60 * 60

        google.fetch("RELIANCE.NS", *_WINDOW)
        assert get_bytes.call_args.kwargs["ttl_seconds"] == 7 * 24 * 60 * 60
