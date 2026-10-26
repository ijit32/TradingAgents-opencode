"""Sentiment analyst: one sentiment report from three sources.

The node fetches its sources before calling the model and puts them in the
prompt, so the model reports on data it was given rather than inventing posts:

  1. News headlines: Yahoo Finance, or Google News plus exchange filings for NSE/BSE
  2. StockTwits messages: the cashtag stream, with Bullish/Bearish tags (not
     applicable to NSE/BSE listings, which StockTwits does not cover)
  3. Reddit posts: r/wallstreetbets, r/stocks, r/investing, India's investing
     communities for an NSE/BSE listing, or crypto communities for a crypto pair

Each source is trimmed to the analysis window. With a TypeSafe key, the social
posts are screened by Jev first (see post_screen). These feeds serve recent items
and are not archived, so a historical run's sentiment inputs are not
point-in-time.

The report is a SentimentReport through structured output where the provider
supports it and free text otherwise, so the band, score and confidence header
reads the same across providers.

``build_sentiment_analyst_messages`` exposes the exact messages the node sends,
optionally with a pre-collected data pack instead of the live fetch, so the
OpenCode worker and the graph produce the same prompt.
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.post_screen import jev_screen
from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.agents.tools import get_news
from tradingagents.dataflows import markets
from tradingagents.dataflows.vendors.reddit import (
    CRYPTO_SUBREDDITS,
    DEFAULT_SUBREDDITS,
    INDIA_SUBREDDITS,
    fetch_reddit_posts,
    subreddits_for,
)
from tradingagents.dataflows.vendors.stocktwits import (
    INDIA_NOT_APPLICABLE,
    fetch_stocktwits_messages,
)


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


_OUTER = (
    "You are a helpful AI assistant, collaborating with other assistants."
    " Report what your tools support; another agent decides the trade."
    # No tool-calling here: the data is pre-fetched into the prompt, so
    # tool-range wording would only invite a hallucinated tool call (#1130).
    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
    " " + NO_EXTERNAL_TOOLS +
    "\n{system_message}"
)

_DATA_PACK_HEADER = """## Data sources (pre-fetched, in this prompt)

The plugin collected news headlines, StockTwits messages, and Reddit posts for
this instrument and window before calling you. Treat the pack below as those
fetches' complete output: you cannot fetch again, and you must cite only posts
and headlines that appear in it. If a source is missing or marked unavailable,
say so in the narrative and lower your confidence.

"""


def _source_blocks(
    *,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
    subreddits: tuple[str, ...],
    india: bool,
) -> str:
    """The live-fetch form: three labelled sources, as the graph has always sent them."""
    if subreddits == DEFAULT_SUBREDDITS:
        character = "r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term"
    elif subreddits == INDIA_SUBREDDITS:
        character = (
            "r/IndiaInvestments and r/IndianStockMarket are longer-form; "
            "r/DalalStreetBets and r/IndianStreetBets are higher-noise retail chatter"
        )
    elif len(subreddits) > len(CRYPTO_SUBREDDITS):
        character = f"r/{subreddits[0]} leans toward the coin's holders; r/CryptoCurrency and r/CryptoMarkets are broader"
    else:
        character = "r/CryptoCurrency and r/CryptoMarkets are broad crypto communities"
    if india:
        news_heading = (
            f"### News headlines — Google News, GDELT and Indian publishers, past 7 days "
            f"({start_date} → {end_date})"
        )
        stocktwits_heading = "### StockTwits — not applicable to NSE/BSE listings"
        stocktwits_note = (
            "StockTwits keys its streams by US cashtags and does not list NSE/BSE "
            "instruments, so there is no cashtag stream to read. This is missing "
            "coverage, not neutral sentiment."
        )
        reddit_note = (
            " Indian retail discussion on these communities is thin, so an empty "
            "section is not evidence of indifference."
        )
    else:
        news_heading = (
            f"### News headlines — Yahoo Finance, past 7 days ({start_date} → {end_date})"
        )
        stocktwits_heading = (
            "### StockTwits messages — retail-trader social platform indexed by cashtag"
        )
        stocktwits_note = (
            "Fast-moving signal. Each message carries a user-labeled sentiment tag "
            "(Bullish / Bearish / no-label) plus the message body."
        )
        reddit_note = ""
    return f"""## Data sources (pre-fetched, in this prompt)

{news_heading}
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

{stocktwits_heading}
{stocktwits_note}

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — {", ".join(f"r/{sub}" for sub in subreddits)} (past 7 days)
Community discussion, without vote or comment counts. Subreddit character matters ({character}).{reddit_note}

<start_of_reddit>
{reddit_block}
<end_of_reddit>"""


_ANALYSIS = """## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone. A block headed "Screened by Jev" has had off-topic posts removed; its stance count is a classifier's read of every on-topic post fetched, labelled or not, of which the posts listed are a sample. Read it alongside the user tags.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Read Reddit posts for substance.** The feed carries no vote or comment counts, so judge a post by its body excerpt, not its title alone, and do not infer engagement.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

"""


def _system_message(*, ticker: str, start_date: str, end_date: str, sources: str) -> str:
    """The analyst brief, with whatever source section the caller supplied."""
    return (
        f"You are a financial market sentiment analyst. Your task is to produce a comprehensive "
        f"sentiment report for {ticker} covering the period from {start_date} to {end_date}, "
        "drawing on three complementary data sources that have already been collected for you.\n\n"
        f"{sources}\n\n{_ANALYSIS}{get_language_instruction()}"
    )


def _sentiment_prompt(state, data_pack: str | None = None) -> ChatPromptTemplate:
    """The prompt the sentiment analyst runs on, in live-fetch or pack form."""
    ticker = state["company_of_interest"]
    end_date = state["trade_date"]
    start_date = _seven_days_back(end_date)
    instrument_context = get_instrument_context_from_state(state)
    subreddits = subreddits_for(ticker)
    india = markets.is_india(ticker)

    if data_pack is None:
        news_block = get_news.func(ticker, start_date, end_date)
        # Pass the analysis window so a historical run trims social posts to it
        # instead of leaking today's chatter into a backtest (#1220).
        screen = jev_screen(ticker)
        if india:
            # No cashtag stream exists for an NSE/BSE listing; asking would spend
            # a request on a 404, and the 404 would read as silence.
            stocktwits_block = INDIA_NOT_APPLICABLE
        else:
            stocktwits_block = fetch_stocktwits_messages(
                ticker, limit=30, start_date=start_date, end_date=end_date, screen=screen
            )
        reddit_block = fetch_reddit_posts(
            ticker, subreddits, start_date=start_date, end_date=end_date, screen=screen
        )
        sources = _source_blocks(
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
            subreddits=subreddits,
            india=india,
        )
    else:
        sources = _DATA_PACK_HEADER + data_pack

    prompt = ChatPromptTemplate.from_messages([("system", _OUTER), MessagesPlaceholder(variable_name="messages")])
    return prompt.partial(
        system_message=_system_message(
            ticker=ticker, start_date=start_date, end_date=end_date, sources=sources
        ),
        current_date=end_date,
        instrument_context=instrument_context,
    )


def build_sentiment_analyst_messages(state, data_pack: str | None = None) -> list[BaseMessage]:
    """The exact messages the sentiment analyst is given, for graph and worker alike.

    With ``data_pack=None`` this fetches the three sources exactly as the node
    always has; with a pack, it sends the same brief and the collected blocks.
    """
    return _sentiment_prompt(state, data_pack).format_messages(
        messages=list(state.get("messages") or [])
    )


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = _sentiment_prompt(state).format_messages(
            messages=state["messages"]
        )

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node
