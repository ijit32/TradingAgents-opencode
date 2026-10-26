"""The publisher RSS feeds: live-only gating, name matching, and per-feed failures.

Every test is offline. The transport is mocked at ``client.get_bytes`` — the one
seam this adapter shares with the rest of the NSE package — and ``today_ist`` is
patched in this module so "a window that reaches the present" is decidable
without depending on the day the suite runs. The fixtures are small hand-written
RSS documents under ``tests/fixtures/india/news``; their dates sit in
2025-04-20..2025-04-25, the window every test uses.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.india_news import publishers
from tradingagents.dataflows.vendors.india_news.query import title_key

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "india" / "news"
_GET_BYTES = "tradingagents.dataflows.vendors.nse.client.get_bytes"
_NAME_FOR = "tradingagents.dataflows.vendors.india_news.publishers.query.name_for"
_TODAY = "tradingagents.dataflows.vendors.india_news.publishers.today_ist"

_WINDOW = ("2025-04-20", "2025-04-25")
_PRESENT = date(2025, 4, 25)
_CLOSED = date(2025, 4, 26)

_FEED_FILES = {
    "Economic Times": "economic_times.xml",
    "Moneycontrol": "moneycontrol.xml",
    "Business Standard": "business_standard.xml",
}

_SYNDICATED = "Reliance Industries shares rise on refining margin outlook"
_DESCRIPTION_ONLY = "PSU refiner plans 30,000 crore capex over three years"


def _fixture(name: str) -> bytes:
    return (_FIXTURE_DIR / name).read_bytes()


def _feeds(overrides: dict[str, bytes] | None = None, failing: set[str] | None = None):
    """A ``get_bytes`` stub answering by feed name, so one feed can fail alone."""
    payloads = {name: _fixture(filename) for name, filename in _FEED_FILES.items()}
    payloads.update(overrides or {})
    broken = set(failing or ())

    def get_bytes(url: str, *, bucket: str, ttl_seconds: int, namespace: str, key: str):
        name = key.split(":")[1]
        if name in broken:
            raise VendorUnavailableError(f"NSE publishers request failed for {name}")
        return payloads[name]

    return get_bytes


def _fetch(ticker: str, *, today=_PRESENT, name="Reliance Industries", feeds=None):
    """Run ``fetch`` over the window with today, name resolution and transport stubbed."""
    with (
        mock.patch(_TODAY, return_value=today),
        mock.patch(_NAME_FOR, return_value=name),
        mock.patch(_GET_BYTES, side_effect=feeds or _feeds()),
    ):
        return publishers.fetch(ticker, *_WINDOW)


@pytest.mark.unit
def test_a_closed_window_makes_no_request_and_returns_nothing():
    with (
        mock.patch(_TODAY, return_value=_CLOSED),
        mock.patch(_GET_BYTES) as get_bytes,
    ):
        articles, notes = publishers.fetch("RELIANCE.NS", *_WINDOW)

    assert (articles, notes) == ([], [])
    # Not "no news": the feeds simply cannot see a window that has closed, so
    # there is nothing to ask them and nothing to apologise for.
    get_bytes.assert_not_called()


@pytest.mark.unit
def test_the_config_gate_disables_every_feed():
    set_config({"india": {"publisher_feeds_enabled": False}})

    with mock.patch(_TODAY, return_value=_PRESENT), mock.patch(_GET_BYTES) as get_bytes:
        assert publishers.fetch("RELIANCE.NS", *_WINDOW) == ([], [])

    get_bytes.assert_not_called()


@pytest.mark.unit
def test_each_feed_is_requested_with_its_own_cache_key():
    with (
        mock.patch(_TODAY, return_value=_PRESENT),
        mock.patch(_NAME_FOR, return_value="Reliance Industries"),
        mock.patch(_GET_BYTES, side_effect=_feeds()) as get_bytes,
    ):
        publishers.fetch("RELIANCE.NS", *_WINDOW)

    calls = get_bytes.call_args_list
    assert [call.args[0] for call in calls] == [url for _, url in publishers._FEEDS]
    assert [call.kwargs["key"] for call in calls] == [
        f"publishers:{name}:2025-04-20:2025-04-25" for name, _ in publishers._FEEDS
    ]
    for call in calls:
        assert call.kwargs["bucket"] == "publishers"
        assert call.kwargs["namespace"] == "india_news"
        assert call.kwargs["ttl_seconds"] == 3600


@pytest.mark.unit
def test_a_present_reaching_window_keeps_only_the_named_story():
    articles, notes = _fetch("RELIANCE.NS")

    assert notes == []
    by_title = {article.title: article for article in articles}
    assert set(by_title) == {_SYNDICATED, _DESCRIPTION_ONLY}
    # "Bloomberg Wire" is the dc:creator of the first item; the feed that
    # carried it is what the reader is told.
    assert by_title[_SYNDICATED].publisher == "Economic Times"
    assert by_title[_DESCRIPTION_ONLY].publisher == "Business Standard"
    assert all(article.published is not None for article in articles)
    assert all(article.link.startswith("https://") for article in articles)


@pytest.mark.unit
def test_html_in_the_description_is_stripped_and_still_matches():
    articles, _ = _fetch("RELIANCE.NS")

    # Its title never says "Reliance"; only the markup-laden description does.
    refiner = next(article for article in articles if article.title == _DESCRIPTION_ONLY)
    assert refiner.publisher == "Business Standard"
    assert not any("<" in article.title or ">" in article.title for article in articles)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "failing"),
    [
        ({}, {"Moneycontrol"}),
        ({"Moneycontrol": _fixture("malformed.xml")}, set()),
    ],
    ids=["request-failed", "malformed-xml"],
)
def test_a_failing_feed_becomes_a_note_and_the_others_survive(overrides, failing):
    articles, notes = _fetch(
        "RELIANCE.NS", feeds=_feeds(overrides=overrides, failing=failing)
    )

    assert len(notes) == 1
    assert "Moneycontrol feed could not be read" in notes[0]
    assert "the other feeds still answered" in notes[0]
    assert {article.publisher for article in articles} == {
        "Economic Times",
        "Business Standard",
    }


@pytest.mark.unit
def test_every_feed_failing_returns_notes_instead_of_raising():
    articles, notes = _fetch(
        "RELIANCE.NS", feeds=_feeds(failing=set(_FEED_FILES))
    )

    assert articles == []
    assert len(notes) == len(_FEED_FILES)
    assert all("could not be read" in note for note in notes)


@pytest.mark.unit
def test_a_missing_name_falls_back_to_the_bare_symbol():
    articles, notes = _fetch("TCS.NS", name="")

    assert notes == []
    assert [article.title for article in articles] == ["TCS wins large deal from European insurer"]
    assert articles[0].publisher == "Business Standard"


@pytest.mark.unit
def test_a_two_letter_symbol_does_not_use_the_regex_fallback():
    # "AB Capital" appears as a word in the Business Standard fixture; the
    # length guard is what keeps a two-letter symbol from matching it.
    with (
        mock.patch(_TODAY, return_value=_PRESENT),
        mock.patch(_NAME_FOR, return_value=""),
        mock.patch(_GET_BYTES, side_effect=_feeds()) as get_bytes,
    ):
        articles, notes = publishers.fetch("AB.NS", *_WINDOW)

    assert (articles, notes) == ([], [])
    assert get_bytes.call_count == len(_FEED_FILES)


@pytest.mark.unit
def test_a_story_syndicated_to_two_feeds_is_kept_once():
    articles, _ = _fetch("RELIANCE.NS")

    keys = [title_key(article.title) for article in articles]
    assert keys.count(title_key(_SYNDICATED)) == 1
    assert len(keys) == len(set(keys))
    # The newest copy is the one kept, and it is the paper's own filing.
    copy = next(article for article in articles if title_key(article.title) == title_key(_SYNDICATED))
    assert copy.publisher == "Economic Times"
