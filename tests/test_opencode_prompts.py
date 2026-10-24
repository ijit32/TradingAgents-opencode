"""The worker's market prompt must be the graph's prompt, minus the tool calls.

If the two drift, an OpenCode run and a CLI run stop being comparable, and the
report's numbers become a function of how the run was started.
"""

import pytest

from tradingagents.agents.analysts.market_analyst import (
    build_market_analyst_messages,
)
from tradingagents.agents.managers import (
    portfolio_manager as pm_module,
    research_manager as rm_module,
)
from tradingagents.agents.researchers import (
    bear_researcher,
    bull_researcher,
)
from tradingagents.agents.risk_mgmt import (
    aggressive_debator,
    conservative_debator,
    neutral_debator,
)
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
)
from tradingagents.agents.trader import trader as trader_module
from tradingagents.dataflows.config import run_config

STATE = {
    "company_of_interest": "NVDA",
    "asset_type": "stock",
    "trade_date": "2026-10-23",
    "messages": [],
}

PACK = "# Market data pack — NVDA\n\n## Price history\n\nclose,100\n"


def _messages(data_pack=None):
    with run_config({"output_language": "English"}):
        return build_market_analyst_messages(STATE, data_pack=data_pack)


def _system(messages):
    return messages[0].content


@pytest.mark.unit
def test_graph_prompt_still_asks_for_tools():
    system = _system(_messages())

    assert "You have access to the following tools: get_stock_data, get_indicators" in system
    assert "call get_stock_data first" in system
    assert "call get_verified_market_snapshot" in system
    assert "Pre-fetched market data pack" not in system


@pytest.mark.unit
def test_pack_prompt_drops_tool_calls_and_appends_the_pack():
    system = _system(_messages(PACK))

    assert "You have access to the following tools" not in system
    assert "call get_stock_data first" not in system
    assert "call get_verified_market_snapshot" not in system
    assert "You have no tools in this run" in system
    assert system.rstrip().endswith(PACK.rstrip())


@pytest.mark.unit
def test_prompts_differ_only_in_the_tool_block_and_the_pack():
    graph = _system(_messages())
    packed = _system(_messages(PACK))

    # Everything the analyst is told about the indicators and the report format
    # is shared; only the tool instructions and the appended pack differ.
    shared_guide = graph.split("You are a trading assistant tasked with analyzing")[1].split(
        "When you tool call, please use the exact name"
    )[0]
    assert shared_guide in packed
    report_instructions = graph.split("Before writing the final report,")[1]
    assert report_instructions.split("Make sure to append")[1] in packed

    # Same run context reaches both.
    for message in (_messages(), _messages(PACK)):
        assert "Today's date is 2026-10-23" in _system(message)
        assert "The instrument to analyze is `NVDA`" in _system(message)


@pytest.mark.unit
def test_history_is_preserved_in_both_forms():
    from langchain_core.messages import HumanMessage

    state = {**STATE, "messages": [HumanMessage(content="focus on volume")]}

    with run_config({"output_language": "English"}):
        graph = build_market_analyst_messages(state)
        packed = build_market_analyst_messages(state, data_pack=PACK)

    assert [message.type for message in graph] == ["system", "human"]
    assert [message.type for message in packed] == ["system", "human"]
    assert graph[1].content == packed[1].content == "focus on volume"


@pytest.mark.unit
def test_output_language_reaches_the_pack_prompt():
    with run_config({"output_language": "German"}):
        packed = build_market_analyst_messages(STATE, data_pack=PACK)

    assert "Write your entire response in German" in _system(packed)


# --- Phase 2: the other three analysts -------------------------------------

from tradingagents.agents.analysts import (  # noqa: E402
    fundamentals_analyst,
    news_analyst,
    sentiment_analyst,
)

SENTIMENT_PACK = "# Sentiment data pack — NVDA\n\n## News headlines\n\nNVDA beats\n"
NEWS_PACK = "# News data pack — NVDA\n\n## FRED macro series\n\n### cpi\n\n3.1%\n"
FUNDAMENTALS_PACK = "# Fundamentals data pack — NVDA\n\n## Income statement (quarterly)\n\nrevenue,1\n"

