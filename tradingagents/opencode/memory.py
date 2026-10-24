"""Memory settlement and the end-of-run artefacts, as worker actions.

The graph does two things the analysts do not: it settles past decisions at the
start (``_memory_step``) and records the finished decision, reports and state log
at the end (``record_decision`` + ``save_reports``). This module gives the plugin
the same two steps while keeping every model call in the plugin:

    memory-begin   decide which pending entries are due, render the reflection
                   prompts, and let the plugin generate them
    memory-apply   write the reflections back, then read the lessons and the
                   run note the Portfolio Manager and the report header use
    settle         write the report tree (md + html), the state log, and this
                   run's pending memory entry

Nothing here builds an LLM client. Prices come from the same settlement functions
the graph uses, so "due", the entry bar, and the alpha benchmark cannot drift.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory.log import TradingMemoryLog
from tradingagents.memory.reflection import build_reflection_prompt
from tradingagents.memory.settlement import fetch_returns, resolve_benchmark
from tradingagents.opencode.contract import AnalysisRequest, BadInput
from tradingagents.opencode.run_dir import RunDir

# The files the plugin and the later prompt stages read back.
PENDING_FILE = "pending.json"
REFLECTIONS_FILE = "reflections.json"
PAST_CONTEXT_FILE = "past_context.md"
NOTE_FILE = "note.txt"


def memory_config(request: AnalysisRequest) -> dict[str, Any]:
    """The config the memory log and settlement read, for this run.

    Only the paths a run may move are taken from the request; the holding
    period and benchmark map stay the repo's, so a plugin run and a CLI run
    score the same decision the same way.
    """
    return {
        "memory_log_path": str(
            Path(request.memory_log_path or DEFAULT_CONFIG["memory_log_path"]).expanduser()
        ),
        "memory_log_max_entries": DEFAULT_CONFIG.get("memory_log_max_entries"),
        "holding_period_days": DEFAULT_CONFIG.get("holding_period_days", 5),
        "benchmark_ticker": DEFAULT_CONFIG.get("benchmark_ticker"),
        "benchmark_map": DEFAULT_CONFIG.get("benchmark_map", {}),
        "results_dir": str(
            Path(request.results_dir or DEFAULT_CONFIG["results_dir"]).expanduser()
        ),
    }


def memory_log(request: AnalysisRequest) -> TradingMemoryLog:
    return TradingMemoryLog(memory_config(request))


def _as_of(request: AnalysisRequest) -> str | None:
    """Point-in-time cutoff, matching ``TradingAgentsGraph._memory_as_of``."""
    return request.trade_date if is_historical(request.trade_date) else None


def _key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("ticker", "")), str(item.get("date", ""))


def action_memory_begin(run: RunDir, args) -> dict[str, Any]:
    """Find the pending decisions whose holding window has traded.

    Fetching the prices here — rather than in the plugin — keeps "due" defined
    by one function. Entries whose window has not traded, and entries whose
    prices could not be fetched, stay pending and are reported separately: the
    first is normal, the second is a warning.
    """
    request = run.load_request()
    config = memory_config(request)
    log = TradingMemoryLog(config)
    pending = log.get_pending_entries()

    entries: list[dict[str, Any]] = []
    not_ready: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for entry in pending:
        ticker, date = _key(entry)
        if not ticker or not date:
            continue
        benchmark = resolve_benchmark(ticker, config)
        try:
            raw, alpha, days, resolution = fetch_returns(
                ticker,
                date,
                config.get("holding_period_days", 5),
                benchmark=benchmark,
            )
        except Exception as exc:  # noqa: BLE001 — a vendor error must not end the run
            failed.append({"ticker": ticker, "date": date, "reason": f"prices unavailable: {exc}"})
            continue
        if raw is None or alpha is None or days is None:
            not_ready.append({"ticker": ticker, "date": date})
            continue
        messages = build_reflection_prompt(
            entry.get("decision", ""),
            raw,
            alpha,
            benchmark_name=benchmark,
            holding_days=days,
        )
        entries.append(
            {
                "ticker": ticker,
                "date": date,
                "rating": entry.get("rating", ""),
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "resolution_date": resolution,
                "benchmark": benchmark,
                "system": messages[0][1],
                "user": messages[1][1],
            }
        )

    run.write_json(
        {
            "as_of": _as_of(request),
            "benchmark": resolve_benchmark(request.ticker, config),
            "entries": entries,
            "not_ready": not_ready,
            "failed": failed,
        },
        "memory",
        PENDING_FILE,
    )
    return {
        "ok": True,
        "action": "memory-begin",
        "log_path": config["memory_log_path"],
        "pending": len(pending),
        "due": len(entries),
        "entries": [
            {
                key: entry[key]
                for key in (
                    "ticker",
                    "date",
                    "rating",
                    "raw_return",
                    "alpha_return",
                    "holding_days",
                    "system",
                    "user",
                )
            }
            for entry in entries
        ],
        "not_ready": not_ready,
        "failed": failed,
    }


def _read_reflections(run: RunDir) -> dict[tuple[str, str], str]:
    payload = run.read_json("memory", REFLECTIONS_FILE, default={}) or {}
    if not isinstance(payload, dict):
        raise BadInput(f"memory/{REFLECTIONS_FILE} must be a JSON object")
    rows = payload.get("reflections")
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise BadInput(f"memory/{REFLECTIONS_FILE} must contain a reflections list")
    texts: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if text:
            texts[_key(row)] = text
    return texts


def action_memory_apply(run: RunDir, args) -> dict[str, Any]:
    """Write the generated reflections, then read the lessons for this run.

    One batched rewrite rather than a write per entry: the log is append-only
    markdown under a file lock, so a single read-modify-write keeps a killed run
    from leaving it half-updated. An entry whose reflection never arrived stays
    pending, exactly as a failed reflection does in the graph.
    """
    request = run.load_request()
    config = memory_config(request)
    log = TradingMemoryLog(config)

    pending = run.read_json("memory", PENDING_FILE, default={}) or {}
    entries = pending.get("entries") if isinstance(pending, dict) else None
    if not isinstance(entries, list):
        entries = []
    reflections = _read_reflections(run)

    updates: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ticker, date = _key(entry)
        text = reflections.get((ticker, date))
        if not text:
            missing.append({"ticker": ticker, "date": date})
            continue
        updates.append(
            {
                "ticker": ticker,
                "trade_date": date,
                "raw_return": entry.get("raw_return"),
                "alpha_return": entry.get("alpha_return"),
                "holding_days": entry.get("holding_days"),
                "reflection": text,
                "resolution_date": entry.get("resolution_date"),
            }
        )
    if updates:
        log.batch_update_with_outcomes(updates)

    note = ""
    failed = pending.get("failed") if isinstance(pending, dict) else None
    unsettled = len(missing) + (len(failed) if isinstance(failed, list) else 0)
    if unsettled:
        note = (
            f"{unsettled} past decision(s) could not be settled this run "
            "and stay pending."
        )
    try:
        past_context = log.get_past_context(request.ticker, as_of=_as_of(request))
    except Exception as exc:  # noqa: BLE001 — a broken log never ends the run
        past_context = ""
        note = note or f"The memory log could not be read this run ({type(exc).__name__})."

    run.write_text(past_context, "memory", PAST_CONTEXT_FILE)
    run.write_text(note, "memory", NOTE_FILE)
    return {
        "ok": True,
        "action": "memory-apply",
        "log_path": config["memory_log_path"],
        "settled": [
            {"ticker": entry["ticker"], "date": entry["trade_date"]} for entry in updates
        ],
        "missing": missing,
        "past_context_chars": len(past_context),
        "note": note,
    }


def _state_log_path(request: AnalysisRequest) -> Path:
    """Where the graph keeps a run's state log, under this run's results dir."""
    results = Path(memory_config(request)["results_dir"])
    return (
        results
        / safe_ticker_component(request.ticker)
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{request.trade_date}.json"
    )


def _write_state_log(request: AnalysisRequest, state: dict[str, Any], settings: dict[str, Any]) -> Path:
    """The graph's ``_log_state`` payload: the whole run, in one JSON file."""
    payload = {
        "company_of_interest": request.ticker,
        "trade_date": request.trade_date,
        "market_report": state.get("market_report", ""),
        "sentiment_report": state.get("sentiment_report", ""),
        "news_report": state.get("news_report", ""),
        "fundamentals_report": state.get("fundamentals_report", ""),
        "investment_debate_state": {
            "bull_history": (state.get("investment_debate_state") or {}).get("bull_history", ""),
            "bear_history": (state.get("investment_debate_state") or {}).get("bear_history", ""),
            "history": (state.get("investment_debate_state") or {}).get("history", ""),
            "current_response": (state.get("investment_debate_state") or {}).get(
                "current_response", ""
            ),
        },
        "trader_investment_plan": state.get("trader_investment_plan", ""),
        "risk_debate_state": {
            "aggressive_history": (state.get("risk_debate_state") or {}).get(
                "aggressive_history", ""
            ),
            "conservative_history": (state.get("risk_debate_state") or {}).get(
                "conservative_history", ""
            ),
            "neutral_history": (state.get("risk_debate_state") or {}).get(
                "neutral_history", ""
            ),
            "history": (state.get("risk_debate_state") or {}).get("history", ""),
        },
        "investment_plan": state.get("investment_plan", ""),
        "final_trade_decision": state.get("final_trade_decision", ""),
        "final_rating": run_rating(state),
        "run_settings": settings,
    }
    target = _state_log_path(request)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def _run_notes(run: RunDir, warnings: list[str]) -> Path:
    lines = ["# Run notes", ""]
    if warnings:
        lines.append(
            "Warnings recorded while this run produced its report. They bound what the "
            "report can claim; the report itself is unchanged so it stays comparable "
            "with a CLI run."
        )
        lines.append("")
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No warnings.")
    return run.write_text("\n".join(lines) + "\n", "reports", "run_notes.md")


