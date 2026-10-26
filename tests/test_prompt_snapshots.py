"""Golden snapshots for all twelve stage prompts.

A run's prompts are the contract between the graph and the OpenCode worker: the
worker must hand the model the same words the graph would, so a change to one
template shows up here as a one-line diff instead of as a silent drift between
CLI runs and plugin runs.

The state below is synthetic and self-contained — no run directory, no vendor
call, no cached company profile — so a failure here is always a prompt change,
never an environment difference. Regenerate after an intended prompt edit with:

    UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_prompt_snapshots.py

and review the new digests as part of that change.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.analysts.fundamentals_analyst import (
    build_fundamentals_analyst_messages,
)
from tradingagents.agents.analysts.market_analyst import build_market_analyst_messages
from tradingagents.agents.analysts.news_analyst import build_news_analyst_messages
from tradingagents.agents.analysts.sentiment_analyst import (
    build_sentiment_analyst_messages,
)
from tradingagents.agents.managers.portfolio_manager import (
    build_portfolio_manager_prompt,
)
from tradingagents.agents.managers.research_manager import build_research_manager_prompt
from tradingagents.agents.researchers.bear_researcher import build_bear_researcher_prompt
from tradingagents.agents.researchers.bull_researcher import build_bull_researcher_prompt
from tradingagents.agents.risk_mgmt.aggressive_debator import build_aggressive_prompt
from tradingagents.agents.risk_mgmt.conservative_debator import (
    build_conservative_prompt,
)
from tradingagents.agents.risk_mgmt.neutral_debator import build_neutral_prompt
from tradingagents.agents.trader.trader import build_trader_messages
from tradingagents.dataflows.config import run_config

ANALYST_STATE = {
    "company_of_interest": "NVDA",
    "asset_type": "stock",
    "trade_date": "2026-10-23",
    "instrument_context": "The instrument to analyze is `NVDA` (NVIDIA Corporation).",
    "messages": [],
}

PACK = "# Data pack — NVDA\n\nThe analysis date is 2026-10-23.\n\nclose,210.96\n"

INVESTMENT_DEBATE = {
    "bull_history": "\nBull Analyst: margins are expanding.",
    "bear_history": "\nBear Analyst: the multiple already prices that in.",
    "history": (
        "\nBull Analyst: margins are expanding."
        "\nBear Analyst: the multiple already prices that in."
    ),
    "current_response": "Bear Analyst: the multiple already prices that in.",
    "count": 2,
}

RISK_DEBATE = {
    "aggressive_history": "\nAggressive Analyst: press the size.",
    "conservative_history": "\nConservative Analyst: halve it.",
    "neutral_history": "\nNeutral Analyst: ladder it.",
    "history": (
        "\nAggressive Analyst: press the size."
        "\nConservative Analyst: halve it."
        "\nNeutral Analyst: ladder it."
    ),
    "latest_speaker": "Neutral",
    "current_aggressive_response": "Aggressive Analyst: press the size.",
    "current_conservative_response": "Conservative Analyst: halve it.",
    "current_neutral_response": "Neutral Analyst: ladder it.",
    "count": 3,
}

DECISION_STATE = {
    **ANALYST_STATE,
    "market_report": "Market report: 210.96, below the 50 SMA.",
    "sentiment_report": "Sentiment report: mixed, one source online.",
    "news_report": "News report: no company items in the window.",
    "fundamentals_report": "Fundamentals report: 96,221M revenue TTM.",
    "investment_debate_state": INVESTMENT_DEBATE,
    "risk_debate_state": RISK_DEBATE,
    "investment_plan": "**Recommendation**: Overweight",
    "trader_investment_plan": "**Action**: Buy",
    "past_context": "",
    "messages": [],
}

STAGES = {
    "market_analyst": lambda: build_market_analyst_messages(ANALYST_STATE, data_pack=PACK),
    "sentiment_analyst": lambda: build_sentiment_analyst_messages(
        ANALYST_STATE, data_pack=PACK
    ),
    "news_analyst": lambda: build_news_analyst_messages(ANALYST_STATE, data_pack=PACK),
    "fundamentals_analyst": lambda: build_fundamentals_analyst_messages(
        ANALYST_STATE, data_pack=PACK
    ),
    "bull_researcher_1": lambda: build_bull_researcher_prompt(DECISION_STATE),
    "bear_researcher_1": lambda: build_bear_researcher_prompt(DECISION_STATE),
    "research_manager": lambda: build_research_manager_prompt(DECISION_STATE),
    "trader": lambda: build_trader_messages(DECISION_STATE),
    "aggressive_analyst_1": lambda: build_aggressive_prompt(DECISION_STATE),
    "conservative_analyst_1": lambda: build_conservative_prompt(DECISION_STATE),
    "neutral_analyst_1": lambda: build_neutral_prompt(DECISION_STATE),
    "portfolio_manager": lambda: build_portfolio_manager_prompt(DECISION_STATE),
}

GOLDEN = {
    "market_analyst": "dc1d3eabefc97ce1948cd1b22da4b6d8c269e82fc6cb609531a2d964559a5a25",
    "sentiment_analyst": "9ed42895817c4e1fe008a0e019298f7d353755073b113689a0960104d8a16ae3",
    "news_analyst": "55fd361b4df8deed3cda939c5f37059d4bbae0b7e06a733c1bb9d92abddafeb9",
    "fundamentals_analyst": "92664c85ac82a3a8756455fb433c1410e5e8fd8669206c65b6b255cb69758e0d",
    "bull_researcher_1": "205402ddce1203980bf53f31cb6e55bd030d971d87ff3475ee2a6f74e398740d",
    "bear_researcher_1": "f10f4dba79a49697caedc03edf4379db34791dd76c0ba2e86a402728f643377f",
    "research_manager": "4ddc550ef4b8685936afc9ba43da19631a4dc3668751a39892c46096d8f37500",
    "trader": "f7c0956e0376b727f3658da22864d5d9d0d56dcb99d8110f473042c9b9a5dae6",
    "aggressive_analyst_1": "6ef685d1283a739e293013017bfeb0d62201cb4f2a4e20fb2ee47c628fc44d6d",
    "conservative_analyst_1": "e9ddedf2bda9197a0cfb0eee048a50a0806d2042e87e467224c2c22ce964542f",
    "neutral_analyst_1": "d4f779b7ac96784626222ab33edcd818f83ab9a9e03203c2fbe7513086484393",
    "portfolio_manager": "0b2b811a7853870efe21ebc1f0070530f217d7b7af7cad5f9afabb652beb835e",
}


def _render(built) -> str:
    """The prompt as the worker flattens it: one string, roles labelled."""
    if isinstance(built, str):
        return f"### user\n\n{built.strip()}\n"
    blocks = []
    for message in built:
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
        elif isinstance(message, (SystemMessage, HumanMessage)):
            role = "system" if isinstance(message, SystemMessage) else "user"
            content = message.content if isinstance(message.content, str) else str(message.content)
        else:
            role = getattr(message, "type", "user")
            raw = getattr(message, "content", "")
            content = raw if isinstance(raw, str) else str(raw)
        blocks.append(f"### {role}\n\n{content.strip()}")
    return "\n\n".join(blocks) + "\n"


def digest(built) -> str:
    return hashlib.sha256(_render(built).encode("utf-8")).hexdigest()


@pytest.mark.unit
@pytest.mark.parametrize("stage", sorted(STAGES))
def test_stage_prompt_matches_its_snapshot(stage):
    with run_config({"output_language": "English"}):
        actual = digest(STAGES[stage]())

    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS"):
        print(f'    "{stage}": "{actual}",')
        return

    assert actual == GOLDEN[stage], (
        f"the {stage} prompt changed; review it, then regenerate the snapshot with "
        f"UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/test_prompt_snapshots.py"
    )


@pytest.mark.unit
def test_every_plan_stage_has_a_snapshot():
    """The plan and this file must cover the same twelve stages."""
    from tradingagents.opencode.contract import AnalysisRequest
    from tradingagents.opencode.stages import stage_order

    request = AnalysisRequest.from_mapping(
        {"ticker": "NVDA", "trade_date": "2026-10-23", "depth": 1}
    )

    assert set(stage_order(request)) == set(STAGES)
