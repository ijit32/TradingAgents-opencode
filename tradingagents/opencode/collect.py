"""Pre-fetch the analysts' tool output into bounded data packs.

The plugin's generation path has no tool access, so the data an analyst would
have fetched itself is collected here and handed over as text. Point-in-time
guarantees are preserved by passing ``trade_date`` into the same tool functions
the graph uses: they clamp every requested date to the run's date.

Only what a pack needs is imported lazily, so ``init`` stays fast and does not
load langchain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from tradingagents.dataflows import markets
from tradingagents.dataflows.date_window import is_historical
from tradingagents.opencode.contract import AnalysisRequest, BadInput

# A calendar year of prices is far more than the pack can hold; 180 days keeps a
# usable trend window (enough for a 200-day average to be contextualized by the
# verified snapshot) without spending the budget on redundant rows.
PRICE_LOOKBACK_DAYS = 180
INDICATOR_LOOKBACK_DAYS = 60
SNAPSHOT_LOOKBACK_DAYS = 30
NEWS_WINDOW_DAYS = 7

TARGET_PACK_CHARS = 12_000
MAX_PACK_CHARS = 20_000

# Trim order for the market pack: the raw price table first (the snapshot
# summarizes it), then indicators. The snapshot is never trimmed.
_MARKET_TRIM = {"price": 0, "indicators": 1}

# News: broad feeds go before the company's own news; prediction markets next.
# Macro series are expensive to reconstruct, so they trim last rather than
# never: six unbroken series can exceed the cap on their own.
_NEWS_TRIM = {"global_news": 0, "company_news": 1, "prediction_markets": 2, "macro": 3}

# Fundamentals: statements trim before the profile, which anchors identity.
# The profile is trimmable last rather than never: `get_fundamentals` has no
# row bound, so it alone can exceed the hard cap, and an over-budget prompt is
# worse than a profile with its tail cut.
_FUNDAMENTALS_TRIM = {
    "insider": 0,
    "balance_sheet": 1,
    "cashflow": 2,
    "income_statement": 3,
    "fundamentals": 4,
}

MACRO_INDICATORS = (
    "cpi",
    "core_pce",
    "unemployment",
    "fed_funds_rate",
    "10y_treasury",
    "yield_curve",
)


@dataclass
class Section:
    key: str
    title: str
    body: str
    truncated_chars: int = 0
    # Lower trims first; None means never trim (the section is the source of truth).
    trim_rank: int | None = None

    def render(self) -> str:
        body = self.body
        if self.truncated_chars:
            body += f"\n\n[truncated: {self.truncated_chars} chars]"
        return f"## {self.title}\n\n{body}"


@dataclass
class DataPack:
    """The pack text plus what the plugin records about it."""

    analyst: str
    text: str
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    as_of: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def index_entry(self, path: str) -> dict[str, Any]:
        return {
            "analyst": self.analyst,
            "path": path,
            "chars": self.chars,
            "as_of": self.as_of,
            "sources": self.sources,
            "notes": self.notes,
            "truncated": self.truncated,
            "hash": self.digest,
        }


def _window(trade_date: str, days: int) -> tuple[str, str]:
    end = datetime.strptime(trade_date, "%Y-%m-%d").date()
    start = end - timedelta(days=days)
    return start.isoformat(), trade_date


def _attempt(
    source: str,
    call: Callable[[], str],
    *,
    sources: list[str],
    notes: list[str],
) -> str:
    """Run one fetch; a single vendor failure degrades the pack, never loses it."""
    try:
        text = call()
    except Exception as exc:  # noqa: BLE001 — one vendor failing must not lose the pack
        note = f"{source} unavailable ({type(exc).__name__}: {exc})"
        notes.append(note)
        return f"<{note}>"
    sources.append(source)
    return text


def _assemble(header: str, sections: list[Section]) -> tuple[str, bool]:
    """Join the sections, trimming to ``MAX_PACK_CHARS`` deterministically.

    Sections with ``trim_rank=None`` are never trimmed: they are the pack's
    source of truth, and a partial copy is worse than a shorter feed.
    """
    def total() -> int:
        return len(header) + sum(len(section.render()) + 2 for section in sections)

    truncated = False
    ranks = sorted({section.trim_rank for section in sections if section.trim_rank is not None})
    for rank in ranks:
        while total() > MAX_PACK_CHARS:
            section = next(
                (item for item in sections if item.trim_rank == rank and len(item.body) > 1_000),
                None,
            )
            if section is None:
                break
            over = total() - MAX_PACK_CHARS
            cut = min(len(section.body) - 1_000, max(over, 500))
            section.body = section.body[: len(section.body) - cut]
            section.truncated_chars += cut
            truncated = True
    return header + "\n\n" + "\n\n".join(section.render() for section in sections), truncated


def _finish(
    request: AnalysisRequest,
    analyst: str,
    header: str,
    sections: list[Section],
    sources: list[str],
    notes: list[str],
) -> DataPack:
    text, truncated = _assemble(header, sections)
    if truncated:
        notes.append(f"pack trimmed to the {MAX_PACK_CHARS}-char cap; see truncation markers")
    if not sources:
        raise BadInput(
            f"no {analyst} data could be collected; check the ticker and vendor keys"
        )
    return DataPack(
        analyst=analyst,
        text=text,
        sources=sources,
        notes=notes,
        truncated=truncated,
        as_of=request.trade_date,
    )


def collect_market(request: AnalysisRequest) -> DataPack:
    """The market analyst's pack: prices, the fixed indicator set, and the snapshot."""
    from tradingagents.agents.analysts.market_analyst import MARKET_INDICATORS
    from tradingagents.agents.tools import (
        get_indicators,
        get_stock_data,
        get_verified_market_snapshot,
    )

    ticker = request.ticker
    trade_date = request.trade_date
    start, end = _window(trade_date, PRICE_LOOKBACK_DAYS)
    notes: list[str] = []
    sources: list[str] = []

    price = _attempt(
        "get_stock_data",
        lambda: get_stock_data.func(
            symbol=ticker, start_date=start, end_date=end, trade_date=trade_date
        ),
        sources=sources,
        notes=notes,
    )

    indicator_blocks: list[str] = []
    for indicator in MARKET_INDICATORS:
        text = _attempt(
            f"get_indicators[{indicator}]",
            lambda name=indicator: get_indicators.func(
                symbol=ticker,
                indicator=name,
                curr_date=trade_date,
                look_back_days=INDICATOR_LOOKBACK_DAYS,
                trade_date=trade_date,
            ),
            sources=sources,
            notes=notes,
        )
        indicator_blocks.append(f"### {indicator}\n\n{text}")

    snapshot = _attempt(
        "get_verified_market_snapshot",
        lambda: get_verified_market_snapshot.func(
            symbol=ticker,
            curr_date=trade_date,
            look_back_days=SNAPSHOT_LOOKBACK_DAYS,
            trade_date=trade_date,
        ),
        sources=sources,
        notes=notes,
    )

    header = (
        f"# Market data pack — {ticker} as of {trade_date}\n\n"
        f"Collected once, before the analyst was called. Windows: prices {start} → {end}, "
        f"indicators {INDICATOR_LOOKBACK_DAYS} days, snapshot {SNAPSHOT_LOOKBACK_DAYS} rows. "
        "Every value is point-in-time: nothing later than the analysis date is included."
    )
    sections = [
        Section("price", f"Price history ({start} → {end})", price.strip(), trim_rank=_MARKET_TRIM["price"]),
        Section(
            "indicators",
            f"Indicators (look_back_days={INDICATOR_LOOKBACK_DAYS})",
            "\n\n".join(indicator_blocks).strip(),
            trim_rank=_MARKET_TRIM["indicators"],
        ),
        Section("snapshot", "Verified market snapshot", snapshot.strip(), trim_rank=None),
    ]
    return _finish(request, "market", header, sections, sources, notes)


