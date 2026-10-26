"""Quarterly shareholding patterns: who owns the company, as last disclosed.

A shareholding row has two dates and they are months apart in spirit: ``date`` is
the period the holdings describe (a quarter end) and ``broadcastDate`` is when
the pattern became public. A run must select on ``broadcastDate`` — a quarter
that ended in September is not known in October until the issuer files — and it
must print the period end, because the snapshot is the holdings on that day, not
on the run's day (plan §3.3).

NSE's master endpoint returns the whole history back to 2021 in one response.
Columns pinned against live rows: ``pr_and_prgrp`` (promoter & promoter group %),
``public_val`` (public %), ``employeeTrusts`` (employee-trust %). The three sum
to about 100, so they are percentages, not share counts; they are printed exactly
as filed rather than recomputed.
"""

from __future__ import annotations

from datetime import date

from tradingagents.dataflows.ist import ist_date, parse_ist, to_ist, today_ist
from tradingagents.dataflows.vendors.nse import client
from tradingagents.dataflows.vendors.nse.filings import filing_symbol

_PATH = "/api/corporate-share-holdings-master"
_NAMESPACE = "shareholding"

# One request returns the whole history, so a day of cache is cheap and a stale
# day costs nothing: a pattern is filed once a quarter.
_TTL_SECONDS = 24 * 60 * 60

_PROMOTER = "pr_and_prgrp"
_PUBLIC = "public_val"
_EMPLOYEES = "employeeTrusts"


def _pct(value: object) -> str:
    """A percentage as filed, without inventing one the row does not carry."""
    text = str(value if value is not None else "").strip()
    return f"{text}%" if text else "unavailable"


def _ist_day(value: object) -> date | None:
    try:
        return ist_date(str(value))
    except (TypeError, ValueError):
        return None


def _rows(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _latest_per_period(rows: list[tuple[date, date, dict]]) -> list[tuple[date, date, dict]]:
    """One row per period end: the most recently published version of it.

    A restated pattern is the same quarter filed again. Keeping both would repeat
    a quarter in the history and push an older one out of a five-quarter window,
    and the revision is what the company stood behind on the run's date.
    """
    latest: dict[date, tuple[date, date, dict]] = {}
    for period_end, broadcast, row in rows:
        seen = latest.get(period_end)
        if seen is None or broadcast >= seen[1]:
            latest[period_end] = (period_end, broadcast, row)
    return sorted(latest.values(), key=lambda item: item[0], reverse=True)


def get_shareholding(ticker: str, curr_date: str, quarters: int = 5) -> str:
    """The shareholding pattern public on ``curr_date``, newest quarter first."""
    nse = filing_symbol(ticker)
    as_of = curr_date or today_ist().isoformat()
    end = date.fromisoformat(as_of)

    payload = client.get_json(
        _PATH,
        params={"index": "equities", "symbol": nse},
        ttl_seconds=_TTL_SECONDS,
        namespace=_NAMESPACE,
        key=nse,
    )

    known: list[tuple[date, date, dict]] = []
    later: list[date] = []
    for row in _rows(payload):
        stated = str(row.get("symbol") or "").strip().upper()
        if stated and stated != nse:
            continue
        period_end = _ist_day(row.get("date"))
        broadcast = _ist_day(row.get("broadcastDate"))
        if period_end is None or broadcast is None:
            continue
        if broadcast > end:
            # Filed after the run's date: it cannot be shown, but its existence
            # is worth naming so the reader knows the latest pattern is newer.
            later.append(period_end)
            continue
        known.append((period_end, broadcast, row))

    history = _latest_per_period(known)[: max(int(quarters), 1)]
    if not history:
        return (
            f"Shareholding pattern is unavailable for {ticker.upper()} on or before {as_of}: "
            "NSE carries no pattern for this symbol broadcast by that date.\n"
        )

    period_end, _, latest = history[0]
    # The row's own timestamp, rendered in IST for display; `_ist_day` above
    # already proved it parses.
    broadcast_stamp = to_ist(parse_ist(str(latest.get("broadcastDate")))).strftime(
        "%Y-%m-%d %H:%M IST"
    )

    lines = [
        f"# Shareholding Pattern for {ticker.upper()} as published on or before {as_of}",
        f"# Source quarter: period ended {period_end.isoformat()}, broadcast "
        f"{broadcast_stamp}.",
        f"# These are the holdings on {period_end.isoformat()}, not on {as_of}.",
    ]
    next_later = min(later) if later else None
    if next_later is not None:
        lines.append(
            f"# A pattern for the quarter ended {next_later.isoformat()} was filed after "
            f"{as_of} and is not shown."
        )
    lines += [
        "",
        f"Promoter & promoter group: {_pct(latest.get(_PROMOTER))}",
        f"Public: {_pct(latest.get(_PUBLIC))}",
        f"Employee trusts: {_pct(latest.get(_EMPLOYEES))}",
        "",
        "| Period end | Broadcast | Promoter & promoter group % |",
        "| --- | --- | --- |",
    ]
    for row_period, row_broadcast, row in history:
        lines.append(
            f"| {row_period.isoformat()} | {row_broadcast.isoformat()} | "
            f"{_pct(row.get(_PROMOTER))} |"
        )
    return "\n".join(lines) + "\n"
