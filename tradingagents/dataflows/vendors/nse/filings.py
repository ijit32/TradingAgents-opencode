"""What an NSE/BSE issuer actually filed, and when India could read it.

NSE's corporate-announcements feed and its integrated-filing index answer two
different questions for the same company. The announcements feed is the
universal one: every issuer, every subject, back to 2010, each row carrying the
instant it was published (``an_dt``, IST). The integrated index is narrower — it
starts in April 2025 — but it is the route to the structured (XBRL) results
filing. Coverage measured in Phase 2a was 25 % of sampled quarters, so the
announcement is often the only record: a results filing with ``hasXbrl: false``
is named here with its date and PDF, and no figure is invented for it.

Both feeds are keyed by NSE symbol. A ``.BO`` quote carries a BSE scrip code,
which NSE does not understand, so a BSE listing resolves through the configured
alias map (``issuer.nse_symbol_for_bse``) or is refused. BSE's own API is
blocked from this network, so a BSE-only scrip has no reachable filing feed and
is told so rather than answered with another company's rows.

Every timestamp is converted to aware UTC at parse time and rendered in IST at
display time. A filing published at 20:05 IST belongs to that IST day: a run
dated the same day sees it, a run dated the day before does not (plan §3.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tradingagents.dataflows import markets
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.ist import ist_date, parse_ist, to_ist, today_ist
from tradingagents.dataflows.vendors.nse import client, issuer

_ANNOUNCEMENTS_PATH = "/api/corporate-announcements"
_INTEGRATED_PATH = "/api/integrated-filing-results"

_ANNOUNCEMENTS_NAMESPACE = "announcements"
_INTEGRATED_NAMESPACE = "integrated_filings"

# A window that reaches the last week may still gain rows (a late filing, a
# revision), so it is re-read daily. A window that ended more than a week ago
# cannot change — a filing is immutable once published — so one fetch serves
# every later run for a week.
_FRESH_TTL_SECONDS = 24 * 60 * 60
_ARCHIVE_TTL_SECONDS = 7 * 24 * 60 * 60
_FRESH_WINDOW_DAYS = 7

# SEBI's quarterly results deadline is 45 days. A 7-day default window would
# hide the last results filing, which is the one the block exists to show, so
# the fallback is generous. A deployment can still narrow it through
# ``india.announcements_lookback_days``.
_DEFAULT_LOOKBACK_DAYS = 90

# The integrated index starts in April 2025, so three years is enough to find
# every row that exists for a run dated today while keeping the window bounded
# for a run dated in the past.
_FINANCIALS_LOOKBACK_DAYS = 3 * 365

# The per-symbol filter returns a handful of rows, but the endpoint paginates
# and the default page size is 20. Five pages is 10 000 rows: far more than one
# issuer can file, and a stop so a feed that ignores the symbol filter cannot
# walk forever.
_MAX_PAGES = 5


@dataclass(frozen=True)
class Filing:
    """One exchange announcement, dated by when it was published."""

    subject: str
    published: datetime  # aware UTC; display converts to IST
    url: str
    has_xbrl: bool
    symbol: str


def filing_symbol(ticker: str) -> str:
    """The NSE symbol whose filing feed answers for ``ticker``.

    The market check runs before any request: a US or other-market ticker has no
    business asking NSE anything, and the router relies on this raising so it
    can skip to the next vendor.

    A ``.BO`` listing resolves only through the explicit alias map. Yahoo
    exposes no ISIN for a BSE quote, and joining a BSE scrip to an NSE symbol by
    a guessed name or a shared code could attach another issuer's filings to the
    report — worse than reporting the gap.
    """
    if not markets.is_india(ticker):
        raise NoMarketDataError(ticker, ticker, "not an NSE/BSE listing")
    bare = markets.india_ticker(ticker)
    if markets.india_exchange(ticker) == "NSE":
        return bare
    resolved = issuer.nse_symbol_for_bse(bare)
    if resolved:
        return resolved
    raise NoMarketDataError(
        ticker,
        ticker,
        "BSE scrip with no NSE symbol configured; BSE-only scrips have no "
        "reachable filing feed, and NSE keeps no filing under a BSE scrip code",
    )


def _nse_date(iso_date: str) -> str:
    """``YYYY-MM-DD`` as the ``DD-MM-YYYY`` the NSE APIs take."""
    return date.fromisoformat(iso_date).strftime("%d-%m-%Y")


def _ttl_seconds(end_date: str) -> int:
    """How long a fetched window may be reused."""
    fresh = date.fromisoformat(end_date) >= today_ist() - timedelta(days=_FRESH_WINDOW_DAYS)
    return _FRESH_TTL_SECONDS if fresh else _ARCHIVE_TTL_SECONDS


def _rows(payload: dict | list, label: str, ticker: str, nse: str) -> list[dict]:
    """The row list from an NSE payload, or a typed failure.

    An empty list is an answer — "nothing was filed in this window" — and stays
    one. A payload that is not a row list (``{}`` is what NSE returns for a
    request it did not like) is a vendor shape problem, and must not be read as
    an empty window.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = payload["data"]
    else:
        raise NoMarketDataError(ticker, nse, f"NSE returned no usable {label} payload")
    return [row for row in rows if isinstance(row, dict)]


