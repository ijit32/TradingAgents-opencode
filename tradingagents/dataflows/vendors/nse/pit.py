"""SEBI PIT insider disclosures, served at the date they were disclosed.

A PIT row carries two dates that are never the same and must never be confused:
``acqfromDt``/``acqtoDt`` is when the insider traded, and ``intimDt`` is when the
company told the exchange. The gap is usually two working days and can be a
week, so a run dated inside it must not see the row. Every selection here is
made on ``intimDt`` (plan §3.3); the trade date is printed beside it because the
gap is the fact a reader should weigh, not something to hide.

The feed is NSE-keyed, so a BSE listing self-gates: without an NSE symbol there
is no insider feed to ask (``filings.filing_symbol``).
"""

from __future__ import annotations

from datetime import date, timedelta

from tradingagents.dataflows.ist import ist_date, today_ist
from tradingagents.dataflows.vendors.nse import client
from tradingagents.dataflows.vendors.nse.filings import filing_symbol

_PATH = "/api/corporates-pit"
_NAMESPACE = "pit"

# A window that ends today or near it can gain a filing at any time, so a day
# of cache is the most a re-read can safely skip.
_TTL_SECONDS = 24 * 60 * 60

# Six months covers a quarter's worth of promoter activity with room to spare;
# SEBI requires intimation within two working days, so there is no backlog to
# walk further back for.
_LOOKBACK_DAYS = 180

# A busy promoter group files dozens of rows a quarter. The block is context for
# a research report, not a full register, so the newest fifty are shown and the
# header says how many were cut.
_MAX_ROWS = 50

_COLUMNS = (
    "Acquirer",
    "Category",
    "Transaction",
    "Securities",
    "Before",
    "After",
    "After %",
    "Trade window",
    "Intimated",
)


def _cell(value: object) -> str:
    """A table cell: trimmed, pipe-free, and ``-`` when the feed has nothing."""
    text = str(value if value is not None else "").strip()
    if not text:
        return "-"
    return text.replace("|", "/")


def _ist_day(value: object) -> date | None:
    try:
        return ist_date(str(value))
    except (TypeError, ValueError):
        return None


def _window(from_date: object, to_date: object) -> str:
    """The trade window as one date or an inclusive range."""
    first, last = _ist_day(from_date), _ist_day(to_date)
    if first is None and last is None:
        return "-"
    if first is None or last is None:
        # One end only: print the date the feed gave rather than a range with an
        # invented bound.
        return (first or last).isoformat()
    return first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}"


def _rows(payload: dict | list) -> list[dict]:
    """The row list from the PIT payload; anything else is an empty result."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def get_insider_transactions(ticker: str, curr_date: str) -> str:
    """Recent SEBI PIT disclosures for ``ticker``, as known on ``curr_date``."""
    nse = filing_symbol(ticker)
    as_of = curr_date or today_ist().isoformat()
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=_LOOKBACK_DAYS)

    payload = client.get_json(
        _PATH,
        params={
            "index": "equities",
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
            "symbol": nse,
        },
        ttl_seconds=_TTL_SECONDS,
        namespace=_NAMESPACE,
        key=f"{nse}:{start.isoformat()}:{end.isoformat()}",
    )

    disclosures: list[tuple[date, date, dict]] = []
    for row in _rows(payload):
        stated = str(row.get("symbol") or "").strip().upper()
        if stated and stated != nse:
            continue
        disclosed = _ist_day(row.get("intimDt"))
        # The gate is the intimation date, inclusive. A trade made before it is
        # not public yet, however early the trade itself happened.
        if disclosed is None or disclosed > end:
            continue
        traded = _ist_day(row.get("acqtoDt")) or disclosed
        disclosures.append((disclosed, traded, row))
    disclosures.sort(key=lambda item: (item[0], item[1]), reverse=True)

    header = [
        f"# Insider Transactions for {ticker.upper()} (SEBI PIT, NSE filings)",
        f"# Window {start.isoformat()} to {end.isoformat()}, by intimation date.",
        "# The trade date and the intimation date differ: a row became public only on",
        f"# the intimation date shown, so nothing here was known before it (cut-off {as_of}).",
    ]
    if len(disclosures) > _MAX_ROWS:
        header.append(f"# Showing the latest {_MAX_ROWS} of {len(disclosures)} disclosures.")
    if not disclosures:
        return "\n".join(
            header
            + [
                "",
                f"No PIT disclosures were filed in the window {start.isoformat()} "
                f"to {end.isoformat()}.",
            ]
        ) + "\n"

    lines = [
        *header,
        "",
        "| " + " | ".join(_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _COLUMNS) + " |",
    ]
    for disclosed, traded, row in disclosures[:_MAX_ROWS]:
        trade_window = _window(row.get("acqfromDt"), row.get("acqtoDt"))
        if trade_window == "-":
            # Both trade dates unreadable: the intimation date at least places
            # the row in time without inventing a trade date.
            trade_window = traded.isoformat()
        cells = (
            _cell(row.get("acqName")),
            _cell(row.get("personCategory")),
            _cell(row.get("tdpTransactionType")),
            _cell(row.get("secAcq")),
            _cell(row.get("befAcqSharesNo")),
            _cell(row.get("afterAcqSharesNo")),
            _cell(row.get("afterAcqSharesPer")),
            trade_window,
            disclosed.isoformat(),
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
