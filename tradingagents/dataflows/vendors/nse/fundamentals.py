"""What an Indian issuer filed, appended to the profile the reader already knows.

An NSE/BSE run used to get either a withheld Yahoo profile (historical) or a
present-day snapshot with no filing context. This module composes the India
answer from the sources that are actually point-in-time:

* the Yahoo profile, unchanged and still withheld for a historical run;
* the last announcements, each named, dated in IST and linked;
* the latest structured results (xbrl.py) and the latest annual balance sheet,
  which may come from an earlier filing than the P&L — quarterly instances
  carry no balance sheet;
* the latest shareholding pattern, labelled with the period it describes.

Coverage is partial by measurement, not by omission: the integrated index only
starts in April 2025, so most quarters exist as an announcement and a PDF. A
filing with no instance is named and linked with an explicit "no figures stated
here" — the pack never derives numbers from a PDF, and never fills a gap with
another issuer's or another quarter's figures.

The unit is absolute INR. NSE instances declare ``LevelOfRounding`` as crores
while their fact values are already absolute rupees, so rescaling on the
declaration would be wrong by a factor of ten million.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from tradingagents.dataflows import markets
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.ist import ist_date, parse_ist, to_ist, today_ist
from tradingagents.dataflows.vendors.nse import client, filings, shareholding, xbrl

logger = logging.getLogger(__name__)

# The integrated index starts in April 2025. Three years is enough to find the
# latest annual instance for any run date while keeping the index request bounded.
_FINANCIALS_LOOKBACK_DAYS = 3 * 365

# A filing's instance never changes after publication, so one download serves
# every later run over the same period.
_XBRL_TTL_SECONDS = 7 * 24 * 60 * 60

# Four rows is the latest quarter, its YTD/annual sibling and one revision;
# walking further would spend requests on a balance sheet that is not there.
_MAX_INSTANCE_ATTEMPTS = 4

# One quarter's instance is a few dozen kilobytes; a cap keeps a malformed
# response from being read into memory as if it were an instance.
_MAX_INSTANCE_BYTES = 8 * 1024 * 1024


def get_fundamentals(ticker: str, as_of_date: str | None = None) -> str:
    """The fundamentals text for an Indian listing, as of ``as_of_date``.

    Self-gates every non-India symbol so the router can keep one chain for all
    markets: this vendor raises, the next one serves.
    """
    if not markets.is_india(ticker):
        raise NoMarketDataError(ticker, ticker, "not an NSE/BSE listing")

    as_of = as_of_date or today_ist().isoformat()
    date.fromisoformat(as_of)  # fail on a malformed date before any request

    blocks = [_profile(ticker, as_of), _provenance(as_of)]

    try:
        recent = _recent_with_results(ticker, as_of)
        blocks.append(filings.format_recent_filings(recent, ticker, as_of))
    except VendorError as exc:
        blocks.append(
            f"### As filed with NSE — {ticker}\n\n"
            f"Filings could not be read right now ({exc}). No filing is described "
            "here, and no figure is stated in its place."
        )

    structured = _structured(ticker, as_of)
    if structured:
        blocks.append(structured)

    try:
        blocks.append(shareholding.get_shareholding(ticker, as_of))
    except VendorError as exc:
        blocks.append(
            f"### Shareholding Pattern for {ticker}\n\n"
            f"Shareholding could not be read right now ({exc}). No percentage is stated."
        )

    return "\n\n".join(block for block in blocks if block)


def _recent_with_results(ticker: str, as_of: str, limit: int = 3) -> list[filings.Filing]:
    """The newest ``limit`` filings, always including the latest results filing.

    The three newest rows for a large cap are routinely investor-meet updates
    and press releases; the results announcement can sit weeks back, and it is
    the one the block exists to show. The results row is added only when the
    newest rows do not already contain it, so the block stays short.
    """
    recent = filings.recent_filings(ticker, as_of, limit=limit)
    results = filings.results_filings(ticker, as_of, limit=1)
    if not results:
        return recent
    latest = results[0]
    if any(item.url == latest.url for item in recent):
        return recent
    merged = recent + [latest]
    merged.sort(key=lambda filing: filing.published, reverse=True)
    return merged


def _profile(ticker: str, as_of: str) -> str:
    """Yahoo's profile, or an honest note when the vendor cannot serve it.

    A historical run already withholds the snapshot (it is present-day data); a
    vendor failure must not take the filings down with it, so it becomes one
    line and the rest of the block still ships.
    """
    from tradingagents.dataflows.vendors.yahoo.fundamentals import (
        get_fundamentals as get_yahoo_fundamentals,
    )

    try:
        return get_yahoo_fundamentals(ticker, as_of)
    except VendorError as exc:
        return (
            f"Company profile unavailable right now ({exc}). The filings below are "
            "what the exchange published; no profile metric is stated."
        )


def _provenance(as_of: str) -> str:
    if as_of < today_ist().isoformat():
        return (
            f"Historical run dated {as_of}: the present-day company profile is "
            "withheld, and everything below is what was public on that date."
        )
    return (
        f"Live run dated {as_of}: the company profile above is current; the "
        "filings below are the latest public on that date."
    )


def _day(value: object) -> date | None:
    """The IST calendar day of an NSE date field, or None when unreadable."""
    if not value:
        return None
    try:
        return ist_date(str(value))
    except ValueError:
        return None


def _financial_rows(ticker: str, as_of: str) -> list[dict]:
    """Structured-results rows public on ``as_of``, newest period first.

    One row per period end: the original filing before a revision, and the
    configured statement basis before the other one. A revision still wins when
    no original exists in the window.
    """
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=_FINANCIALS_LOOKBACK_DAYS)
    preferred = str((get_config().get("india") or {}).get("statement_basis") or "consolidated")

    candidates: list[tuple[date, int, int, date, dict]] = []
    for row in filings.integrated_filings(ticker, start.isoformat(), end.isoformat()):
        if "financials" not in str(row.get("type") or "").lower():
            continue
        period_end = _day(row.get("qe_Date"))
        broadcast = _day(row.get("broadcast_Date"))
        if period_end is None or broadcast is None:
            continue
        if period_end > end or broadcast > end:
            continue
        basis = str(row.get("consolidated") or "").strip().lower()
        candidates.append(
            (
                period_end,
                0 if "revis" in str(row.get("type_Sub") or "").lower() else 1,
                1 if basis == preferred else 0,
                broadcast,
                row,
            )
        )

    candidates.sort(key=lambda candidate: candidate[:4], reverse=True)
    rows: list[dict] = []
    seen: set[date] = set()
    for period_end, _original, _basis, _broadcast, row in candidates:
        if period_end in seen:
            continue
        seen.add(period_end)
        rows.append(row)
    return rows


def _fetch_instance(row: dict, ticker: str) -> xbrl.ParsedInstance | None:
    """Fetch and parse one row's instance; None when it cannot be read.

    A vendor failure and an unreadable instance are both "no structured figures
    for this filing", which is a fact about coverage rather than a reason to
    fail the pack. The caller names the filing and links the PDF instead.
    """
    url = row.get("xbrl")
    period_end = _day(row.get("qe_Date"))
    if not isinstance(url, str) or not url.startswith("http") or period_end is None:
        return None

    name = url.rsplit("/", 1)[-1] or "instance.xml"
    try:
        xml = client.get_bytes(
            url,
            bucket="archives",
            ttl_seconds=_XBRL_TTL_SECONDS,
            namespace="xbrl",
            key=f"{period_end.isoformat()}-{name}",
        )
        if len(xml) > _MAX_INSTANCE_BYTES:
            raise ValueError(f"instance is {len(xml)} bytes, larger than any real filing")
        return xbrl.parse_instance(xml, period_end, consolidated=None)
    except (VendorError, ValueError) as exc:
        logger.debug("no parseable instance for %s at %s: %s", ticker, url, exc)
        return None


def _structured(ticker: str, as_of: str) -> str:
    """The latest P&L and the latest annual balance sheet, when they exist."""
    rows = _financial_rows(ticker, as_of)
    if not rows:
        return (
            f"### Structured results (NSE XBRL) — {ticker}\n\n"
            "No structured results filing is public on or before this date. The "
            "announcements above are the complete record of what was filed."
        )

    income: tuple[xbrl.ParsedInstance, dict] | None = None
    balance: tuple[xbrl.ParsedInstance, dict] | None = None
    for row in rows[:_MAX_INSTANCE_ATTEMPTS]:
        parsed = _fetch_instance(row, ticker)
        if parsed is None:
            continue
        if income is None and parsed.statements.get("income_statement"):
            income = (parsed, row)
        if parsed.statements.get("balance_sheet"):
            balance = (parsed, row)
            break

    blocks: list[str] = []
    if income is not None:
        blocks.append(_format_statement(income[0], income[1], ticker, as_of, "Income Statement"))
    else:
        blocks.append(_unstructured_notice(rows[0], ticker, as_of))

    if balance is not None:
        blocks.append(_format_statement(balance[0], balance[1], ticker, as_of, "Balance Sheet"))
        if balance[0].statements.get("cashflow"):
            blocks.append(
                _format_statement(balance[0], balance[1], ticker, as_of, "Cash Flow Statement")
            )

    if income is not None and balance is None:
        blocks.append(
            "No annual instance with a balance sheet is public on or before this "
            "date, so none is shown."
        )
    return "\n\n".join(blocks)


_KINDS = {
    "Income Statement": ("income_statement", "Income Statement"),
    "Balance Sheet": ("balance_sheet", "Balance Sheet"),
    "Cash Flow Statement": ("cashflow", "Cash Flow Statement"),
}


def _format_statement(
    parsed: xbrl.ParsedInstance,
    row: dict,
    ticker: str,
    as_of: str,
    title: str,
) -> str:
    key, heading = _KINDS[title]
    rows = parsed.statements.get(key) or []
    days = (parsed.period_end - parsed.period_start).days + 1
    period = "annual" if 300 <= days <= 400 else "quarterly"

    broadcast = _broadcast(row)
    audited = str(row.get("audited") or "").strip()
    facts = ", ".join(
        part for part in (parsed.basis, audited.lower(), f"taxonomy {parsed.taxonomy}") if part
    )
    lines = [
        f"# {heading} for {ticker} ({period}), INR unless the row says otherwise",
        f"# NSE XBRL filed on or before {as_of} (broadcast {broadcast}, {facts}), "
        f"period {parsed.period_start.isoformat()} to {parsed.period_end.isoformat()}",
        "",
        "| Line item | Value | Unit |",
        "| --- | ---: | --- |",
    ]
    for item in rows:
        lines.append(f"| {item.label} | {_number(item.value, item.unit)} | {item.unit} |")
    return "\n".join(lines)


def _broadcast(row: dict) -> str:
    raw = row.get("broadcast_Date")
    if not raw:
        return "date not stated"
    try:
        moment = to_ist(parse_ist(str(raw)))
    except ValueError:
        return str(raw)
    return moment.strftime("%Y-%m-%d %H:%M IST")


def _number(value: float, unit: str) -> str:
    if unit == "INR":
        return f"{value:,.0f}"
    if unit == "INR/share":
        return f"{value:,.2f}"
    if unit == "ratio":
        return f"{value:.4f}"
    return f"{value:,.0f}"


def _unstructured_notice(row: dict, ticker: str, as_of: str) -> str:
    """The §5.3 "filed but not structured" notice, naming the filing."""
    period_end = _day(row.get("qe_Date"))
    when = _broadcast(row)
    subject = str(row.get("type") or "Results filing").strip()
    pdf = str(row.get("pdf_attach") or "").strip()
    link = f" The filing is at {pdf}." if pdf.startswith("http") else ""
    stated = "carry no structured XBRL" if not row.get("xbrl") else "could not be read as XBRL"
    period = f" for the period ended {period_end.isoformat()}" if period_end else ""
    return (
        f"# Income Statement for {ticker} as of {as_of}\n"
        f"# Results{period} were filed on {when} (\"{subject}\") but {stated}."
        f"{link} No figures are stated here: the report would have to read a PDF "
        "to obtain them."
    )