def _belongs_to(row: dict, nse: str) -> bool:
    """Whether ``row`` is this issuer's, when the feed states an issuer at all."""
    stated = str(row.get("symbol") or "").strip().upper()
    return not stated or stated == nse


def _ist_day(value: object) -> date | None:
    """The IST calendar day of an NSE timestamp, or None if it is unreadable."""
    try:
        return ist_date(str(value))
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> datetime | None:
    """An NSE timestamp as aware UTC, or None if it is unreadable."""
    try:
        return parse_ist(str(value))
    except (TypeError, ValueError):
        return None


def _truthy(value: object) -> bool:
    """A boolean from a feed that has been seen to send ``true`` and ``"true"``."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def announcements(symbol: str, start_date: str, end_date: str) -> list[Filing]:
    """Every announcement ``symbol`` published in ``[start_date, end_date]``.

    The dates are IST calendar days and bound the feed's own publication field,
    so an empty result is a real absence for that issuer, not a failed query.
    Rows whose timestamp cannot be parsed are dropped: an undated filing cannot
    be placed on the right side of a run's date.
    """
    nse = filing_symbol(symbol)
    payload = client.get_json(
        _ANNOUNCEMENTS_PATH,
        params={
            "index": "equities",
            "from_date": _nse_date(start_date),
            "to_date": _nse_date(end_date),
            "symbol": nse,
        },
        ttl_seconds=_ttl_seconds(end_date),
        namespace=_ANNOUNCEMENTS_NAMESPACE,
        key=f"{nse}:{start_date}:{end_date}",
    )

    filings: list[Filing] = []
    for row in _rows(payload, "announcements", symbol, nse):
        if not _belongs_to(row, nse):
            continue
        published = _timestamp(row.get("an_dt"))
        if published is None:
            continue
        filings.append(
            Filing(
                subject=str(row.get("desc") or "").strip() or "Unspecified filing",
                published=published,
                url=str(row.get("attchmntFile") or "").strip(),
                has_xbrl=_truthy(row.get("hasXbrl")),
                symbol=nse,
            )
        )
    return filings


def _lookback_days(configured: int | None) -> int:
    """The window length for ``recent_filings``: caller, then config, then 90."""
    if configured is not None:
        return int(configured)
    india = get_config().get("india") or {}
    value = india.get("announcements_lookback_days")
    return int(value) if value else _DEFAULT_LOOKBACK_DAYS


def recent_filings(
    symbol: str,
    as_of_date: str,
    limit: int = 3,
    lookback_days: int | None = None,
) -> list[Filing]:
    """The last ``limit`` filings published on or before ``as_of_date``.

    This is the latest filings overall, not only results: the block exists to
    show what the issuer told the exchange, and a results announcement
    ("Outcome of Board Meeting") is usually one row among several same-day ones
    (a dividend, an investor presentation, a press release). Filtering to
    results here would hide the rest and could pick the wrong row.
    """
    end = date.fromisoformat(as_of_date)
    start = end - timedelta(days=_lookback_days(lookback_days))
    published = announcements(symbol, start.isoformat(), end.isoformat())
    # NSE is asked for a bounded window, but the point-in-time rule is applied
    # here as well: a feed that ignored a parameter must not leak a later filing.
    kept = [filing for filing in published if to_ist(filing.published).date() <= end]
    kept.sort(key=lambda filing: filing.published, reverse=True)
    return kept[: max(limit, 0)]


# How issuers announce results. "Outcome of Board Meeting" is the common form;
# a board meeting may also be called for a dividend or a fund raise, so a hit is
# a candidate, not proof — the structured block is the proof.
_RESULTS_HINTS = ("financial result", "outcome of board meeting", "results")


def results_filings(
    symbol: str,
    as_of_date: str,
    limit: int = 1,
    lookback_days: int | None = None,
) -> list[Filing]:
    """The last results-related announcements on or before ``as_of_date``.

    The three newest filings can all be routine updates while the results sit a
    month back, which is the one filing the fundamentals block exists to show.
    This asks for it explicitly over the same window as ``recent_filings``, so
    the second call is served from the announcements cache rather than the wire.
    """
    end = date.fromisoformat(as_of_date)
    start = end - timedelta(days=_lookback_days(lookback_days))
    published = announcements(symbol, start.isoformat(), end.isoformat())
    kept = [
        filing
        for filing in published
        if to_ist(filing.published).date() <= end
        and any(hint in filing.subject.casefold() for hint in _RESULTS_HINTS)
    ]
    kept.sort(key=lambda filing: filing.published, reverse=True)
    return kept[: max(limit, 0)]


def _page_size() -> int:
    india = get_config().get("india") or {}
    return int(india.get("integrated_filings_page_size") or 2000)


def integrated_filings(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Raw integrated-filing rows for ``symbol`` in the window, newest first.

    The endpoint is paginated and unfiltered it carries thousands of rows a
    month, so the request names the symbol (NSE honours it) and the symbol is
    checked again client-side: an unhonoured filter would otherwise mix other
    issuers into the results index.
    """
    nse = filing_symbol(symbol)
    size = _page_size()
    rows: list[dict] = []
    page = 1
    while True:
        payload = client.get_json(
            _INTEGRATED_PATH,
            params={
                "index": "equities",
                "from_date": _nse_date(start_date),
                "to_date": _nse_date(end_date),
                "symbol": nse,
                "size": size,
                "page": page,
            },
            ttl_seconds=_ttl_seconds(end_date),
            namespace=_INTEGRATED_NAMESPACE,
            key=f"{nse}:{start_date}:{end_date}:{page}",
        )
        page_rows = _rows(payload, "integrated filings", symbol, nse)
        rows.extend(row for row in page_rows if _belongs_to(row, nse))
        total = int(payload.get("totalCount") or 0) if isinstance(payload, dict) else 0
        if len(page_rows) < size or len(rows) >= total or page >= _MAX_PAGES:
            break
        page += 1
    rows.sort(key=lambda row: _ist_day(row.get("broadcast_Date")) or date.min, reverse=True)
    return rows


