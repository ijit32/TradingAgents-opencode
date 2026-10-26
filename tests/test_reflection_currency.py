"""A settled decision's reflection names the currency its alpha is quoted in.

The labelled figures are local-currency percentage returns. Without a currency
line, "Alpha vs ^NSEI: +2.1%" reads downstream as a USD return, and an Indian
decision is graded against a benchmark it was never measured in.
"""

import pytest

from tradingagents.dataflows.markets import report_currency
from tradingagents.memory import settlement
from tradingagents.memory.reflection import Reflector, build_reflection_prompt

pytestmark = pytest.mark.unit

DECISION = "**Rating**: Buy\n\nThe thesis was demand."


def _human(**kwargs) -> str:
    messages = build_reflection_prompt(DECISION, 0.042, 0.021, **kwargs)
    return next(content for role, content in messages if role == "human")


def test_a_us_prompt_is_untouched_by_the_default_arguments():
    """The US prompt is snapshotted by other tests; it must stay byte-identical."""
    expected = (
        "Raw return over 5 trading days: +4.2%\n"
        "Alpha vs SPY: +2.1%\n\n"
        f"Final Decision:\n{DECISION}"
    )

    assert build_reflection_prompt(DECISION, 0.042, 0.021)[1][1] == expected
    # Explicit USD inputs take the same path as the defaults.
    assert _human(instrument_currency="USD", benchmark_currency="USD") == expected
    assert "USD" not in expected and "(USD" not in expected


def test_an_indian_alpha_names_the_rupee_on_both_sides():
    human = _human(
        benchmark_name="^NSEI",
        instrument_currency=report_currency("RELIANCE.NS"),
        benchmark_currency=report_currency("^NSEI"),
    )

    assert "Alpha vs ^NSEI: +2.1%" in human
    assert "Raw return over 5 trading days: +4.2% (INR)" in human
    assert "Benchmark ^NSEI is quoted in INR" in human
    assert "not converted" in human
    assert "USD" not in human


def test_a_tokyo_listing_states_yen_and_never_claims_dollars():
    """``.T`` is not a market this package maps to a currency, so the code is
    supplied by the caller; once known, the prompt names it and never USD."""
    assert report_currency("7203.T") == ""
    human = _human(
        benchmark_name="^N225",
        instrument_currency="JPY",
        benchmark_currency="JPY",
    )

    assert "Alpha vs ^N225: +2.1%" in human
    assert "JPY" in human
    assert "USD" not in human


def test_differing_currencies_are_named_as_different_and_not_converted():
    human = _human(
        benchmark_name="SPY",
        instrument_currency="INR",
        benchmark_currency="USD",
    )

    assert "Alpha vs SPY: +2.1%" in human
    assert "quoted in INR" in human and "quoted in USD" in human
    assert "different currencies" in human
    assert "not converted" in human


def test_an_unverified_benchmark_currency_is_said_to_be_unverified():
    """An empty code is not USD: the disclosure says so instead of guessing."""
    human = _human(
        benchmark_name="CUSTOM.MI",
        instrument_currency="INR",
        benchmark_currency="",
    )

    assert "Alpha vs CUSTOM.MI: +2.1%" in human
    assert "was not verified" in human
    assert "USD" not in human


def test_the_reflector_threads_both_currencies_to_the_model():
    class _LLM:
        def __init__(self):
            self.messages = None

        def invoke(self, messages):
            self.messages = messages
            return type("Response", (), {"content": "lesson"})()

    llm = _LLM()
    Reflector(llm).reflect_on_final_decision(
        final_decision=DECISION,
        raw_return=0.042,
        alpha_return=0.021,
        benchmark_name="^NSEI",
        instrument_currency="INR",
        benchmark_currency="INR",
    )

    human = next(content for role, content in llm.messages if role == "human")
    assert "Alpha vs ^NSEI: +2.1%" in human
    assert "INR" in human


class _FakeLog:
    """The slice of the log settle_pending reads and writes, without a file."""

    def __init__(self, entries):
        self._entries = entries
        self.updates = []

    def settling(self, wait=True):
        return _Held()

    def get_pending_entries(self):
        return list(self._entries)

    def update_with_outcome(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return True


class _Held:
    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


class _FakeReflector:
    def __init__(self):
        self.calls = []

    def reflect_on_final_decision(self, **kwargs):
        self.calls.append(kwargs)
        return "lesson"


def test_settlement_passes_the_market_currencies_to_the_reflector(monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG

    log = _FakeLog(
        [{"ticker": "RELIANCE.NS", "date": "2026-10-01", "decision": DECISION}]
    )
    reflector = _FakeReflector()
    monkeypatch.setattr(
        settlement, "fetch_returns", lambda *a, **k: (0.042, 0.021, 5, "2026-10-08")
    )

    config = {"benchmark_map": DEFAULT_CONFIG["benchmark_map"]}
    settlement.settle_pending("RELIANCE.NS", log, reflector, config)

    assert len(reflector.calls) == 1
    assert reflector.calls[0]["instrument_currency"] == "INR"
    assert reflector.calls[0]["benchmark_currency"] == "INR"
    assert reflector.calls[0]["benchmark_name"] == "^NSEI"
    assert log.updates


def test_the_worker_memory_prompt_and_pending_entry_carry_the_currency(
    tmp_path, capsys, monkeypatch
):
    """The plugin path renders the same prompt, and pending.json keeps the code
    next to the benchmark so a resumed run can tell them apart."""
    import json

    from tests.test_opencode_worker import (
        _memory_log,
        _pending_decision,
        _run,
        _write_request,
    )
    from tradingagents.opencode import memory as memory_module

    log_path = tmp_path / "trading_memory.md"
    _pending_decision(_memory_log(log_path), ticker="RELIANCE.NS")
    root = tmp_path / "run"
    root.mkdir()
    _write_request(
        root,
        ticker="RELIANCE.NS",
        memory_log_path=str(log_path),
        results_dir=str(tmp_path / "logs"),
    )
    assert _run(capsys, "init", "--run-dir", str(root))[0] == 0
    monkeypatch.setattr(
        memory_module, "fetch_returns", lambda *a, **k: (0.042, 0.021, 5, "2026-10-08")
    )

    code, out = _run(capsys, "memory-begin", "--run-dir", str(root))

    assert code == 0
    entry = out["entries"][0]
    assert entry["benchmark"] == "^NSEI"
    assert entry["benchmark_currency"] == "INR"
    assert "Alpha vs ^NSEI: +2.1%" in entry["user"]
    assert "INR" in entry["user"]
    assert "USD" not in entry["user"]

    pending = json.loads((root / "memory" / "pending.json").read_text(encoding="utf-8"))
    saved = pending["entries"][0]
    assert saved["benchmark"] == "^NSEI"
    assert saved["benchmark_currency"] == "INR"
