"""Google News RSS: the Indian coverage Yahoo's sample leaves out.

Yahoo tags almost no Indian article with an NSE symbol, so a symbol-based news
feed returns nothing for most of the market even on days a company was in every
paper. Google News indexes the Indian publishers themselves and accepts a
date-ranged query, so the search names the issuer (``"Mahindra & Mahindra"``)
rather than the ticker, and the dated result is already the window the run may
read. That is why this vendor is searched by name: the query, not the feed, is
what made the old path empty.

Every link is the ``news.google.com/rss/articles/...`` redirect Google wraps
around the publisher's URL. Resolving it would cost one extra request per
article, and the redirect is stable for the reader, so the link is reported as
returned and the reader's browser follows it. Reporting the real URL is not
worth turning twenty articles into twenty more requests a run.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window_ist
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.ist import today_ist
from tradingagents.dataflows.vendors.india_news import query
from tradingagents.dataflows.vendors.nse import client

_RSS_URL = "https://news.google.com/rss/search"

# Google News localises a search by edition, not by host: the language, the
# country and the edition id together choose which papers are searched. Indian
# English is the edition an NSE issuer is covered in; leaving the default would
# search the US edition for the same query.
_EDITION = {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}

# A window that reaches the last week may still gain articles (Google indexes a
# story a day or two late), so it is re-read daily. A window that ended more
# than a week ago cannot change, so one fetch serves every later run.
_FRESH_TTL_SECONDS = 24 * 60 * 60
_ARCHIVE_TTL_SECONDS = 7 * 24 * 60 * 60
_FRESH_WINDOW_DAYS = 7

# Google often omits ``<source>`` on syndicated items; the title suffix then
# carries no publisher to strip, but the article still needs a label.
_FALLBACK_PUBLISHER = "Google News"

# Undated items sort last so the cap keeps the freshest dated stories.
_UNDATED = datetime.min.replace(tzinfo=UTC)


def _ttl_seconds(end_date: str) -> int:
    """How long a fetched window may be reused."""
    fresh = date.fromisoformat(end_date) >= today_ist() - timedelta(days=_FRESH_WINDOW_DAYS)
    return _FRESH_TTL_SECONDS if fresh else _ARCHIVE_TTL_SECONDS


def _feed(query_text: str, ttl_seconds: int) -> bytes:
    """The cached RSS document for one query.

    ``get_bytes`` takes no query parameters, so they are encoded into the URL
    here. The cache key carries the full query as well: the transport hashes the
    key, not the URL, and two searches for different issuers must not share an
    entry.
    """
    url = f"{_RSS_URL}?{urlencode({'q': query_text, **_EDITION})}"
    return client.get_bytes(
        url,
        bucket="google_news",
        ttl_seconds=ttl_seconds,
        namespace="india_news",
        key=f"google:{query_text}",
    )


def _local(tag: str) -> str:
    """An element name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    """The first child called ``name``, matched on the local name.

    Feeds occasionally carry a default namespace, and a namespaced document is
    still the same RSS, so matching on the qualified tag would reject it.
    """
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element: ET.Element, name: str) -> str:
    """The stripped text of a child, or ``""`` when it is absent or empty."""
    found = _child(element, name)
    return (found.text or "").strip() if found is not None else ""


def _published(raw: str) -> datetime | None:
    """Google News' RFC 2822 ``pubDate`` as aware UTC, or None when unreadable.

    A value with no zone is UTC by the format's own rule (RFC 2822 treats
    ``-0000`` and a missing zone alike for this purpose). An unreadable date
    stays None rather than becoming a guess: ``in_window_ist`` keeps an undated
    item only when the window reaches the present.
    """
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _items(data: bytes) -> list[ET.Element]:
    """The ``rss > channel > item`` elements, or a typed failure.

    A block page is what a throttled or refused Google request returns: HTML,
    often not even well-formed, so it fails to parse. Either way that is "the
    document could not be read", never "no news in this window".
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise VendorUnavailableError(
            "Google News returned a document that is not RSS (an HTML block page?)"
        ) from exc
    channel = _child(root, "channel") if _local(root.tag) == "rss" else None
    if channel is None:
        raise VendorUnavailableError(
            "Google News returned a document with no rss/channel (an HTML block page?)"
        )
    return [child for child in channel if _local(child.tag) == "item"]


def _articles(data: bytes, start_date: str, end_date: str) -> list[query.Article]:
    """One query's items, trimmed to the window, deduplicated and labelled.

    Deduplication is by ``title_key`` because Google syndicates one story to
    several publishers, and the key folds the publisher suffix away. An item is
    marked seen only once it survives the window, so an out-of-window copy
    cannot shadow the in-window one.
    """
    kept: list[query.Article] = []
    seen: set[str] = set()
    for item in _items(data):
        publisher = _text(item, "source") or _FALLBACK_PUBLISHER
        title = query.strip_publisher(_text(item, "title"), publisher)
        key = query.title_key(title)
        if not key or key in seen:
            continue
        published = _published(_text(item, "pubDate"))
        if not in_window_ist(published, start_date, end_date):
            continue
        seen.add(key)
        kept.append(
            query.Article(
                title=title,
                publisher=publisher,
                link=_text(item, "link"),
                published=published,
            )
        )
    return kept


def _capped(articles: list[query.Article]) -> list[query.Article]:
    """Newest first, at most ``news_article_limit`` of them."""
    limit = int(get_config().get("news_article_limit") or 20)
    ordered = sorted(articles, key=lambda article: article.published or _UNDATED, reverse=True)
    return ordered[: max(limit, 0)]


def fetch(ticker: str, start_date: str, end_date: str) -> tuple[list[query.Article], list[str]]:
    """Google News articles about ``ticker`` published in ``[start, end]``.

    The queries run most specific first; the symbol-plus-name query usually
    answers, and the company-name-only query is tried only when it returned no
    dated article, so a symbol Google reads as an ordinary word still finds the
    company. A request or XML that cannot be read raises
    ``VendorUnavailableError`` — the caller reports a source that did not answer
    rather than an absence of news — while a readable feed with nothing in the
    window returns an empty list.
    """
    notes: list[str] = []
    if not query.name_for(ticker):
        notes.append(
            f"Google News: the NSE issuer master resolved no company name for {ticker}, "
            "so the search used the symbol only."
        )

    candidates = query.google_queries(ticker, start_date, end_date)
    ttl = _ttl_seconds(end_date)
    articles: list[query.Article] = []
    for index, text in enumerate(candidates):
        articles = _articles(_feed(text, ttl), start_date, end_date)
        if articles:
            break
        if index + 1 < len(candidates):
            notes.append(
                f"Google News: the symbol query returned no article dated in {start_date}.."
                f"{end_date}; the company-name-only query was used instead."
            )
    return _capped(articles), notes
