"""GDELT's news pass: the IST trim, the 90-day horizon and its visible clamp.

Every test is offline. The transport is mocked at ``client.get_json`` (the seam
every India adapter shares) and today's date is frozen by replacing the
module-level ``today_ist`` import, so the cutoff arithmetic is pinned to a fixed
day rather than to the day the suite runs. The payload is a hand-written GDELT
``artlist`` response with its real field names and obviously synthetic values,
including the stamps each rule turns on: a story after IST midnight of
``end_date``, an unparseable ``seendate``, a blank ``domain`` and one headline
syndicated twice.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.india_news import gdelt

_FIXTURE = Path(__file__).parent / "fixtures" / "india" / "news" / "gdelt.json"
_GET_JSON = "tradingagents.dataflows.vendors.nse.client.get_json"
_NAME_FOR = "tradingagents.dataflows.vendors.india_news.query.name_for"

# Frozen "today": the cutoff is then 2025-07-12, which lets the main window sit
# wholly inside GDELT's reach and the straddle test cross it on both sides.
_TODAY = date(2025, 10, 10)

_WINDOW = ("2025-10-06", "2025-10-08")


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    """Freeze the clock and resolve the issuer name without touching NSE."""
    monkeypatch.setattr(gdelt, "today_ist", lambda: _TODAY)
    monkeypatch.setattr(_NAME_FOR, lambda ticker: "Mahindra & Mahindra")


def _answer(monkeypatch, payload) -> mock.MagicMock:
    get_json = mock.MagicMock(return_value=payload)
    monkeypatch.setattr(_GET_JSON, get_json)
    return get_json


@pytest.mark.unit
def test_articles_are_parsed_in_ist_order_and_trimmed_to_the_window(monkeypatch):
    get_json = _answer(monkeypatch, _fixture())

    articles, notes = gdelt.fetch("M&M.NS", *_WINDOW)

    assert notes == []
    assert [article.title for article in articles] == [
        "M&M Q2 profit rises 12% on strong SUV demand",
        "Mahindra arm plans EV plant in Pune",
    ]
    first, second = articles
    assert first.publisher == "economictimes.indiatimes.com"
    assert first.link == (
        "https://economictimes.indiatimes.com/markets/stocks/news/synthetic-mahindra-q2.shtml"
    )
    assert first.published == datetime(2025, 10, 8, 7, 30, tzinfo=UTC)
    # A blank GDELT domain is labelled, not left as "Unknown" downstream.
    assert second.publisher == "GDELT"
    assert second.published == datetime(2025, 10, 6, 18, 30, tzinfo=UTC)

    kwargs = get_json.call_args.kwargs
    assert kwargs["bucket"] == "gdelt"
    assert kwargs["namespace"] == "india_news"
    assert kwargs["key"] == "gdelt:2025-10-06:2025-10-08"
    # The window ends inside the last week, so it is re-read daily.
    assert kwargs["ttl_seconds"] == 24 * 60 * 60
    assert kwargs["params"] == {
        "query": '"Mahindra & Mahindra"',
        "mode": "artlist",
        "format": "json",
        "maxrecords": 20,
        "sort": "datedesc",
        # Midnight and 23:59:59 of the IST days, written as the naive wall
        # clock GDELT's API takes.
        "startdatetime": "20251006000000",
        "enddatetime": "20251008235959",
    }


@pytest.mark.unit
def test_items_outside_the_ist_days_are_dropped(monkeypatch):
    _answer(monkeypatch, _fixture())

    articles, _ = gdelt.fetch("M&M.NS", *_WINDOW)

    titles = {article.title for article in articles}
    # 2025-10-08 19:00 UTC is 00:30 IST on 2025-10-09, the day after end_date.
    assert "Mahindra scooter recall widens" not in titles
    # 2025-10-05 12:00 UTC is before the window's first IST day.
    assert "Mahindra tractor sales fall in September" not in titles
    # An unreadable seendate is never placed inside a closed window.
    assert "Mahindra shares slip ahead of results" not in titles
    # A row with no title has nothing to report and is skipped.
    assert "" not in titles


@pytest.mark.unit
def test_one_story_syndicated_twice_is_kept_once(monkeypatch):
    _answer(monkeypatch, _fixture())

    articles, _ = gdelt.fetch("M&M.NS", *_WINDOW)

    deduped = [a for a in articles if "profit rises" in a.title.casefold()]
    assert [article.publisher for article in deduped] == ["economictimes.indiatimes.com"]


@pytest.mark.unit
def test_a_window_older_than_the_index_makes_no_request_and_says_why(monkeypatch):
    get_json = _answer(monkeypatch, _fixture())

    articles, notes = gdelt.fetch("M&M.NS", "2025-06-01", "2025-06-30")

    get_json.assert_not_called()
    assert articles == []
    assert len(notes) == 1
    assert "90" in notes[0]
    assert "not an absence of news" in notes[0]


@pytest.mark.unit
def test_a_window_straddling_the_cutoff_is_clamped_visibly(monkeypatch):
    get_json = _answer(monkeypatch, _fixture())

    articles, notes = gdelt.fetch("M&M.NS", "2025-06-01", "2025-07-31")

    params = get_json.call_args.kwargs["params"]
    assert params["startdatetime"] == "20250712000000"  # the frozen cutoff day
    assert params["enddatetime"] == "20250731235959"
    # The requested window is older than a week, so the fetch is cached for one.
    assert get_json.call_args.kwargs["ttl_seconds"] == 7 * 24 * 60 * 60
    assert len(notes) == 1
    assert "2025-07-12" in notes[0]
    assert "not an absence of news" in notes[0]
    # No fixture row falls in this window, and none is invented to fill it.
    assert articles == []


@pytest.mark.unit
def test_a_disabled_gdelt_makes_no_request_and_leaves_no_note(monkeypatch):
    get_json = _answer(monkeypatch, _fixture())
    set_config({"india": {"gdelt_enabled": False}})

    assert gdelt.fetch("M&M.NS", *_WINDOW) == ([], [])
    get_json.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("payload", [{}, {"articles": []}, []])
def test_an_empty_answer_is_no_rows_not_a_failure(monkeypatch, payload):
    _answer(monkeypatch, payload)

    assert gdelt.fetch("M&M.NS", *_WINDOW) == ([], [])


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    ["<html>rate limited</html>", 42, ["not", "an", "object"], {"articles": "nope"}],
)
def test_an_unreadable_payload_raises_vendor_unavailable(monkeypatch, payload):
    _answer(monkeypatch, payload)

    with pytest.raises(VendorUnavailableError):
        gdelt.fetch("M&M.NS", *_WINDOW)
