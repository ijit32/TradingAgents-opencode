"""Who to search for, and how: the one place a ticker becomes a news query.

A Google News search for ``M&M`` returns music hardware; the same search for
``"Mahindra & Mahindra"`` returns the company. Indian symbols are short,
punctuated and often ordinary words, so the query is built from the issuer name
NSE publishes rather than from the ticker alone, with the symbol kept as an
alternative term only when it is unambiguous.

The module is deliberately pure where it can be: name resolution is the single
call into ``nse.issuer``, and everything else — cleaning, ambiguity, the query
string, the dedupe key — is a function of its arguments that tests can pin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tradingagents.dataflows import markets
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.vendors.nse import issuer

# Google News' date operators take an inclusive day, so the window is expressed
# as ``after:start before:end + 1 day``: ``before:`` is exclusive.
_DATE_CLAUSE = "after:{start} before:{after}"

# Suffixes every Indian issuer appends to its registered name. They are dropped
# for searching because journalists do not write "Limited"; the remainder is
# what a headline actually contains.
_SUFFIXES = {"limited", "ltd"}

# Typographic quotes differ between the exchange master and a headline; fold
# them to the ASCII form before comparing or searching. A curly apostrophe in
# "D'Souza" must not split the name into two tokens.
_PUNCTUATION = re.compile(r"[\u2018\u2019\u201c\u201d]")


@dataclass(frozen=True)
class Article:
    """One news item, dated by when the publisher says it appeared."""

    title: str
    publisher: str
    link: str
    published: datetime | None  # aware UTC; None when the feed gives no date


def display_name(name: str) -> str:
    """An issuer's registered name as a headline would write it.

    ``"Mahindra & Mahindra Limited"`` reads ``"Mahindra & Mahindra"``;
    ``"Larsen & Toubro Ltd."`` reads ``"Larsen & Toubro"``. Only trailing
    corporate suffixes and punctuation are removed — a leading or embedded
    "Limited" is part of a name and stays.
    """
    text = _PUNCTUATION.sub("'", name or "").strip()
    words = text.split()
    while words and words[-1].casefold().strip(".,'\"") in _SUFFIXES:
        words.pop()
    return " ".join(words).strip(" ,;:-")


def title_key(title: str) -> str:
    """A comparison form for dedupe across feeds.

    Google News appends ``" - Publisher"`` and GDELT does not; punctuation,
    spacing and ``&`` versus ``and`` also differ between syndications. Folding
    all of that means one story syndicated to three sites is kept once.
    """
    text = title.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def strip_publisher(title: str, publisher: str) -> str:
    """Remove Google News' ``" - Publisher"`` suffix when it matches ``source``.

    The RSS ``<source>`` element already names the publisher; leaving the suffix
    in the title repeats it and makes the dedupe key differ from GDELT's copy of
    the same headline.
    """
    suffix = f" - {publisher.strip()}"
    if publisher.strip() and title.casefold().endswith(suffix.casefold()):
        return title[: -len(suffix)].rstrip()
    return title.strip()


def name_for(ticker: str) -> str:
    """The issuer's searchable name, or ``""`` when NSE cannot supply one.

    A name is an improvement, not a prerequisite: the symbol alone still finds
    coverage, and the equity master being throttled must not take the news
    section down with it.
    """
    if not markets.is_india(ticker):
        return ""
    try:
        resolved = issuer.issuer_for(ticker)
    except VendorError:
        return ""
    return display_name(resolved.name) if resolved else ""


def is_ambiguous(symbol: str) -> bool:
    """Whether the bare NSE/BSE symbol is too weak to search alongside the name.

    ``symbol`` is what ``markets.india_ticker`` returned — no exchange suffix —
    because that is the form the query already built from. ``M&M`` is the case
    that forced this: used alone it matches music and audio equipment, so the
    query drops it and relies on the quoted company name. Short all-letter
    symbols are left to the name-plus-symbol query, where the name disambiguates
    them (``ITC``, ``TCS``, ``GAIL``).
    """
    return "&" in symbol or len(symbol.strip()) <= 2


def _date_clause(start_date: str, end_date: str) -> str:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    after = end + timedelta(days=1)
    return _DATE_CLAUSE.format(start=start.isoformat(), after=after.isoformat())


def google_queries(ticker: str, start_date: str, end_date: str) -> list[str]:
    """Queries to try in order, most specific first.

    The first query pairs the quoted company name with the symbol and the two
    exchange names. The second drops the symbol terms: it is tried only when the
    first returned nothing, which is how a symbol that Google reads as an
    ordinary word (``M&M`` is caught earlier, but ``ITC`` is not) still finds
    the company.
    """
    bare = markets.india_ticker(ticker)
    dates = _date_clause(start_date, end_date)
    name = name_for(ticker)
    if not name:
        return [f"{bare} (NSE OR BSE) {dates}".strip()]
    if is_ambiguous(bare):
        return [f'"{name}" {dates}']
    return [
        f'"{name}" ({bare} OR NSE OR BSE) {dates}',
        f'"{name}" {dates}',
    ]


def gdelt_query(ticker: str) -> str:
    """The phrase GDELT searches for; its syntax has no symbol alternative."""
    name = name_for(ticker)
    if name:
        return f'"{name}"'
    return f"{markets.india_ticker(ticker)} NSE"
