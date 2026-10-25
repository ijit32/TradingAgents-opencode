#!/usr/bin/env python3
"""Phase 2a structured-coverage audit for NSE/BSE filings.

Measures, for a fixed 20-issuer sample and the last eight calendar quarters plus
three annual reports:

1. whether the corporate-announcements feed carries a results announcement;
2. whether the integrated-filing index carries a ``Financials`` row;
3. whether the XBRL instance parses and carries a duration-matched,
   non-dimensional revenue+profit context (quarters), or a non-dimensional
   balance-sheet instant (annuals).

This is a measurement tool, not product code. It writes one JSON file per issuer
as it goes, so a crash or an open circuit leaves the completed work on disk. It
uses only the repo's NSE transport, so the throttle, cache and breaker are the
same ones the adapters will use.

Usage::

    .venv/bin/python scripts/india_coverage_audit.py
    .venv/bin/python scripts/india_coverage_audit.py --refresh   # ignore saved files

Artifacts land in ``docs/india-phase2a-artifacts/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.ist import ist_date
from tradingagents.dataflows.vendors.nse import client, issuer

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "india-phase2a-artifacts"
INSTANCE_DIR = OUT_DIR / "instances"

# The fixed measurement window. Filing feeds are asked for the whole span once
# per issuer and filtered client-side, so a quarter with no row is a real miss
# rather than a window bug.
WINDOW_START = date(2023, 7, 1)
WINDOW_END = date(2025, 10, 31)
QUARTER_ENDS = [
    date(2025, 6, 30),
    date(2025, 3, 31),
    date(2024, 12, 31),
    date(2024, 9, 30),
    date(2024, 6, 30),
    date(2024, 3, 31),
    date(2023, 12, 31),
    date(2023, 9, 30),
]
FY_ENDS = [date(2025, 3, 31), date(2024, 3, 31), date(2023, 3, 31)]

# SEBI's quarterly deadline is 45 days; 90 days is deliberately generous, so a
# filing counted as "on time" here would have been visible in a normal quarter.
REPORTING_LAG_DAYS = 90

SAMPLE = [
    # bucket, NSE symbol (None for .BO-only), note
    ("large", "RELIANCE", ""),
    ("large", "TCS", ""),
    ("large", "HDFCBANK", ""),
    ("large", "INFY", ""),
    ("large", "ICICIBANK", ""),
    ("mid", "FEDERALBNK", ""),
    ("mid", "MPHASIS", ""),
    ("mid", "GODREJPROP", ""),
    ("mid", "INDHOTEL", ""),
    ("mid", "CUMMINSIND", ""),
    ("small", "KEC", ""),
    ("small", "CARBORUNIV", ""),
    ("small", "JYOTHYLAB", ""),
    ("small", "RAIN", ""),
    ("small", "TEJASNET", ""),
    # Yahoo lists these with a BSE quote; the name is absent from NSE's EQUITY_L
    # master and the NSE symbol is absent, so they cannot be keyed by NSE feeds.
    # Verified 2026-10-25 against Yahoo chart metadata and EQUITY_L.
    ("bo_only", "NAPEROL.BO", "Naperol Investments Limited"),
    ("bo_only", "SKPSEC.BO", "SKP Securities Limited"),
    ("bo_only", "WARRENTEA.BO", "Warren Tea Limited"),
    ("bo_only", "SURAJ.BO", "Suraj Products Limited"),
    ("bo_only", "GALXBRG.BO", "Galaxy Bearings Limited"),
]

ANNOUNCEMENT_PATH = "/api/corporate-announcements"
INTEGRATED_PATH = "/api/integrated-filing-results"

# A results announcement is described by one of these; the exchange is not
# consistent across issuers and years.
_RESULTS_HINTS = (
    "financial result",
    "financial results",
    "outcome of board meeting",
    "results",
)

REVENUE_TAGS = {
    "RevenueFromOperations",
    "Revenue",
    "RevenueFromContractsWithCustomers",
    "TotalRevenue",
    "IncomeFromOperations",
    "TotalIncome",
    "Income",
    "GrossIncome",
    "InterestEarned",
    "InterestIncome",
    "NetSales",
    "Turnover",
    "RevenueFromSaleOfGoods",
    "RevenueFromSaleOfServices",
}
PROFIT_TAGS = {
    "ProfitLossForPeriod",
    "ProfitLoss",
    "ProfitLossAfterTax",
    "ProfitAfterTax",
    "NetProfitLossForPeriod",
    "ProfitLossForThePeriod",
    "NetProfit",
    "ProfitBeforeTax",
}
BALANCE_SHEET_TAGS = {
    "Assets",
    "EquityAndLiabilities",
    "CapitalAndLiabilities",
    "TotalAssets",
    "TotalEquityAndLiabilities",
}

_XBRLI = "{http://www.xbrl.org/2003/instance}"


class Abort(Exception):
    """The NSE breaker opened; stop rather than spend the rest of the budget."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _log(message: str) -> None:
    print(message, flush=True)


