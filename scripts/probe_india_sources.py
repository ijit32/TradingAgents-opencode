#!/usr/bin/env python3
"""Phase 5 measurement probe for the India macro sources.

Answers, live and per alias:

1. does Yahoo serve the quote, and does the module clip it to today;
2. does the FRED series ID exist (keyless ``fredgraph.csv`` + the series page's
   ``<title>``), and with ``FRED_API_KEY`` does FRED accept a
   ``realtime_start=realtime_end=today`` vintage pin for it;
3. does ``repo_rate_india`` still answer with its explicit gap notice.

This is a measurement tool, not product code. It only reads the vendor modules
and the two keyless FRED endpoints, so it measures the same surface the analyst
would see. Exit status is 1 when any Yahoo alias answers empty/unavailable or any
FRED candidate is missing; 0 otherwise.

Usage::

    uv run python scripts/probe_india_sources.py
    FRED_API_KEY=... uv run python scripts/probe_india_sources.py
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date

from tradingagents.dataflows.vendors import fred, india_macro

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
FRED_PAGE = "https://fred.stlouisfed.org/series/{series_id}"
REQUEST_TIMEOUT = 30
USER_AGENT = "tradingagents-india-macro-probe/1.0"

# A month is enough to prove the endpoint answers and the report renders; the
# module's own year default is exercised by the tests, not by this probe.
PROBE_LOOKBACK_DAYS = 30


def _get(url: str) -> str:
    """GET a text URL with a named client, as FRED's pages expect."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _first_lines(text: str, count: int = 6) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines()[:count])


def _probe_yahoo(as_of_date: str) -> list[str]:
    print(f"Yahoo aliases (live, clipped to {as_of_date}):")
    failures: list[str] = []
    for alias, series in india_macro.YAHOO_INDIA_SERIES.items():
        output = india_macro.get_macro_data(alias, as_of_date, PROBE_LOOKBACK_DAYS)
        # Both the empty answer and a failed request carry this sentence; either
        # way the alias was not verified live.
        ok = "missing coverage" not in output.casefold()
        print(f"  {'PASS' if ok else 'FAIL'} {alias} ({series.symbol})")
        print(_first_lines(output))
        if not ok:
            failures.append(alias)
    return failures


def _fred_csv_last_observation(series_id: str) -> tuple[str, str] | None:
    """Last populated observation from FRED's keyless CSV export.

    The CSV holds one row per period with an empty value where FRED has no
    observation, so the last row is not necessarily the last number.
    """
    text = _get(FRED_CSV.format(series_id=series_id))
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0]:
        return None
    last: tuple[str, str] | None = None
    for row in rows[1:]:
        if len(row) < 2:
            continue
        value = row[1].strip()
        if value and value != ".":
            last = (row[0].strip(), value)
    return last


def _fred_page_title(series_id: str) -> str:
    html = _get(FRED_PAGE.format(series_id=series_id))
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else "(no <title>)"


def _fred_vintage_pin(series_id: str, as_of_date: str) -> tuple[str, str, str] | None:
    """Prove FRED accepts a today-pinned vintage; return title/frequency/last obs."""
    realtime = {"realtime_start": as_of_date, "realtime_end": as_of_date}
    meta = fred._request("series", {"series_id": series_id, **realtime}).get("seriess") or []
    if not meta:
        return None
    info = meta[0]
    observations = fred._request(
        "series/observations",
        {"series_id": series_id, "sort_order": "asc", **realtime},
    ).get("observations") or []
    points = [
        (row["date"], row["value"])
        for row in observations
        if row.get("value") not in (".", None, "")
    ]
    last = f"{points[-1][1]} ({points[-1][0]})" if points else "no observation at this vintage"
    return info.get("title", series_id), info.get("frequency", ""), last


def _probe_fred(as_of_date: str) -> list[str]:
    has_key = bool(os.getenv("FRED_API_KEY"))
    print(f"FRED candidates (key {'present' if has_key else 'absent'}, as of {as_of_date}):")
    failures: list[str] = []
    for alias, series_id in india_macro.FRED_INDIA_SERIES.items():
        try:
            last = _fred_csv_last_observation(series_id)
            title = _fred_page_title(series_id)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  FAIL {alias} -> {series_id}: {exc}")
            failures.append(alias)
            continue

        if last is None:
            print(f"  FAIL {alias} -> {series_id}: no observation in fredgraph.csv")
            failures.append(alias)
            continue

        print(f"  PASS {alias} -> {series_id}: exists, last {last[1]} ({last[0]})")
        print(f"    <title>: {title or '(empty)'}")

        if not has_key:
            continue
        try:
            pinned = _fred_vintage_pin(series_id, as_of_date)
        except Exception as exc:  # noqa: BLE001 - a probe reports every failure
            print(f"    FAIL vintage pin {as_of_date}: {exc}")
            failures.append(alias)
            continue
        if pinned is None:
            print(f"    FAIL vintage pin {as_of_date}: no metadata returned")
            failures.append(alias)
            continue
        pinned_title, frequency, pinned_last = pinned
        print(
            f"    PASS vintage pin {as_of_date}: {pinned_title} "
            f"(frequency: {frequency or 'n/a'}), last {pinned_last}"
        )
    return failures


def _probe_repo_rate(as_of_date: str) -> bool:
    print("Repo rate:")
    output = india_macro.get_macro_data(india_macro.REPO_RATE_INDIA["alias"], as_of_date)
    ok = "no free point-in-time source" in output and "not estimated" in output
    print(
        "  "
        + ("not covered (explicit notice)" if ok else "FAIL: explicit notice missing")
    )
    return ok


def main() -> int:
    # The probe asks for today: it measures the source, not a backtest window.
    as_of_date = date.today().isoformat()
    print(f"India macro source probe, {as_of_date}\n")

    failures = _probe_yahoo(as_of_date)
    print()
    failures += _probe_fred(as_of_date)
    print()
    repo_ok = _probe_repo_rate(as_of_date)

    print()
    if failures or not repo_ok:
        print(f"RESULT: FAIL ({', '.join(failures) if failures else 'repo-rate notice'})")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