def collect_sentiment(request: AnalysisRequest) -> DataPack:
    """The sentiment analyst's pack: news headlines, StockTwits, and Reddit."""
    from tradingagents.agents.post_screen import jev_screen
    from tradingagents.agents.tools import get_news
    from tradingagents.dataflows.vendors.reddit import fetch_reddit_posts, subreddits_for
    from tradingagents.dataflows.vendors.stocktwits import fetch_stocktwits_messages

    ticker = request.ticker
    trade_date = request.trade_date
    start, end = _window(trade_date, NEWS_WINDOW_DAYS)
    subreddits = subreddits_for(ticker)
    screen = jev_screen(ticker)
    notes: list[str] = []
    sources: list[str] = []

    news = _attempt(
        "get_news",
        lambda: get_news.func(ticker, start, end, trade_date=trade_date),
        sources=sources,
        notes=notes,
    )
    stocktwits = _attempt(
        "fetch_stocktwits_messages",
        lambda: fetch_stocktwits_messages(
            ticker, limit=30, start_date=start, end_date=end, screen=screen
        ),
        sources=sources,
        notes=notes,
    )
    reddit = _attempt(
        "fetch_reddit_posts",
        lambda: fetch_reddit_posts(
            ticker, subreddits, start_date=start, end_date=end, screen=screen
        ),
        sources=sources,
        notes=notes,
    )

    if is_historical(trade_date):
        note = (
            "social feeds (StockTwits, Reddit) serve recent items only and are not archived; "
            "this historical run's sentiment inputs are not point-in-time"
        )
        notes.append(note)

    header = (
        f"# Sentiment data pack — {ticker} as of {trade_date}\n\n"
        f"Window: {start} → {end}. The plugin fetched news headlines, the StockTwits cashtag "
        f"stream (limit 30), and posts from {', '.join(f'r/{sub}' for sub in subreddits)} before "
        "the analyst was called. Each section is that fetch's complete output; cite only what "
        "appears below."
    )
    if is_historical(trade_date):
        header += (
            "\n\n**Data-quality note:** the social feeds serve recent items and are not "
            "archived, so a historical run's StockTwits and Reddit blocks are not "
            "point-in-time. Weigh the news block accordingly and lower confidence when the "
            "social sections are missing or undated."
        )
    sections = [
        Section(
            "company_news",
            f"News headlines — Yahoo Finance ({start} → {end})",
            news.strip(),
            trim_rank=0,
        ),
        Section(
            "stocktwits",
            "StockTwits messages — user-labelled Bullish / Bearish tags",
            stocktwits.strip(),
            trim_rank=1,
        ),
        Section(
            "reddit",
            f"Reddit posts — {', '.join(f'r/{sub}' for sub in subreddits)}",
            reddit.strip(),
            trim_rank=2,
        ),
    ]
    return _finish(request, "social", header, sections, sources, notes)


