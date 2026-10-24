"""The worker's backtest actions: which cells to run, and what the sweep scored."""

from __future__ import annotations

import types

import pytest

from tradingagents.backtest import summarize
from tradingagents.memory import TradingMemoryLog
from tradingagents.opencode.backtest import (
    action_backtest_plan,
    plan_payload,
    summary_payload,
    sweep_dir,
    sweep_log_path,
)
from tradingagents.opencode.contract import BadInput
from tradingagents.opencode.run_dir import RunDir


@pytest.mark.unit
def test_plan_lists_cells_in_ticker_major_order(tmp_path):
    payload = plan_payload(tmp_path, "sweep1", "2026-01-05", "2026-01-07", 1, ["NVDA", "AAPL"])

    assert payload["ok"] is True
    assert payload["dates"] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert payload["cells"] == [
        {"ticker": "NVDA", "date": "2026-01-05"},
        {"ticker": "NVDA", "date": "2026-01-06"},
        {"ticker": "NVDA", "date": "2026-01-07"},
        {"ticker": "AAPL", "date": "2026-01-05"},
        {"ticker": "AAPL", "date": "2026-01-06"},
        {"ticker": "AAPL", "date": "2026-01-07"},
    ]
    assert payload["skipped"] == 0
    assert payload["total"] == 6
    assert payload["todo"] == payload["cells"]


@pytest.mark.unit
def test_a_cell_already_in_the_sweep_log_is_skipped(tmp_path):
    log = sweep_log_path(tmp_path, "sweep2")
    TradingMemoryLog({"memory_log_path": str(log)}).store_decision(
        "NVDA", "2026-01-06", "Rating: Buy\n\nbuy it"
    )

    payload = plan_payload(tmp_path, "sweep2", "2026-01-05", "2026-01-07", 1, ["NVDA", "AAPL"])

    assert {"ticker": "NVDA", "date": "2026-01-06"} not in payload["todo"]
    assert payload["skipped"] == 1
    assert payload["total"] == 6
    assert len(payload["todo"]) == 5


@pytest.mark.unit
def test_sweep_log_lives_under_backtest_and_rejects_traversal(tmp_path):
    assert sweep_dir(tmp_path, "sweep3") == tmp_path / "backtest" / "sweep3"
    assert sweep_log_path(tmp_path, "sweep3") == (
        tmp_path / "backtest" / "sweep3" / "trading_memory.md"
    )

    with pytest.raises(ValueError):
        sweep_dir(tmp_path, "../escape")
    with pytest.raises(BadInput):
        plan_payload(tmp_path, "../escape", "2026-01-05", "2026-01-07", 1, ["NVDA"])


@pytest.mark.unit
def test_summary_of_a_missing_log_is_empty_not_an_error(tmp_path):
    payload = summary_payload(tmp_path, "never-ran")

    assert payload["ok"] is True
    assert payload["resolved"] == 0
    assert payload["pending"] == 0
    assert payload["unscored"] == 0
    assert payload["holding"] == ""
    assert payload["by_rating"] == {}
    assert payload["markdown"].strip()
    # A missing log is read, never created: summary must not fabricate a sweep.
    assert not sweep_log_path(tmp_path, "never-ran").exists()


@pytest.mark.unit
def test_summary_scores_resolved_and_counts_pending(tmp_path):
    log = sweep_log_path(tmp_path, "sweep5")
    memory = TradingMemoryLog({"memory_log_path": str(log)})
    memory.store_decision("NVDA", "2026-01-05", "Rating: Buy\n\nbuy it")
    assert memory.update_with_outcome(
        "NVDA",
        "2026-01-05",
        raw_return=0.042,
        alpha_return=0.021,
        holding_days=5,
        reflection="It worked.",
        resolution_date="2026-01-12",
    )
    memory.store_decision("AAPL", "2026-01-05", "Rating: Hold\n\nwait")

    payload = summary_payload(tmp_path, "sweep5")

    assert payload["resolved"] == 1
    assert payload["pending"] == 1
    assert payload["unscored"] == 0
    assert payload["holding"] == "5 trading days"
    assert payload["by_rating"]["Buy"]["count"] == 1
    assert payload["by_rating"]["Buy"]["hit_rate"] == 1.0
    assert payload["by_rating"]["Buy"]["mean_alpha"] == pytest.approx(0.021)
    assert payload["markdown"] == summarize(log).render()


@pytest.mark.unit
def test_action_backtest_plan_reads_its_settings_from_json_args(tmp_path):
    run = RunDir(tmp_path)
    args = types.SimpleNamespace(
        json_args={
            "base": str(tmp_path),
            "run_id": "sweep6",
            "start": "2026-01-05",
            "end": "2026-01-07",
            "every_n_days": 2,
            "tickers": ["NVDA"],
        }
    )

    payload = action_backtest_plan(run, args)

    assert payload == plan_payload(tmp_path, "sweep6", "2026-01-05", "2026-01-07", 2, ["NVDA"])
    assert payload["action"] == "backtest-plan"
    assert payload["dates"] == ["2026-01-05", "2026-01-07"]