# builder, pack, the pack-form marker, and whether the pack closes the prompt.
# The sentiment brief puts its analysis guide after the sources, so its pack is
# mid-prompt; the tool analysts append the pack last.
PACK_CASES = [
    (
        sentiment_analyst.build_sentiment_analyst_messages,
        SENTIMENT_PACK,
        "you cannot fetch again",
        False,
    ),
    (news_analyst.build_news_analyst_messages, NEWS_PACK, "You have no tools in this run", True),
    (
        fundamentals_analyst.build_fundamentals_analyst_messages,
        FUNDAMENTALS_PACK,
        "You have no tools in this run",
        True,
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("build,pack,marker,closed", PACK_CASES)
def test_every_pack_prompt_is_tool_free_and_carries_the_pack(build, pack, marker, closed):
    with run_config({"output_language": "English"}):
        system = build(STATE, data_pack=pack)[0].content

    assert marker in system
    assert "Use the available tools" not in system
    assert pack.strip() in system
    if closed:
        assert system.rstrip().endswith(pack.rstrip())
    assert "Today's date is 2026-10-23" in system
    assert "The instrument to analyze is `NVDA`" in system


@pytest.mark.unit
@pytest.mark.parametrize("build,pack,marker,closed", PACK_CASES)
def test_output_language_reaches_every_pack_prompt(build, pack, marker, closed):
    with run_config({"output_language": "German"}):
        system = build(STATE, data_pack=pack)[0].content

    assert "Write your entire response in German" in system


@pytest.mark.unit
def test_sentiment_graph_prompt_still_names_its_sources(monkeypatch):
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "st block")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "rd block")
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda *a, **k: None)
    monkeypatch.setattr(sentiment_analyst.get_news, "func", lambda *a, **k: "news block", raising=False)

    with run_config({"output_language": "English"}):
        graph = sentiment_analyst.build_sentiment_analyst_messages(STATE)[0].content
        packed = sentiment_analyst.build_sentiment_analyst_messages(STATE, data_pack=SENTIMENT_PACK)[0].content

    assert "### News headlines — Yahoo Finance" in graph
    assert "<start_of_news>\nnews block\n<end_of_news>" in graph
    assert "st block" in graph and "rd block" in graph
    assert "2026-10-16 → 2026-10-23" in graph
    assert "<start_of_news>" not in packed
    # The analysis guide and the output fields are shared by both forms.
    assert graph.split("## How to analyze this data")[1] in packed


@pytest.mark.unit
def test_news_graph_prompt_keeps_its_tools_and_pack_form_drops_them():
    with run_config({"output_language": "English"}):
        graph = news_analyst.build_news_analyst_messages(STATE)[0].content
        packed = news_analyst.build_news_analyst_messages(STATE, data_pack=NEWS_PACK)[0].content

    assert "You have access to the following tools: get_news, get_global_news" in graph
    assert "Use the available tools: get_news(start_date, end_date)" in graph
    assert "get_prediction_markets(topic, limit)" in graph
    assert "You have no tools in this run" in packed
    assert "Use the available tools" not in packed
    assert "collectively" not in packed  # no made-up tool narrative
    # The report instructions are identical in both forms.
    shared = graph.split("Please write a comprehensive report")[1].split("Use the available tools:")[0]
    assert shared in packed


@pytest.mark.unit
def test_fundamentals_graph_prompt_keeps_its_tools_and_pack_form_drops_them():
    with run_config({"output_language": "English"}):
        graph = fundamentals_analyst.build_fundamentals_analyst_messages(STATE)[0].content
        packed = fundamentals_analyst.build_fundamentals_analyst_messages(
            STATE, data_pack=FUNDAMENTALS_PACK
        )[0].content

    assert "You have access to the following tools: get_fundamentals" in graph
    assert "`get_income_statement`" in graph
    assert "You have no tools in this run" in packed
    assert "Use the available tools" not in packed
    shared = graph.split("Please write a comprehensive report")[1].split(" Use the available tools:")[0]
    assert shared in packed


# --- Phase 3: the decision pyramid -----------------------------------------

INVEST_DEBATE = {
    "bull_history": "\nBull Analyst: bull case",
    "bear_history": "\nBear Analyst: bear case",
    "history": "\nBull Analyst: bull case\nBear Analyst: bear case",
    "current_response": "Bear Analyst: bear case",
    "count": 2,
}