def action_settle(run: RunDir, args) -> dict[str, Any]:
    """Write the report tree, the state log, and this run's memory entry.

    Idempotent: the report files are overwritten, the state log is overwritten,
    and ``store_decision`` refuses a second entry for the same ticker and date.
    A resume therefore settles again without duplicating anything.
    """
    request = run.load_request()
    state = run.read_json("final_state.json", default=None)
    if not isinstance(state, dict) or not state:
        raise BadInput("final_state.json is missing; run the stages first")

    settings = run.read_json("run.json", default={}) or {}
    options = args.json_args if isinstance(args.json_args, dict) else {}
    html = bool(options.get("html", True))
    warnings = [str(item) for item in options.get("warnings", []) or []]

    from tradingagents.reporting import write_report_tree

    report_dir = run.path("reports")
    report_md = write_report_tree(state, request.ticker, report_dir, settings=settings, html=html)
    notes = _run_notes(run, warnings)
    state_log = _write_state_log(request, state, settings)

    recorded = False
    decision = str(state.get("final_trade_decision") or "").strip()
    rating = run_rating(state)
    if decision:
        memory_log(request).store_decision(
            ticker=request.ticker,
            trade_date=request.trade_date,
            final_trade_decision=decision,
            rating=rating,
        )
        recorded = True

    return {
        "ok": True,
        "action": "settle",
        "rating": rating,
        "report_md": str(report_md),
        "report_html": str(report_md.with_suffix(".html")) if html else "",
        "state_log": str(state_log),
        "notes": str(notes),
        "memory_log": memory_config(request)["memory_log_path"],
        "decision_recorded": recorded,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