def _window_params() -> dict[str, str]:
    return {
        "from_date": WINDOW_START.strftime("%d-%m-%Y"),
        "to_date": WINDOW_END.strftime("%d-%m-%Y"),
    }


def _announcements(symbol: str) -> list[dict]:
    params = {"index": "equities", **_window_params(), "symbol": symbol}
    payload = client.get_json(
        ANNOUNCEMENT_PATH,
        params,
        ttl_seconds=7 * 24 * 60 * 60,
        namespace="phase2a",
        key=f"ann-{symbol}-{WINDOW_START}-{WINDOW_END}",
    )
    return payload if isinstance(payload, list) else []


def _integrated_rows(symbol: str) -> list[dict]:
    params = {
        "index": "equities",
        **_window_params(),
        "symbol": symbol,
        "size": 2000,
        "page": 1,
    }
    payload = client.get_json(
        INTEGRATED_PATH,
        params,
        ttl_seconds=7 * 24 * 60 * 60,
        namespace="phase2a",
        key=f"ifr-{symbol}-{WINDOW_START}-{WINDOW_END}",
    )
    if not isinstance(payload, dict):
        return []
    rows = list(payload.get("data") or [])
    total = int(payload.get("totalCount") or 0)
    page = 1
    while len(rows) < total and page < 10:
        page += 1
        more = client.get_json(
            INTEGRATED_PATH,
            {**params, "page": page},
            ttl_seconds=7 * 24 * 60 * 60,
            namespace="phase2a",
            key=f"ifr-{symbol}-{WINDOW_START}-{WINDOW_END}-p{page}",
        )
        chunk = (more or {}).get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
    return rows


def _row_date(row: dict, field: str) -> date | None:
    raw = row.get(field)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return ist_date(raw)
    except ValueError:
        return None


def _looks_like_results(desc: str, text: str) -> bool:
    haystack = f"{desc} {text}".casefold()
    return any(hint in haystack for hint in _RESULTS_HINTS)


def _announcement_evidence(rows: list[dict], qe: date) -> list[dict]:
    """Results announcements published in the quarter's reporting window."""
    limit = qe + timedelta(days=REPORTING_LAG_DAYS)
    found = []
    for row in rows:
        published = _row_date(row, "an_dt")
        if published is None or not (qe <= published <= limit):
            continue
        desc = str(row.get("desc") or "")
        text = str(row.get("attchmntText") or "")
        if not _looks_like_results(desc, text):
            continue
        found.append(
            {
                "desc": desc,
                "an_dt": row.get("an_dt"),
                "published_on": published.isoformat(),
                "hasXbrl": row.get("hasXbrl"),
                "attachment": row.get("attchmntFile"),
                "text": text[:240],
            }
        )
    found.sort(key=lambda item: item["published_on"])
    return found


def _pick_financial_row(rows: list[dict], qe: date) -> dict | None:
    """The consolidated original Financials row for ``qe``, else an honest fallback."""
    candidates = []
    for row in rows:
        if "financials" not in str(row.get("type") or "").casefold():
            continue
        if _row_date(row, "qe_Date") != qe:
            continue
        candidates.append(row)
    if not candidates:
        return None

    def rank(row: dict) -> tuple:
        basis = str(row.get("consolidated") or "")
        sub = str(row.get("type_Sub") or "")
        published = _row_date(row, "broadcast_Date") or date.max
        # Consolidated is the plan's default; Original beats a revision; within
        # a class the earliest publication is the one a point-in-time run saw.
        return (
            0 if basis.casefold() == "consolidated" else 1,
            0 if sub.casefold() == "original" else 1,
            published,
        )

    return sorted(candidates, key=rank)[0]