def collect_news(request: AnalysisRequest) -> DataPack:
    """The news analyst's pack: company news, global news, macro series, and odds."""
    from tradingagents.agents.tools import (
        get_global_news,
        get_macro_indicators,
        get_news,
        get_prediction_markets,
    )

    ticker = request.ticker
    trade_date = request.trade_date
    start, end = _window(trade_date, NEWS_WINDOW_DAYS)
    year = trade_date[:4]
    notes: list[str] = []
    sources: list[str] = []

    company_news = _attempt(
        "get_news",
        lambda: get_news.func(ticker, start, end, trade_date=trade_date),
        sources=sources,
        notes=notes,
    )
    global_news = _attempt(
        "get_global_news",
        lambda: get_global_news.func(
            curr_date=trade_date, look_back_days=None, limit=None, trade_date=trade_date
        ),
        sources=sources,
        notes=notes,
    )

    macro_blocks = []
    for indicator in MACRO_INDICATORS:
        text = _attempt(
            f"get_macro_indicators[{indicator}]",
            lambda name=indicator: get_macro_indicators.func(
                indicator=name,
                curr_date=trade_date,
                look_back_days=None,
                trade_date=trade_date,
            ),
            sources=sources,
            notes=notes,
        )
        macro_blocks.append(f"### {indicator}\n\n{text}")

    odds_blocks = []
    for topic in ("Fed rate cut", f"recession {year}", ticker):
        text = _attempt(
            f"get_prediction_markets[{topic}]",
            lambda value=topic: get_prediction_markets.func(
                topic=value, limit=6, trade_date=trade_date
            ),
            sources=sources,
            notes=notes,
        )
        odds_blocks.append(f"### {topic}\n\n{text}")

    header = (
        f"# News data pack — {ticker} as of {trade_date}\n\n"
        f"Windows: company news {start} → {end}; global news and macro use the configured "
        "lookback ending on the analysis date. Collected once, before the analyst was called. "
        "Nothing later than the analysis date is included."
    )
    if markets.is_india(ticker):
        header += (
            "\n\nCompany news for an NSE/BSE listing is searched by issuer name across "
            "Google News, GDELT and the Indian publisher feeds, and the company-news "
            "section also carries the exchange's own filing record for the window. "
            "Headlines name their publisher, and a source that could not be reached "
            "is reported as a gap, not as an absence of news."
        )
    sections = [
        Section(
            "company_news",
            f"{ticker} news ({start} → {end})",
            company_news.strip(),
            trim_rank=_NEWS_TRIM["company_news"],
        ),
        Section(
            "global_news",
            "Global and macro news",
            global_news.strip(),
            trim_rank=_NEWS_TRIM["global_news"],
        ),
        Section(
            "macro",
            "FRED macro series",
            "\n\n".join(macro_blocks).strip(),
            trim_rank=_NEWS_TRIM["macro"],
        ),
        Section(
            "prediction_markets",
            "Prediction markets (market-implied probabilities)",
            "\n\n".join(odds_blocks).strip(),
            trim_rank=_NEWS_TRIM["prediction_markets"],
        ),
    ]
    return _finish(request, "news", header, sections, sources, notes)