RISK_DEBATE = {
    "aggressive_history": "\nAggressive Analyst: aggressive case",
    "conservative_history": "\nConservative Analyst: conservative case",
    "neutral_history": "\nNeutral Analyst: neutral case",
    "history": (
        "\nAggressive Analyst: aggressive case"
        "\nConservative Analyst: conservative case"
        "\nNeutral Analyst: neutral case"
    ),
    "latest_speaker": "Neutral",
    "current_aggressive_response": "Aggressive Analyst: aggressive case",
    "current_conservative_response": "Conservative Analyst: conservative case",
    "current_neutral_response": "Neutral Analyst: neutral case",
    "count": 3,
}

DECISION_STATE = {
    **STATE,
    "instrument_context": "The instrument to analyze is `NVDA`.",
    "market_report": "market report",
    "sentiment_report": "sentiment report",
    "news_report": "news report",
    "fundamentals_report": "fundamentals report",
    "investment_debate_state": INVEST_DEBATE,
    "risk_debate_state": RISK_DEBATE,
    "investment_plan": "**Recommendation**: Overweight",
    "trader_investment_plan": "**Action**: Buy",
    "past_context": "",
}


class _Recorder:
    """A plain LLM that records the prompt it was given."""

    def __init__(self, captured):
        self.captured = captured

    def invoke(self, prompt, *args, **kwargs):
        self.captured["prompt"] = prompt
        from langchain_core.messages import AIMessage

        return AIMessage(content="answer")


class _StructuredRecorder(_Recorder):
    """A structured LLM that records the prompt and returns a typed instance."""

    def __init__(self, captured, result):
        super().__init__(captured)
        self.result = result

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt, *args, **kwargs):
        self.captured["prompt"] = prompt
        return self.result


@pytest.mark.unit
@pytest.mark.parametrize(
    "build,create",
    [
        (
            bull_researcher.build_bull_researcher_prompt,
            bull_researcher.create_bull_researcher,
        ),
        (
            bear_researcher.build_bear_researcher_prompt,
            bear_researcher.create_bear_researcher,
        ),
        (
            aggressive_debator.build_aggressive_prompt,
            aggressive_debator.create_aggressive_debator,
        ),
        (
            conservative_debator.build_conservative_prompt,
            conservative_debator.create_conservative_debator,
        ),
        (
            neutral_debator.build_neutral_prompt,
            neutral_debator.create_neutral_debator,
        ),
    ],
)
def test_debate_nodes_send_the_builder_prompt_verbatim(build, create):
    captured = {}

    with run_config({"output_language": "English"}):
        expected = build(DECISION_STATE)
        create(_Recorder(captured))(DECISION_STATE)

    assert captured["prompt"] == expected


@pytest.mark.unit
def test_research_manager_node_sends_the_builder_prompt_verbatim():
    captured = {}
    llm = _StructuredRecorder(
        captured,
        ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            rationale="r",
            strategic_actions="a",
        ),
    )

    with run_config({"output_language": "English"}):
        expected = rm_module.build_research_manager_prompt(DECISION_STATE)
        rm_module.create_research_manager(llm)(DECISION_STATE)

    assert captured["prompt"] == expected


@pytest.mark.unit
def test_portfolio_manager_node_sends_the_builder_prompt_verbatim():
    captured = {}
    llm = _StructuredRecorder(
        captured,
        PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="e",
            investment_thesis="t",
        ),
    )

    with run_config({"output_language": "English"}):
        expected = pm_module.build_portfolio_manager_prompt(DECISION_STATE)
        pm_module.create_portfolio_manager(llm)(DECISION_STATE)

    assert captured["prompt"] == expected


@pytest.mark.unit
def test_trader_node_sends_the_builder_messages_verbatim():
    captured = {}
    llm = _StructuredRecorder(
        captured,
        TraderProposal(action=TraderAction.BUY, reasoning="r"),
    )

    with run_config({"output_language": "English"}):
        expected = trader_module.build_trader_messages(DECISION_STATE)
        trader_module.create_trader(llm)(DECISION_STATE)

    assert captured["prompt"] == expected
    assert [message["role"] for message in expected] == ["system", "user"]
