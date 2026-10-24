"""Assemble the graph state a downstream stage reads, from the run directory.

The analysts' reports, the debate turns, and the earlier decisions are files in
the run, not a LangGraph state object. This module rebuilds the same dict the
graph would hand each agent, including the per-speaker histories and the
``current_response`` fields the debate prompts read.

Debate turns are stored as an ordered list keyed by stage, so a rerun of an
earlier turn truncates the tail instead of leaving a transcript from the
previous attempt. Long transcripts are compacted deterministically: the last
two turns stay verbatim and earlier ones lose their tail, with a marker saying
how much was dropped. No extra model call is involved.
"""

from __future__ import annotations

import json
from typing import Any

from tradingagents.opencode.contract import AnalysisRequest
from tradingagents.opencode.run_dir import RunDir
from tradingagents.opencode.stages import ANALYST_REPORTS, StageDef

# The last this many turns stay verbatim; earlier ones are trimmed.
COMPACT_KEEP_TURNS = 2
COMPACT_TURN_CHARS = 800

DEBATE_FILES = {
    "investment": "investment_debate.json",
    "risk": "risk_debate.json",
}


def _empty_investment_debate() -> dict[str, Any]:
    return {
        "bull_history": "",
        "bear_history": "",
        "history": "",
        "current_response": "",
        "count": 0,
    }


def _empty_risk_debate() -> dict[str, Any]:
    return {
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "count": 0,
    }


def read_turns(run: RunDir, debate: str) -> list[dict[str, str]]:
    """The debate's turns so far, or an empty list."""
    payload = run.read_json("stages", DEBATE_FILES[debate], default={}) or {}
    turns = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(turns, list):
        return []
    return [
        {
            "stage": str(turn.get("stage", "")),
            "speaker": str(turn.get("speaker", "")),
            "argument": str(turn.get("argument", "")),
        }
        for turn in turns
        if isinstance(turn, dict)
    ]


def _compact(argument: str, *, earlier: bool) -> str:
    if not earlier or len(argument) <= COMPACT_TURN_CHARS:
        return argument
    omitted = len(argument) - COMPACT_TURN_CHARS
    return (
        argument[:COMPACT_TURN_CHARS]
        + f"\n[… {omitted} chars of this earlier turn omitted for length …]"
    )


def _transcript(turns: list[dict[str, str]]) -> str:
    """The graph's ``history`` string, with old turns compacted.

    The graph accumulates ``history + "\\n" + argument`` from an empty string, so
    a short transcript is byte-identical here.
    """
    last = len(turns) - 1
    return "".join(
        "\n" + _compact(turn["argument"], earlier=index <= last - COMPACT_KEEP_TURNS)
        for index, turn in enumerate(turns)
    )


def _speaker_history(turns: list[dict[str, str]], speaker: str) -> str:
    return "".join("\n" + turn["argument"] for turn in turns if turn["speaker"] == speaker)


def investment_debate_state(turns: list[dict[str, str]]) -> dict[str, Any]:
    state = _empty_investment_debate()
    if not turns:
        return state
    return {
        **state,
        "bull_history": _speaker_history(turns, "Bull Analyst"),
        "bear_history": _speaker_history(turns, "Bear Analyst"),
        "history": _transcript(turns),
        "current_response": turns[-1]["argument"],
        "count": len(turns),
    }


def risk_debate_state(turns: list[dict[str, str]]) -> dict[str, Any]:
    state = _empty_risk_debate()
    if not turns:
        return state

    def latest(speaker: str) -> str:
        return next(
            (turn["argument"] for turn in reversed(turns) if turn["speaker"] == speaker),
            "",
        )

    return {
        **state,
        "aggressive_history": _speaker_history(turns, "Aggressive Analyst"),
        "conservative_history": _speaker_history(turns, "Conservative Analyst"),
        "neutral_history": _speaker_history(turns, "Neutral Analyst"),
        "history": _transcript(turns),
        "latest_speaker": turns[-1]["speaker"].split(" ")[0],
        "current_aggressive_response": latest("Aggressive Analyst"),
        "current_conservative_response": latest("Conservative Analyst"),
        "current_neutral_response": latest("Neutral Analyst"),
        "count": len(turns),
    }


def save_argument(run: RunDir, spec: StageDef, argument: str) -> None:
    """Record one debate turn at its position, dropping any later turns.

    A rerun of turn *n* must not leave *n+1* from the previous attempt in the
    transcript the next prompt reads.
    """
    turns = read_turns(run, spec.debate)
    entry = {"stage": spec.stage, "speaker": spec.speaker, "argument": argument}
    turns = turns[: spec.index] + [entry]
    run.write_json({"turns": turns}, "stages", DEBATE_FILES[spec.debate])


def portfolio_context(request: AnalysisRequest) -> str:
    """The portfolio block, rendered like ``PortfolioContext.render`` when possible."""
    raw = request.portfolio.strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "Portfolio context:\n" + raw
    try:
        from tradingagents.portfolio import PortfolioContext

        return PortfolioContext.model_validate(payload).render(request.ticker)
    except Exception:  # noqa: BLE001 — prose that happens to be JSON is still prose
        return "Portfolio context:\n" + raw


def load_state(run: RunDir, request: AnalysisRequest, spec: StageDef) -> dict[str, Any]:
    """The state the stage's prompt builder expects, read from the run's files."""
    meta = run.read_json("meta.json", default={}) or {}
    if not isinstance(meta, dict):
        meta = {}

    state: dict[str, Any] = {
        "messages": [],
        "company_of_interest": request.ticker,
        "asset_type": request.asset_type,
        "trade_date": request.trade_date,
        "instrument_context": str(meta.get("instrument_context") or ""),
        "portfolio_context": portfolio_context(request),
        # Settled beside the analysts, exactly as the graph's memory step does;
        # a run with memory disabled leaves both empty.
        "past_context": run.read_text("memory", "past_context.md") or "",
        "memory_note": run.read_text("memory", "note.txt") or "",
        "market_report": "",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "investment_plan": run.read_text("stages", "research_manager.md") or "",
        "trader_investment_plan": run.read_text("stages", "trader.md") or "",
        "investment_debate_state": _empty_investment_debate(),
        "risk_debate_state": _empty_risk_debate(),
    }
    for report, stage in ANALYST_REPORTS.items():
        state[report] = run.read_text("stages", f"{stage}.md") or ""

    if spec.debate:
        # A debate turn reads the transcript up to its own position only.
        turns = read_turns(run, spec.debate)[: spec.index]
        if spec.debate == "investment":
            state["investment_debate_state"] = investment_debate_state(turns)
        else:
            state["risk_debate_state"] = risk_debate_state(turns)
    else:
        # The managers read both debates in full.
        state["investment_debate_state"] = investment_debate_state(
            read_turns(run, "investment")
        )
        state["risk_debate_state"] = risk_debate_state(read_turns(run, "risk"))
    return state


def debate_histories(run: RunDir) -> dict[str, Any]:
    """Both debates in ``final_state.json`` shape, for the settle step and reports."""
    investment = investment_debate_state(read_turns(run, "investment"))
    risk = risk_debate_state(read_turns(run, "risk"))
    return {
        "investment_debate_state": {
            key: investment[key]
            for key in ("bull_history", "bear_history", "history", "current_response")
        },
        "risk_debate_state": {
            key: risk[key]
            for key in (
                "aggressive_history",
                "conservative_history",
                "neutral_history",
                "history",
            )
        },
    }