def collect_fundamentals(request: AnalysisRequest) -> DataPack:
    """The fundamentals analyst's pack: profile, three statements, insider trades."""
    from tradingagents.agents.tools import (
        get_balance_sheet,
        get_cashflow,
        get_fundamentals,
        get_income_statement,
        get_insider_transactions,
    )

    ticker = request.ticker
    trade_date = request.trade_date
    notes: list[str] = []
    sources: list[str] = []

    profile = _attempt(
        "get_fundamentals",
        lambda: get_fundamentals.func(
            ticker=ticker, curr_date=trade_date, trade_date=trade_date
        ),
        sources=sources,
        notes=notes,
    )

    def statement(source: str, call: Callable[[], str]) -> str:
        return _attempt(source, call, sources=sources, notes=notes)

    balance = statement(
        "get_balance_sheet",
        lambda: get_balance_sheet.func(
            ticker=ticker, freq="quarterly", curr_date=trade_date, trade_date=trade_date
        ),
    )
    cashflow = statement(
        "get_cashflow",
        lambda: get_cashflow.func(
            ticker=ticker, freq="quarterly", curr_date=trade_date, trade_date=trade_date
        ),
    )
    income = statement(
        "get_income_statement",
        lambda: get_income_statement.func(
            ticker=ticker, freq="quarterly", curr_date=trade_date, trade_date=trade_date
        ),
    )
    insider = statement(
        "get_insider_transactions",
        lambda: get_insider_transactions.func(ticker=ticker, trade_date=trade_date),
    )

    if request.asset_type == "crypto":
        notes.append(
            "crypto instruments usually have no financial statements; unavailable sections are expected"
        )

    header = (
        f"# Fundamentals data pack — {ticker} as of {trade_date}\n\n"
        "Collected once, before the analyst was called: company profile and fundamentals, "
        "quarterly balance sheet, cash flow and income statement, and insider transactions. "
        "Every figure is as of the analysis date; statements whose period ends later are absent."
    )
    sections = [
        Section(
            "fundamentals",
            "Company profile and fundamentals",
            profile.strip(),
            trim_rank=_FUNDAMENTALS_TRIM["fundamentals"],
        ),
        Section(
            "income_statement",
            "Income statement (quarterly)",
            income.strip(),
            trim_rank=_FUNDAMENTALS_TRIM["income_statement"],
        ),
        Section(
            "balance_sheet",
            "Balance sheet (quarterly)",
            balance.strip(),
            trim_rank=_FUNDAMENTALS_TRIM["balance_sheet"],
        ),
        Section(
            "cashflow",
            "Cash flow statement (quarterly)",
            cashflow.strip(),
            trim_rank=_FUNDAMENTALS_TRIM["cashflow"],
        ),
        Section(
            "insider",
            "Insider transactions",
            insider.strip(),
            trim_rank=_FUNDAMENTALS_TRIM["insider"],
        ),
    ]
    return _finish(request, "fundamentals", header, sections, sources, notes)


COLLECTORS: dict[str, Callable[[AnalysisRequest], DataPack]] = {
    "market": collect_market,
    "social": collect_sentiment,
    "news": collect_news,
    "fundamentals": collect_fundamentals,
}
