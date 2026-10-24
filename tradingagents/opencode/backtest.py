"""Backtest sweep bookkeeping for the OpenCode worker (no LLM, no analysis).

The plugin runs the cells; this module decides which cells still need a run and
summarises the sweep's own memory log, mirroring ``run_backtest``'s layout of
``<results_dir>/backtest/<run_id>/trading_memory.md`` without importing the
LangGraph graph.

A sweep is resumable by construction: the run's memory log is the results table,
so a cell listed there has already run and is skipped. The plugin owns the model
calls and the settlement pass; every action here is a pure file-in, file-out
step, like the rest of the worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.opencode.contract import BadInput
from tradingagents.opencode.run_dir import RunDir

BACKTEST_DIRNAME = "backtest"
LOG_FILENAME = "trading_memory.md"


def sweep_dir(base: str | Path, run_id: str) -> Path:
    """``<base>/backtest/<run_id>``, validated like ``run_backtest``'s own layout.

    ``run_id`` becomes a path segment, so it goes through the same check as a
    ticker: an absolute or dotted value must not place the sweep outside ``base``.
    """
    return Path(base).expanduser() / BACKTEST_DIRNAME / safe_ticker_component(run_id)


def sweep_log_path(base: str | Path, run_id: str) -> Path:
    """The sweep's memory log, at the path ``run_backtest`` would have written."""
    return sweep_dir(base, run_id) / LOG_FILENAME


def _require_base(base: str | Path | None) -> Path:
    """The results root; required, because ``Path("")`` would silently mean cwd."""
    if base is None or not str(base).strip():
        raise BadInput("base is required")
    return Path(base).expanduser()


def _require_run_id(run_id: Any, base: str | Path | None) -> tuple[str, Path, Path]:
    """Validate the sweep identity once and return the paths derived from it."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise BadInput("run_id is required")
    run_id = run_id.strip()
    try:
        target = sweep_dir(_require_base(base), run_id)
    except ValueError as exc:
        # safe_ticker_component's message names the offending value; keep it.
        raise BadInput(str(exc)) from exc
    return run_id, target, target / LOG_FILENAME


def _tickers(value: Any) -> list[str]:
    """The sweep's tickers, in the order given and deduplicated.

    ``run_backtest`` treats a ticker named twice as one cell, so the plan does
    too: two identical cells would otherwise be reported as two runs of the same
    decision.
    """
    if not isinstance(value, (list, tuple)) or not value:
        raise BadInput("tickers must be a non-empty list of ticker strings")
    clean: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BadInput(f"tickers must be non-empty strings, got {item!r}")
        clean.append(item.strip())
    return list(dict.fromkeys(clean))


def _grid_dates(start: str, end: str, every_n_days: int) -> list[str]:
    """The analysis dates, with ``iter_grid`` owning every date rule.

    ``tradingagents.backtest`` is imported here rather than at module import:
    it pulls in the graph and its LLM clients, which ``init`` and ``plan`` must
    not pay for. Its ``ValueError`` is the worker's ``BadInput``.
    """
    from tradingagents.backtest import iter_grid

    try:
        every = int(every_n_days)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"every_n_days must be an integer >= 1, got {every_n_days!r}") from exc
    try:
        return iter_grid(start, end, every)
    except ValueError as exc:
        raise BadInput(str(exc)) from exc


def _log_entries(log_path: Path) -> list[dict[str, Any]]:
    """The sweep's recorded cells; an absent log is an empty one."""
    from tradingagents.memory.log import TradingMemoryLog

    # Rotation is explicitly disabled: a sweep must see every recorded cell, or
    # a resumed plan would re-run work the log has already retained history for.
    return TradingMemoryLog(
        {"memory_log_path": str(log_path), "memory_log_max_entries": None}
    ).load_entries()


def _cell_dicts(cells: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"ticker": ticker, "date": date} for ticker, date in cells]


def plan_payload(
    base: str | Path,
    run_id: str,
    start: str,
    end: str,
    every_n_days: int,
    tickers: list[str],
) -> dict[str, Any]:
    """Which (ticker, date) cells still need a run.

    Order matches ``run_backtest``: ticker-major, dates ascending, duplicates
    collapsed. ``todo`` is what the plugin should run; ``skipped`` is how many
    cells the sweep's log already holds, so a resumed sweep costs nothing for
    work already done.
    """
    run_id, target, log_path = _require_run_id(run_id, base)
    dates = _grid_dates(start, end, every_n_days)
    tickers = _tickers(tickers)

    done = {(entry["ticker"], entry["date"]) for entry in _log_entries(log_path)}
    cells = [(ticker, date) for ticker in tickers for date in dates]
    todo = [cell for cell in cells if cell not in done]

    return {
        "ok": True,
        "action": "backtest-plan",
        "run_id": run_id,
        "sweep_dir": str(target),
        "log_path": str(log_path),
        "dates": dates,
        "cells": _cell_dicts(cells),
        "todo": _cell_dicts(todo),
        "skipped": len(cells) - len(todo),
        "total": len(cells),
    }


def summary_payload(base: str | Path, run_id: str) -> dict[str, Any]:
    """Score the sweep's settled cells, or report that it has not run.

    A sweep that failed before its first cell wrote no log. That is a normal
    state for a freshly planned sweep, so it is reported as empty counts rather
    than raised: the plugin polls this action to decide whether to keep waiting.
    """
    run_id, target, log_path = _require_run_id(run_id, base)
    if not log_path.is_file():
        return {
            "ok": True,
            "action": "backtest-summary",
            "run_id": run_id,
            "sweep_dir": str(target),
            "log_path": str(log_path),
            "resolved": 0,
            "pending": 0,
            "unscored": 0,
            "holding": "",
            "by_rating": {},
            "markdown": f"The sweep has not run yet: no memory log at {log_path}.",
        }

    from tradingagents.backtest import summarize

    summary = summarize(log_path)
    return {
        "ok": True,
        "action": "backtest-summary",
        "run_id": run_id,
        "sweep_dir": str(target),
        "log_path": str(log_path),
        "resolved": summary.resolved,
        "pending": summary.pending,
        "unscored": summary.unscored,
        "holding": summary.holding,
        "by_rating": {
            rating: {
                "count": score.count,
                "hit_rate": score.hit_rate,
                "mean_alpha": score.mean_alpha,
            }
            for rating, score in summary.by_rating.items()
        },
        "markdown": summary.render(),
    }


def _options(args: Any) -> dict[str, Any]:
    """The action's settings, parsed by ``cli.main`` into ``args.json_args``."""
    options = getattr(args, "json_args", None)
    return options if isinstance(options, dict) else {}


def action_backtest_plan(run: RunDir, args) -> dict[str, Any]:
    """``backtest-plan``: the sweep settings arrive through ``args.json_args``."""
    options = _options(args)
    return plan_payload(
        base=options.get("base"),
        run_id=options.get("run_id", ""),
        start=options.get("start", ""),
        end=options.get("end", ""),
        every_n_days=options.get("every_n_days", 1),
        tickers=options.get("tickers"),
    )


def action_backtest_summary(run: RunDir, args) -> dict[str, Any]:
    """``backtest-summary``: score the sweep named in ``args.json_args``."""
    options = _options(args)
    return summary_payload(base=options.get("base"), run_id=options.get("run_id", ""))
