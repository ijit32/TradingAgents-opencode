"""The Indian business dailies' own feeds: the latest stories, matched by name.

Economic Times, Moneycontrol and Business Standard each publish an RSS feed of
their most recent market stories — twenty to forty items, rolling off the end as
new ones arrive and never archived. They are live-only for that reason: asked
for a window that closed before today they could only answer with stories the
window never saw, or with the wrong day's stories re-dated, so a closed window is
answered with nothing at all. That silence is not a claim that no news exists;
the archive-capable sources (Google News, GDELT) speak for it.

These are general market wires, not company-tagged feeds: an item carries no
symbol, and ``<dc:creator>`` names the journalist or agency that filed it, not
the issuer. So relevance is decided by text — an item is kept only when its
title or description mentions the issuer's resolved name (``query.name_for``).
The name is used rather than the symbol because an Indian symbol is short and
often an ordinary word: ``ITC``, ``BEL`` and ``GAIL`` alone would sweep in
unrelated stories, while "Mahindra & Mahindra" would not. The bare symbol is a
fallback only when the equity master could not supply a name, and only for
symbols of three characters or more, where an uppercase word-boundary match is
specific enough to mean something.

The adapter is supplemental. One feed failing costs its own items and a note,
never the other two feeds and never an exception: a run that already has Google
News and GDELT should not lose them because a publisher changed its RSS layout.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from tradingagents.dataflows import markets
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window_ist
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.ist import today_ist
from tradingagents.dataflows.vendors.india_news import query
from tradingagents.dataflows.vendors.india_news.query import Article, title_key
from tradingagents.dataflows.vendors.nse import client

# The feeds, in the order they are read: the widest market wire first. The name
# is the attribution shown to the reader — not any ``dc:creator`` in the item.
_FEEDS = (
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
)

# An hour is short enough that a live feed still reads as today's and long enough
# that a run's three feeds are not re-fetched by every stage that touches news.
_TTL_SECONDS = 3600

_NAMESPACE = "india_news"
_BUCKET = "publishers"

# Two characters match inside ordinary words and abbreviations, so the symbol
# fallback starts at three.
_MIN_SYMBOL_LENGTH = 3

_HTML_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

# Undated items sort last; the caller shows them without inventing a time.
_UNDATED = datetime.min.replace(tzinfo=UTC)


def fetch(ticker: str, start_date: str, end_date: str) -> tuple[list[Article], list[str]]:
    """Publisher-feed articles that mention ``ticker`` in ``[start, end]``, and notes.

    Returns the in-window articles and one human-readable note per feed that
    could not be read. A window that has already closed returns nothing and
    says nothing: these feeds cannot observe it, so an empty answer would be a
    false absence of news rather than a gap. Never raises for a single feed.
    """
    if not _enabled():
        return [], []
    if not markets.is_india(ticker):
        return [], []
    if date.fromisoformat(end_date) < today_ist():
        return [], []

    name = query.name_for(ticker)
    articles: list[Article] = []
    notes: list[str] = []
    for feed_name, url in _FEEDS:
        try:
            payload = client.get_bytes(
                url,
                bucket=_BUCKET,
                ttl_seconds=_TTL_SECONDS,
                namespace=_NAMESPACE,
                key=f"publishers:{feed_name}:{start_date}:{end_date}",
            )
            items = _items(payload)
        except VendorError as exc:
            notes.append(_unread(feed_name, exc))
            continue
        except (ElementTree.ParseError, UnicodeDecodeError) as exc:
            notes.append(_unread(feed_name, exc))
            continue
        for item in items:
            article = _article(item, feed_name, ticker, name, start_date, end_date)
            if article is not None:
                articles.append(article)
    return _dedupe(articles), notes


def _enabled() -> bool:
    """Whether the run wants the publisher feeds; absent means the default, on."""
    india = get_config().get("india") or {}
    return bool(india.get("publisher_feeds_enabled", True))


def _items(payload: bytes) -> list[dict[str, str]]:
    """The channel's items as plain field maps.

    Raises ``ElementTree.ParseError`` for anything that is not an RSS document
    with a channel, which the caller turns into that feed's note.
    """
    root = ElementTree.fromstring(payload)
    channel = root.find("channel")
    if channel is None:
        raise ElementTree.ParseError("no <channel> element")
    return [
        {
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
            "pubDate": item.findtext("pubDate") or "",
            "description": item.findtext("description") or "",
        }
        for item in channel.findall("item")
    ]


def _article(
    item: dict[str, str],
    publisher: str,
    ticker: str,
    name: str,
    start_date: str,
    end_date: str,
) -> Article | None:
    """One item as an article, or None when it is irrelevant or out of window.

    The publisher is the feed's configured name by design: ``dc:creator`` often
    names a wire service whose copy the paper re-ran, so it would misattribute a
    story the reader can only find on that paper's site.
    """
    title = _plain(item["title"])
    if not title:
        return None
    description = _plain(item["description"])
    if not _mentions(ticker, name, f"{title} {description}"):
        return None
    published = _published(item["pubDate"])
    if not in_window_ist(published, start_date, end_date):
        return None
    return Article(
        title=title,
        publisher=publisher,
        link=item["link"].strip(),
        published=published,
    )


def _plain(text: str) -> str:
    """An element's display text with any HTML markup and entities removed.

    Some feeds put the whole story in ``<description>`` as escaped HTML; folding
    it to text is what lets a name that appears only there still match.
    """
    if not text:
        return ""
    return _SPACE.sub(" ", _HTML_TAG.sub(" ", html.unescape(text))).strip()


def _mentions(ticker: str, name: str, text: str) -> bool:
    """Whether ``text`` names the issuer, or — with no name — the bare symbol.

    The resolved name is compared as a normalised phrase, the same fold
    ``title_key`` applies to dedupe, so punctuation and "&" versus "and" do not
    hide a match. The symbol is only a fallback: it is matched case-sensitively
    with word boundaries, because a general market feed writes a listed symbol
    in capitals while lower-case prose rarely does.
    """
    if name:
        needle = title_key(name)
        return bool(needle) and f" {needle} " in f" {title_key(text)} "
    symbol = _symbol(ticker)
    if len(symbol) < _MIN_SYMBOL_LENGTH:
        return False
    return re.search(rf"\b{re.escape(symbol)}\b", text) is not None


def _symbol(ticker: str) -> str:
    """The bare exchange symbol, or ``""`` when the ticker is not Indian."""
    try:
        return markets.india_ticker(ticker)
    except ValueError:
        return ""


def _published(raw: str) -> datetime | None:
    """An item's ``pubDate`` as aware UTC; ``None`` when it is absent or unreadable.

    RSS dates are RFC 2822. A feed that omits the offset yields a naive value,
    which is taken as UTC — the same assumption made everywhere else here.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _dedupe(articles: list[Article]) -> list[Article]:
    """Newest first, one copy of each story, capped at the configured limit.

    The same story is syndicated across all three papers, so the first copy kept
    is the newest one — its title is what a later merge with Google News matches.
    """
    limit = int(get_config().get("news_article_limit") or 20)
    ordered = sorted(articles, key=lambda article: article.published or _UNDATED, reverse=True)
    seen: set[str] = set()
    kept: list[Article] = []
    for article in ordered:
        key = title_key(article.title)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(article)
        if len(kept) >= limit:
            break
    return kept


def _unread(feed_name: str, exc: Exception) -> str:
    """A per-feed gap that says the rest of the answer is still there."""
    return f"the {feed_name} feed could not be read ({exc}); the other feeds still answered"
