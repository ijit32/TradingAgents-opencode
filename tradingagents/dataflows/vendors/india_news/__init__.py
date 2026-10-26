"""India company news: Google News first, GDELT and publisher feeds second.

An NSE/BSE run used to read Yahoo's sample, which tags almost no Indian article
with the symbol and returns nothing for most of them. This package searches by
the issuer's name instead — Google News RSS with a date-ranged query — and adds
two narrower passes: GDELT, which reaches back three months, and the Indian
publisher feeds, which only carry the latest items and are therefore skipped for
a window that has already closed.

The exchange is the fourth source, and the best dated one: NSE announcements
carry the instant they were published, so the section after the articles is the
issuer's own filing record for the same window. A source that cannot answer is
named in the output rather than left as a silent gap; the caller must be able to
tell "no news" from "no answer".
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from tradingagents.dataflows import markets
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.ist import to_ist
from tradingagents.dataflows.vendors.india_news import gdelt, google, publishers
from tradingagents.dataflows.vendors.india_news.query import Article, title_key
from tradingagents.dataflows.vendors.nse import filings

_UNDATED = datetime.min.replace(tzinfo=UTC)

# A window of filings is evidence, not a feed: ten rows is enough to show what
# the issuer told the exchange around the run, and more would crowd the prompt.
_MAX_FILINGS = 10


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Articles about ``ticker`` and its exchange filings, for ``[start, end]``.

    Self-gates every non-India symbol so the router can keep one chain for all
    markets: this vendor raises, the next one serves. Dates are IST calendar
    days, which is how both Google News and NSE bound a window.
    """
    if not markets.is_india(ticker):
        raise NoMarketDataError(ticker, ticker, "not an NSE/BSE listing")

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError(f"news window ends before it starts ({start_date}..{end_date})")

    notices: list[str] = []
    articles: list[Article] = []
    answered = 0

    for source, fetch in (
        ("Google News", google.fetch),
        ("GDELT", gdelt.fetch),
        ("publisher feeds", publishers.fetch),
    ):
        try:
            found, source_notes = fetch(ticker, start_date, end_date)
        except VendorError as exc:
            notices.append(_unavailable(source, start_date, end_date, exc))
            continue
        answered += 1
        articles.extend(found)
        notices.extend(source_notes)

    merged = _dedupe(articles)
    blocks = [f"## {ticker} news, {start_date} to {end_date}", ""]
    if merged:
        blocks.extend(_format_articles(merged))
    elif answered:
        blocks.append(
            f"No news article was found for {ticker} between {start_date} and "
            f"{end_date} by the sources that answered. That is an absence from "
            "those sources, not proof the company was not discussed elsewhere."
        )
    else:
        blocks.append(
            f"No news source answered for {ticker} between {start_date} and "
            f"{end_date}; see the notice below rather than reading this as an "
            "absence of news."
        )

    blocks.append("")
    blocks.append(_filings_block(ticker, start_date, end_date, notices))
    if notices:
        blocks.append("")
        blocks.append("\n".join(notices))
    return "\n".join(blocks).strip() + "\n"


def _dedupe(articles: list[Article]) -> list[Article]:
    """Newest first, one copy of each story, capped at the configured limit.

    Google News appends the publisher to the title and GDELT does not, so the
    key folds punctuation and word variants; the first copy kept is the one with
    the most recent timestamp after sorting.
    """
    limit = int(get_config().get("news_article_limit") or 20)
    ordered = sorted(articles, key=lambda item: item.published or _UNDATED, reverse=True)
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


def _format_articles(articles: list[Article]) -> list[str]:
    lines: list[str] = []
    for article in articles:
        publisher = article.publisher or "Unknown"
        lines.append(f"### {article.title} (source: {publisher})")
        if article.published is not None:
            stamp = to_ist(article.published).strftime("%Y-%m-%d %H:%M IST")
            lines.append(f"Published: {stamp}")
        if article.link:
            lines.append(f"Link: {article.link}")
        lines.append("")
    return lines


def _filings_block(
    ticker: str, start_date: str, end_date: str, notices: list[str]
) -> str:
    """The issuer's own record for the window, or a named gap.

    Filings are the only source here that states when it was published, so they
    run after the articles even when no article was found. A BSE-only scrip has
    no NSE filing feed; that is a gap in the source, not an empty window, and is
    said so on its own line.
    """
    heading = f"## Exchange filings (NSE), {start_date} to {end_date}"
    try:
        rows = filings.announcements(ticker, start_date, end_date)
    except VendorError as exc:
        notices.append(_unavailable("Exchange filings (NSE)", start_date, end_date, exc))
        return f"{heading}\n\nNo filing feed answered for this window."
    rows.sort(key=lambda filing: filing.published, reverse=True)
    if not rows:
        return f"{heading}\n\nNo exchange filing was published in this window."
    lines = [heading, ""]
    for filing in rows[:_MAX_FILINGS]:
        stamp = to_ist(filing.published).strftime("%Y-%m-%d %H:%M IST")
        lines.append(f"- {stamp} | {filing.subject} | {filing.url or 'no attachment link'}")
    return "\n".join(lines)


def _unavailable(source: str, start_date: str, end_date: str, exc: Exception) -> str:
    """A gap with the same shape as ``coverage_gap``: unavailable, not absent."""
    return (
        f"<india_news unavailable for {start_date}..{end_date}: {source} could not "
        f"be reached ({exc}), so this is not an absence of news>"
    )