def _quarter_start(qe: date) -> date:
    month = ((qe.month - 1) // 3) * 3 + 1
    return date(qe.year, month, 1)


def _parse_contexts(root: ET.Element) -> dict[str, dict]:
    contexts: dict[str, dict] = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        context_id = el.get("id")
        if not context_id:
            continue
        dimensions = [
            child for child in el.iter() if _local(child.tag) in ("explicitMember", "typedMember")
        ]
        period: dict[str, date | None] = {"start": None, "end": None, "instant": None}
        key_for = {"startDate": "start", "endDate": "end", "instant": "instant"}
        for child in el.iter():
            name = _local(child.tag)
            if name in key_for and child.text:
                try:
                    period[key_for[name]] = date.fromisoformat(child.text.strip())
                except ValueError:
                    period[key_for[name]] = None
        contexts[context_id] = {"dimensional": bool(dimensions), **period}
    return contexts


def _parse_facts(root: ET.Element) -> dict[str, set[str]]:
    """Local fact names per context, numeric facts only."""
    facts: dict[str, set[str]] = {}
    for el in root.iter():
        context_ref = el.get("contextRef")
        if not context_ref or not el.get("unitRef") or not (el.text or "").strip():
            continue
        raw = el.text.strip().replace(",", "")
        try:
            float(raw)
        except ValueError:
            continue
        facts.setdefault(context_ref, set()).add(_local(el.tag))
    return facts


def _analyse_instance(data: bytes, qe: date, *, annual: bool) -> dict:
    """Answer 'parseable for this period?' and, if not, why not."""
    result: dict = {
        "xml": False,
        "quarterly_context": None,
        "accepted_tags": [],
        "balance_sheet": False,
        "balance_sheet_tags": [],
        "reason": None,
    }
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        result["reason"] = f"xml: {exc}"
        return result
    result["xml"] = True
    if _local(root.tag) != "xbrl":
        result["reason"] = f"root element is {_local(root.tag)!r}, not xbrl"
        return result

    contexts = _parse_contexts(root)
    facts = _parse_facts(root)
    start = _quarter_start(qe) if not annual else date(qe.year - 1, 4, 1)

    duration_ids = [
        cid
        for cid, ctx in contexts.items()
        if not ctx["dimensional"] and ctx["start"] == start and ctx["end"] == qe
    ]
    dimensional_only = [
        cid for cid, ctx in contexts.items() if ctx["dimensional"] and ctx["start"] and ctx["end"]
    ]

    if not duration_ids:
        result["reason"] = (
            "dimensional-only: no non-dimensional context for "
            f"{start}..{qe} ({len(dimensional_only)} dimensional duration contexts)"
            if dimensional_only
            else f"no context for {start}..{qe}"
        )
    else:
        for cid in duration_ids:
            names = facts.get(cid, set())
            revenue = sorted(names & REVENUE_TAGS)
            profit = sorted(names & PROFIT_TAGS)
            if revenue and profit:
                result["quarterly_context"] = cid
                result["accepted_tags"] = [*revenue, *profit]
                break
        if not result["quarterly_context"]:
            names = sorted(set().union(*(facts.get(cid, set()) for cid in duration_ids)))
            result["reason"] = (
                f"no revenue+profit fact pair in a duration context; facts={names[:10]}"
            )

    instant_ids = [
        cid
        for cid, ctx in contexts.items()
        if not ctx["dimensional"]
        and ctx["instant"] is not None
        and abs((ctx["instant"] - qe).days) <= 3
    ]
    for cid in instant_ids:
        tags = sorted(facts.get(cid, set()) & BALANCE_SHEET_TAGS)
        if tags:
            result["balance_sheet"] = True
            result["balance_sheet_tags"] = tags
            break
    return result


def _save_instance(symbol: str, qe: date, data: bytes) -> str | None:
    """Keep small instances as evidence; the threshold avoids a fat artifact dir."""
    if len(data) > 400_000:
        return None
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{symbol.replace('.', '_')}-{qe.isoformat()}.xml"
    path = INSTANCE_DIR / name
    path.write_bytes(data)
    return str(path.relative_to(ROOT))


def _measure_quarter(
    symbol: str, qe: date, announcements: list[dict], rows: list[dict], evidence: dict
) -> dict:
    record: dict = {
        "quarter_end": qe.isoformat(),
        "announcement": None,
        "integrated": None,
        "instance": None,
    }
    ann = _announcement_evidence(announcements, qe)
    record["announcement"] = {
        "exists": bool(ann),
        "count": len(ann),
        "candidates": ann[:3],
    }

    row = _pick_financial_row(rows, qe)
    if row is None:
        record["instance"] = {
            "parseable": False,
            "status": "no_filing_row",
            "detail": "integrated index has no Financials row for this quarter end",
        }
        return record

    published = _row_date(row, "broadcast_Date")
    record["integrated"] = {
        "qe_Date": row.get("qe_Date"),
        "broadcast_Date": row.get("broadcast_Date"),
        "published_on": published.isoformat() if published else None,
        "days_after_quarter": (published - qe).days if published else None,
        "within_reporting_window": bool(
            published and published <= qe + timedelta(days=REPORTING_LAG_DAYS)
        ),
        "basis": row.get("consolidated"),
        "type_Sub": row.get("type_Sub"),
        "type": row.get("type"),
        "xbrl": row.get("xbrl"),
        "ixbrl": row.get("ixbrl"),
    }
    xbrl_url = row.get("xbrl")
    if not xbrl_url:
        record["instance"] = {
            "parseable": False,
            "status": "no_xbrl",
            "detail": "Financials row has no xbrl link",
        }
        return record

    try:
        data = client.get_bytes(
            xbrl_url,
            ttl_seconds=90 * 24 * 60 * 60,
            namespace="phase2a-instances",
            key=f"xbrl-{row.get('seq_Id') or xbrl_url.rsplit('/', 1)[-1]}",
        )
    except VendorUnavailableError as exc:
        if "circuit open" in str(exc).casefold():
            raise Abort(str(exc)) from exc
        record["instance"] = {"parseable": False, "status": "fetch_failed", "detail": str(exc)}
        return record

    analysis = _analyse_instance(data, qe, annual=False)
    parseable = bool(analysis["quarterly_context"])
    record["instance"] = {
        "parseable": parseable,
        "status": "ok" if parseable else "unparseable",
        "detail": analysis["reason"],
        "accepted_tags": analysis["accepted_tags"],
        "context": analysis["quarterly_context"],
        "balance_sheet": analysis["balance_sheet"],
        "balance_sheet_tags": analysis["balance_sheet_tags"],
        "xml_bytes": len(data),
    }
    if parseable:
        evidence.setdefault("quarter_instance", (qe, data))
    elif evidence.get("failed_instance") is None:
        evidence["failed_instance"] = (qe, data, analysis["reason"])
    return record


def _measure_annual(symbol: str, fy_end: date, rows: list[dict], evidence: dict) -> dict:
    record: dict = {"fy_end": fy_end.isoformat(), "integrated": None, "instance": None}
    row = _pick_financial_row(rows, fy_end)
    if row is None:
        record["instance"] = {
            "balance_sheet": False,
            "status": "no_filing_row",
            "detail": "integrated index has no Financials row for this year end",
        }
        return record
    published = _row_date(row, "broadcast_Date")
    record["integrated"] = {
        "broadcast_Date": row.get("broadcast_Date"),
        "published_on": published.isoformat() if published else None,
        "basis": row.get("consolidated"),
        "type_Sub": row.get("type_Sub"),
        "xbrl": row.get("xbrl"),
    }
    xbrl_url = row.get("xbrl")
    if not xbrl_url:
        record["instance"] = {
            "balance_sheet": False,
            "status": "no_xbrl",
            "detail": "Financials row has no xbrl link",
        }
        return record
    try:
        data = client.get_bytes(
            xbrl_url,
            ttl_seconds=90 * 24 * 60 * 60,
            namespace="phase2a-instances",
            key=f"xbrl-{row.get('seq_Id') or xbrl_url.rsplit('/', 1)[-1]}",
        )
    except VendorUnavailableError as exc:
        if "circuit open" in str(exc).casefold():
            raise Abort(str(exc)) from exc
        record["instance"] = {"balance_sheet": False, "status": "fetch_failed", "detail": str(exc)}
        return record

    analysis = _analyse_instance(data, fy_end, annual=True)
    record["instance"] = {
        "balance_sheet": analysis["balance_sheet"],
        "status": "ok" if analysis["balance_sheet"] else "no_balance_sheet",
        "detail": analysis["reason"],
        "balance_sheet_tags": analysis["balance_sheet_tags"],
        "annual_pnl_ok": bool(analysis["quarterly_context"]),
        "accepted_tags": analysis["accepted_tags"],
        "xml_bytes": len(data),
    }
    if analysis["balance_sheet"] and evidence.get("annual_instance") is None:
        evidence["annual_instance"] = (fy_end, data)
    return record


def _measure_bo(symbol: str, name: str, master_symbols: set[str]) -> dict:
    bare = symbol[:-3] if symbol.endswith(".BO") else symbol
    resolved = issuer.issuer_for(symbol)
    absent = resolved is None and bare.upper() not in master_symbols
    return {
        "symbol": symbol,
        "bucket": "bo_only",
        "verification": {
            "yahoo_name": name,
            "nse_symbol": None,
            "in_nse_master": not absent,
            "evidence": (
                "Yahoo BSE quote with this name; name/symbol absent from NSE "
                "EQUITY_L. BSE JSON API is HTTP 403 from this network, so no "
                "BSE filing feed can key it."
            ),
        },
        "quarters": [
            {
                "quarter_end": qe.isoformat(),
                "announcement": {"exists": None, "detail": "no NSE symbol"},
                "integrated": None,
                "instance": {
                    "parseable": False,
                    "status": "no_nse_key",
                    "detail": (
                        "BSE-only scrip: the NSE feeds are keyed by NSE symbol and "
                        "the BSE API returns HTTP 403 from this network"
                    ),
                },
            }
            for qe in QUARTER_ENDS
        ],
        "annuals": [
            {
                "fy_end": fy.isoformat(),
                "integrated": None,
                "instance": {
                    "balance_sheet": False,
                    "status": "no_nse_key",
                    "detail": "BSE-only scrip; no reachable BSE filing feed (HTTP 403)",
                },
            }
            for fy in FY_ENDS
        ],
    }


def _measure_nse(symbol: str, bucket: str) -> dict:
    announcements = _announcements(symbol)
    rows = _integrated_rows(symbol)
    _log(
        f"  announcements={len(announcements)} integrated_rows={len(rows)} "
        f"financials={sum('financials' in str(r.get('type') or '').casefold() for r in rows)}"
    )
    evidence: dict = {}
    record = {
        "symbol": symbol,
        "bucket": bucket,
        "verification": {
            "in_nse_master": issuer.issuer_for(f"{symbol}.NS") is not None,
        },
        "quarters": [
            _measure_quarter(symbol, qe, announcements, rows, evidence) for qe in QUARTER_ENDS
        ],
        "annuals": [_measure_annual(symbol, fy, rows, evidence) for fy in FY_ENDS],
    }
    if item := evidence.get("quarter_instance"):
        record["artifacts"] = {"quarter_instance": _save_instance(symbol, *item)}
    if item := evidence.get("annual_instance"):
        record.setdefault("artifacts", {})["annual_instance"] = _save_instance(symbol, *item)
    if item := evidence.get("failed_instance"):
        qe, data, reason = item
        record.setdefault("artifacts", {})["failed_instance"] = {
            "saved": _save_instance(symbol, qe, data),
            "reason": reason,
        }
    return record


def _write_individual(record: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = record["symbol"].replace(".", "_")
    (OUT_DIR / f"coverage-{slug}.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )


def _load_existing(symbol: str) -> dict | None:
    slug = symbol.replace(".", "_")
    path = OUT_DIR / f"coverage-{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _tally(records: list[dict]) -> dict:
    nse = [r for r in records if r["bucket"] != "bo_only"]
    bo = [r for r in records if r["bucket"] == "bo_only"]
    quarters = sum(len(r["quarters"]) for r in nse)
    parseable = sum(1 for r in nse for q in r["quarters"] if (q["instance"] or {}).get("parseable"))
    on_time = sum(
        1
        for r in nse
        for q in r["quarters"]
        if (q["instance"] or {}).get("parseable")
        and (q["integrated"] or {}).get("within_reporting_window")
    )
    annual_issuers = sum(
        1 for r in nse if any((a["instance"] or {}).get("balance_sheet") for a in r["annuals"])
    )
    # The integrated-filing regime starts with Q3 FY25; Q4 FY25 and Q1 FY26 are
    # the only full quarters in this sample's window.
    recent_quarters = [q for r in nse for q in r["quarters"] if q["quarter_end"] >= "2025-03-31"]
    recent_parseable = sum(1 for q in recent_quarters if (q["instance"] or {}).get("parseable"))
    return {
        "nse_issuers": len(nse),
        "bo_issuers": len(bo),
        "nse_quarters": quarters,
        "nse_quarters_parseable": parseable,
        "nse_quarters_parseable_on_time": on_time,
        "nse_quarter_coverage_pct": round(100.0 * parseable / quarters, 1) if quarters else 0.0,
        "pct_with_bo_denominator": round(100.0 * parseable / (quarters + 8 * len(bo)), 1)
        if quarters
        else 0.0,
        "nse_annual_issuers_with_bs": annual_issuers,
        "nse_annual_issuers": len(nse),
        "recent_regime_quarters": len(recent_quarters),
        "recent_regime_parseable": recent_parseable,
        "recent_regime_pct": round(100.0 * recent_parseable / len(recent_quarters), 1)
        if recent_quarters
        else 0.0,
        "announcement_coverage": sum(
            1 for r in nse for q in r["quarters"] if (q["announcement"] or {}).get("exists")
        ),
        "bo_quarters_measured": 8 * len(bo),
        "bo_quarters_parseable": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="ignore saved per-issuer JSON")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    master_symbols = set(issuer.issuers())
    _log(
        f"Phase 2a coverage audit, {date.today().isoformat()}, "
        f"{len(SAMPLE)} issuers, windows {WINDOW_START}..{WINDOW_END}"
    )

    records: list[dict] = []
    aborted: str | None = None
    for index, (bucket, symbol, name) in enumerate(SAMPLE, start=1):
        _log(f"[{index}/{len(SAMPLE)}] {bucket:8} {symbol}")
        if not args.refresh:
            existing = _load_existing(symbol)
            if existing is not None:
                _log("  loaded existing result")
                records.append(existing)
                continue
        try:
            if bucket == "bo_only":
                record = _measure_bo(symbol, name, master_symbols)
            else:
                record = _measure_nse(symbol, bucket)
        except Abort as exc:
            aborted = str(exc)
            _log(f"  ABORT: {exc}")
            break
        except VendorUnavailableError as exc:
            message = str(exc)
            _log(f"  vendor error, recording partial: {message}")
            if "circuit open" in message.casefold():
                aborted = message
                break
            record = {
                "symbol": symbol,
                "bucket": bucket,
                "verification": {},
                "error": message,
                "quarters": [],
                "annuals": [],
            }
        _write_individual(record)
        records.append(record)
        if bucket != "bo_only":
            ok = sum(1 for q in record["quarters"] if (q["instance"] or {}).get("parseable"))
            annual = sum(1 for a in record["annuals"] if (a["instance"] or {}).get("balance_sheet"))
            _log(
                f"  quarters parseable={ok}/{len(record['quarters'])} annual BS={annual}/{len(record['annuals'])}"
            )

    summary = _tally(records)
    combined = {
        "run_date": date.today().isoformat(),
        "method": {
            "announcements_url": f"{client.NSE_BASE}{ANNOUNCEMENT_PATH}",
            "integrated_url": f"{client.NSE_BASE}{INTEGRATED_PATH}",
            "window": {"from_date": WINDOW_START.isoformat(), "to_date": WINDOW_END.isoformat()},
            "params": {"index": "equities", "symbol": "<NSE symbol>", "size": 2000},
            "reporting_lag_days": REPORTING_LAG_DAYS,
        },
        "sample": [{"bucket": b, "symbol": s, "note": n} for b, s, n in SAMPLE],
        "summary": summary,
        "aborted": aborted,
        "issuers": records,
    }
    (OUT_DIR / "coverage.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    _log(json.dumps(summary, indent=2))
    if aborted:
        _log("Run stopped early; partial results are on disk.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
