"""GDELT DOC 2.0: the second news pass, and the one with a clock on it.

Google News is the primary search — it is keyed by the issuer name the caller
built and its RSS is cheap to ask. GDELT is the reinforcement: an independent
index that often carries a story Google News has not syndicated under the same
phrasing. It is deliberately second, because it is the more fragile of the two:

* its DOC 2.0 API searches a rolling window of roughly the last three months,
  so a window older than that cannot be answered at all — and an empty answer
  there would be a lie about the issuer rather than an absence of news;
* it throttles aggressively (HTTP 429), which is why every request goes through
  the shared NSE client: its ``gdelt`` bucket spaces requests by
  ``india.min_interval_seconds`` (6 s by default), caches the payload on disk
  and retries a 429 once after ``Retry-After``.

The window is an IST calendar window, as everywhere else in the India pack, but
GDELT's ``startdatetime``/``enddatetime`` take a naive ``YYYYMMDDHHMMSS`` string
with no offset. The IST wall-clock day is therefore written into that string
as-is. GDELT reads it as UTC, which places the requested start 5 h 30 min before
true IST midnight and the requested end the same amount after IST 23:59:59 — an
over-fetch of at most half a day at each edge, never an under-fetch. What is
returned is then trimmed to the exact IST days by ``in_window_ist``, so the
generous request cannot leak a story outside the run's window.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window_ist
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.ist import today_ist
from tradingagents.dataflows.vendors.india_news import query
from tradingagents.dataflows.vendors.india_news.query import Article, title_key
from tradingagents.dataflows.vendors.nse import client

_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# DOC 2.0 keeps a rolling three-month index. Ninety days is the conservative
# read of "three months": a window that ended before ``today - 90`` is beyond
# reach even when the API's own horizon is generous by a day or two.
_LOOKBACK_DAYS = 90

# A window that reaches the last week may still gain rows, so it is re-read
# daily; an older window is fixed and one fetch serves it for a week. The same
# split as the NSE filings adapter, for the same reason.
_FRESH_TTL_SECONDS = 24 * 60 * 60
_ARCHIVE_TTL_SECONDS = 7 * 24 * 60 * 60
_FRESH_WINDOW_DAYS = 7

# GDELT's own default page size; used when the configuration names none.
_DEFAULT_LIMIT = 20

# What a row without a publisher is labelled. GDELT's ``domain`` is the only
# publisher field it gives, and a blank one is not a reason to drop the story.
_FALLBACK_PUBLISHER = "GDELT"

# ``seendate`` is when GDELT indexed the article, written in UTC: no offset is
# carried in the string, so the suffix is the offset.
_SEENDATE_FORMAT = "%Y%m%dT%H%M%SZ"


def fetch(ticker: str, start_date: str, end_date: str) -> tuple[list[Article], list[str]]:
    """Articles GDELT indexed in ``[start_date, end_date]``, plus coverage notes.

    ``start_date`` and ``end_date`` are IST calendar days. The returned articles
    are already trimmed to that window, deduplicated by ``title_key`` and capped
    at ``news_article_limit``; the notes say what the window could not observe,
    so the caller can print a gap instead of an absence.

    A request that could not be made or read raises ``VendorUnavailableError``
    for the caller to report. A window older than GDELT's three-month reach is
    not a failure: no request is made and one note explains the missing section.
    """
    india = get_config().get("india") or {}
    if not india.get("gdelt_enabled", True):
        return [], []

    today = today_ist()
    cutoff = today - timedelta(days=_LOOKBACK_DAYS)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    # The whole window is older than the index: asking would return nothing and
    # say "no news", which is exactly the misreading the note prevents.
    if end < cutoff:
        return [], [_beyond_reach(start_date, end_date, cutoff)]

    notes: list[str] = []
    # A window that only partly predates the index is still worth searching. The
    # start moves to the oldest reachable day and the trim is announced, so the
    # earlier days are a named gap rather than a silently truncated window.
    clamped_start = max(start, cutoff)
    if clamped_start > start:
        notes.append(_clamped(start_date, end_date, cutoff))

    payload = client.get_json(
        _ENDPOINT,
        params={
            "query": query.gdelt_query(ticker),
            "mode": "artlist",
            "format": "json",
            "maxrecords": _limit(),
            "sort": "datedesc",
            # The IST day written as the naive wall clock GDELT expects; see the
            # module docstring for why this over-fetches by half a day and is
            # then trimmed by ``in_window_ist``.
            "startdatetime": _stamp(clamped_start, "000000"),
            "enddatetime": _stamp(end, "235959"),
        },
        bucket="gdelt",
        ttl_seconds=_ttl_seconds(end_date, today),
        namespace="india_news",
        key=f"gdelt:{start_date}:{end_date}",
    )
    return _articles(payload, start_date, end_date), notes


def _limit() -> int:
    """The configured article cap, falling back to GDELT's default page size."""
    limit = get_config().get("news_article_limit")
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return value if value > 0 else _DEFAULT_LIMIT