def _revision_row(row: dict) -> int:
    """1 for a first filing, 0 for a correction of one.

    ``type_Sub`` reads ``Original`` or ``New`` on a first filing and ``Revised``
    on a correction. The first filing is preferred because its figures are the
    ones the market first traded on; the revision stays identifiable and is used
    only when no original is in the window.
    """
    return 0 if "revis" in str(row.get("type_Sub") or "").lower() else 1


def _consolidated_row(row: dict) -> int:
    return 1 if str(row.get("consolidated") or "").strip().lower() == "consolidated" else 0


def latest_financials(symbol: str, as_of_date: str) -> dict | None:
    """The newest structured results row public on or before ``as_of_date``.

    Selection is by period end first — a later quarter always wins — then by
    filing class: an original before a revision, consolidated before standalone.
    The row must also have been broadcast by the run's date: a quarter that ended
    on ``as_of_date`` is not public until the issuer files, weeks later.

    The XBRL instance is not parsed here; that is ``nse/xbrl.py``'s job. The raw
    row (``xbrl`` URL, ``qe_Date``, basis) is returned so the parser can be given
    exactly the filing this date is allowed to see.
    """
    end = date.fromisoformat(as_of_date)
    start = end - timedelta(days=_FINANCIALS_LOOKBACK_DAYS)
    candidates: list[tuple[date, int, int, date, dict]] = []
    for row in integrated_filings(symbol, start.isoformat(), end.isoformat()):
        if "financials" not in str(row.get("type") or "").lower():
            continue
        period_end = _ist_day(row.get("qe_Date"))
        broadcast = _ist_day(row.get("broadcast_Date"))
        if period_end is None or broadcast is None:
            continue
        if period_end > end or broadcast > end:
            continue
        candidates.append((period_end, _revision_row(row), _consolidated_row(row), broadcast, row))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[:4], reverse=True)
    return candidates[0][4]


def format_recent_filings(filings: list[Filing], ticker: str, as_of_date: str) -> str:
    """The "As filed with NSE" block for the fundamentals pack.

    One line per filing, newest first as given: IST timestamp, subject, link.
    A filing without a structured instance says so on its own line — the reader
    must not go looking for figures the filing does not carry.
    """
    heading = f"### As filed with NSE — {ticker.upper()}, on or before {as_of_date}"
    if not filings:
        return f"{heading}\n\nNo filing was found on or before {as_of_date}.\n"
    lines = [heading, ""]
    for filing in filings:
        stamp = to_ist(filing.published).strftime("%Y-%m-%d %H:%M IST")
        line = f"- {stamp} | {filing.subject} | {filing.url or 'no attachment link'}"
        if not filing.has_xbrl:
            line += " | no structured XBRL"
        lines.append(line)
    return "\n".join(lines) + "\n"
