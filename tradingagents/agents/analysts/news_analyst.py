from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_global_news,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_news,
    get_global_news,
    get_macro_indicators,
    get_prediction_markets,
)

_OUTER_WITH_TOOLS = (
    "You are a helpful AI assistant, collaborating with other assistants."
    " Use the provided tools to progress towards answering the question."
    " If you are unable to fully answer, that's OK; another assistant with different tools"
    " will help where you left off. Execute what you can to make progress."
    " Report what your tools support; another agent decides the trade."
    " You have access to the following tools: {tool_names}."
    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
    "{system_message}"
)

_OUTER_WITHOUT_TOOLS = (
    "You are a helpful AI assistant, collaborating with other assistants."
    " You have no tools in this run: the news, macro data, and prediction markets for this"
    " instrument have already been fetched and are included below as a data pack."
    " Treat the pack as those tool calls' complete output; if something you would normally"
    " look up is missing, say it is missing rather than filling the gap from memory."
    " Report what the pack supports; another agent decides the trade."
    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}\n"
    "{system_message}"
)

_TOOL_SENTENCE = (
    "Use the available tools: get_news(start_date, end_date) for news about the {asset_label}"
    " under analysis, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic"
    " news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary"
    " in actual data (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate',"
    " '10y_treasury', 'yield_curve'; for an NSE/BSE listing use 'usd_inr', 'nifty_50',"
    " 'india_vix', 'cpi_india'), and get_prediction_markets(topic, limit) for live"
    " market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026',"
    " geopolitical or sector events)."
)

_NO_TOOL_SENTENCE = (
    "The data pack below holds the {asset_label} news for the analysis window, broader global"
    " and macro news, macro series, and prediction-market probabilities, all collected as of"
    " {current_date}. Cite only what it contains; do not describe these as tool calls you made."
)

_DATA_PACK_HEADER = """# Pre-fetched news data pack

The plugin ran get_news, get_global_news, get_macro_indicators, and
get_prediction_markets before this call. Treat the sections below as those
tools' complete output: you cannot call them again, and you must cite only
figures and headlines that appear in it. If a section is missing or marked
unavailable, say so instead of estimating it.

"""


def _system_message(*, current_date: str, asset_label: str, data_pack: str | None) -> str:
    body = (
        _TOOL_SENTENCE.format(asset_label=asset_label)
        if data_pack is None
        else _NO_TOOL_SENTENCE.format(asset_label=asset_label, current_date=current_date)
    )
    message = (
        "You are a news researcher tasked with analyzing recent news and trends over the past week."
        " Please write a comprehensive report of the current state of the world that is relevant for"
        " trading and macroeconomics. " + body +
        " Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
        " Make sure to append a Markdown table at the end of the report to organize key points in"
        " the report, organized and easy to read."
        + get_language_instruction()
    )
    if data_pack is not None:
        message += "\n\n" + _DATA_PACK_HEADER + data_pack
    return message


def _news_prompt(state, data_pack: str | None = None) -> ChatPromptTemplate:
    current_date = state["trade_date"]
    asset_type = state.get("asset_type", "stock")
    asset_label = "company" if asset_type == "stock" else "asset"
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _OUTER_WITHOUT_TOOLS if data_pack is not None else _OUTER_WITH_TOOLS),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )
    return prompt.partial(
        system_message=_system_message(
            current_date=current_date, asset_label=asset_label, data_pack=data_pack
        ),
        tool_names=", ".join(tool.name for tool in TOOLS),
        current_date=current_date,
        instrument_context=get_instrument_context_from_state(state),
    )


def build_news_analyst_messages(state, data_pack: str | None = None) -> list[BaseMessage]:
    """The exact messages the news analyst is given, for graph and worker alike."""
    return _news_prompt(state, data_pack).format_messages(
        messages=list(state.get("messages") or [])
    )


def create_news_analyst(llm):
    def news_analyst_node(state):
        prompt = _news_prompt(state)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