def _stamp(day: date, clock: str) -> str:
    """``YYYYMMDDHHMMSS`` for ``day``, with ``clock`` as ``HHMMSS``."""
    return f"{day:%Y%m%d}{clock}"


def _ttl_seconds(end_date: str, today: date) -> int:
    """How long a fetched window may be reused: daily while fresh, else weekly."""
    fresh = date.fromisoformat(end_date) >= today - timedelta(days=_FRESH_WINDOW_DAYS)
    return _FRESH_TTL_SECONDS if fresh else _ARCHIVE_TTL_SECONDS


def _rows(payload: dict | list) -> list[dict]:
    """The article rows from a GDELT payload.

    An empty object or array is a legitimate "nothing indexed in this window"
    and stays empty. A missing ``articles`` key is the same answer: GDELT omits
    it when it has no rows. Any other shape — a JSON scalar, a populated array
    where an object was documented, ``articles`` that is not a list — is a
    response that cannot be read, and is raised rather than read as no news.
    """
    if isinstance(payload, list):
        if not payload:
            return []
        raise VendorUnavailableError("GDELT returned a JSON array instead of an article object")
    if not isinstance(payload, dict):
        raise VendorUnavailableError(
            f"GDELT returned {type(payload).__name__} instead of a JSON object"
        )
    if not payload:
        return []
    rows = payload.get("articles")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise VendorUnavailableError("GDELT returned a payload with no readable 'articles' list")
    return [row for row in rows if isinstance(row, dict)]


def _published(value: object) -> datetime | None:
    """A GDELT ``seendate`` as aware UTC, or None when it cannot be parsed.

    An unreadable stamp is not invented: the row keeps its place only if the
    window reaches the present, which is what ``in_window_ist`` does with an
    undated item.
    """
    try:
        stamp = datetime.strptime(str(value), _SEENDATE_FORMAT)
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=UTC)


def _article(row: dict) -> Article | None:
    """One GDELT row as an ``Article``, or None when it carries no title.

    GDELT gives the publisher as ``domain`` and nothing else, so a blank domain
    is labelled ``GDELT`` rather than left for the formatter to call "Unknown".
    """
    title = " ".join(str(row.get("title") or "").split())
    if not title:
        return None
    return Article(
        title=title,
        publisher=" ".join(str(row.get("domain") or "").split()) or _FALLBACK_PUBLISHER,
        link=str(row.get("url") or "").strip(),
        published=_published(row.get("seendate")),
    )


def _articles(payload: dict | list, start_date: str, end_date: str) -> list[Article]:
    """The payload's rows, trimmed to the original window and deduplicated.

    GDELT is asked for a slightly wider span than the window (see the module
    docstring) and syndicates one story across several domains, so both filters
    run here: the IST day decides membership, ``title_key`` decides whether the
    story is new. Response order — newest first, as ``sort=datedesc`` asked —
    is kept, so the first copy of a story is its most recent.
    """
    limit = _limit()
    kept: list[Article] = []
    seen: set[str] = set()
    for row in _rows(payload):
        article = _article(row)
        if article is None:
            continue
        if not in_window_ist(article.published, start_date, end_date):
            continue
        key = title_key(article.title)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(article)
        if len(kept) >= limit:
            break
    return kept


def _beyond_reach(start_date: str, end_date: str, cutoff: date) -> str:
    """The note for a window GDELT cannot see at all."""
    return (
        f"<india_news GDELT unavailable for {start_date}..{end_date}: GDELT's DOC "
        f"2.0 index covers only the last {_LOOKBACK_DAYS} days (from "
        f"{cutoff.isoformat()}), so it cannot reach this window and the missing "
        "GDELT section is not an absence of news>"
    )


def _clamped(start_date: str, end_date: str, cutoff: date) -> str:
    """The note for a window whose first days predate the index."""
    return (
        f"<india_news GDELT coverage for {start_date}..{end_date} begins "
        f"{cutoff.isoformat()}: the DOC 2.0 index reaches back only "
        f"{_LOOKBACK_DAYS} days, so the earlier part of this window was not "
        "searched and is not an absence of news>"
    )
