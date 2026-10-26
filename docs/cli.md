# CLI and Python API

Reference for running TradingAgents outside OpenCode: the interactive CLI, the Python
package, Docker, and the provider setup each one needs.

The OpenCode plugin is the primary interface in this repository; see
[README.md](../README.md) for `/ta-analyze`, `/ta-backtest`, and `/ta-report`. Everything below
runs the same pipeline through LangGraph instead of the plugin's stage runner.

## Installation

Clone TradingAgents:

```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

TradingAgents needs Python 3.11 or later. Create a virtual environment in any of your
favorite environment managers:

```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.13
source .venv/bin/activate
```

Install the package and its dependencies (`uv pip install .` with uv):

```bash
pip install .
```

## Docker

Alternatively, run with Docker:

```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

After updating the repository, rebuild the image with `docker compose build`.

Results, reports, the memory log and the cache live in the `tradingagents_data` volume.
To keep them in a folder on the host instead, create the folder and point
`TRADINGAGENTS_DATA_DIR` at it, in `.env` or the shell:
`mkdir -p data && TRADINGAGENTS_DATA_DIR=./data docker compose run --rm tradingagents`.

For local models with Ollama:

```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

## Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen (international, dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen (China, dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax (global, api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax (China, api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export OPENCODE_API_KEY=...        # OpenCode Console and OpenCode Go
export MISTRAL_API_KEY=...         # Mistral
export MOONSHOT_API_KEY=...        # Kimi (Moonshot)
export GROQ_API_KEY=...            # Groq
export NVIDIA_API_KEY=...          # NVIDIA NIM
export FRED_API_KEY=...            # FRED macro data (free, optional)
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
export TYPESAFE_API_KEY=...        # Jev social-post screening (optional)
```

### FRED macro data

`FRED_API_KEY` is optional. FRED is the only macro source here that asks for a key, and it
buys three things: the US and other FRED macro aliases, the four India series that FRED
carries (`cpi_india`, `gsec_10y_india`, `iip_india`, `real_gdp_india`, described in
[India (NSE and BSE)](india.md#macro-series)), and the point-in-time pin on both. An India run does not need it for the rupee, India VIX or the Nifty/Sensex
quotes — those come from Yahoo. Without the key the FRED-backed aliases say their coverage
is missing and the run carries on; the currency, VIX and index sections are unaffected.

The key is also what makes a backtest honest. Every FRED request sets `realtime_start` and
`realtime_end` to the analysis date (clamped to FRED's own today), so a historical run reads
the values that had been published by then, not today's revision. There is deliberately no
fallback to the latest revision when the key is absent: that would leak future data into a
past decision, so the series is reported as missing instead.

The key is free and needs no card. Sign in or register at
<https://fred.stlouisfed.org/>, open
<https://fred.stlouisfed.org/docs/api/api_key.html>, request a key, accept the terms and
copy the 32-character value it shows and emails you.

Put it in the `.env` at the repo root (loaded at import; `.env.example` has the commented
line) or export it in the shell:

```bash
FRED_API_KEY=abcdef0123456789abcdef0123456789
```

Docker Compose reads the same `.env`; an OpenCode plugin run inherits the environment of the
OpenCode process, or the repo `.env` when the worker starts there. Keep the key out of git —
`.env` is ignored — and do not share it: FRED rate-limits per key.

To confirm it works, run the source probe; with a key its FRED candidates print the pinned
observation for each series, not only the existence check they print without one:

```bash
uv run python scripts/probe_india_sources.py
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your
credentials.

To run on your ChatGPT Plus or Pro plan instead of an API key, choose "Subscription" in
the CLI's provider menu, or set `llm_provider: "chatgpt"`. Sign in once with
`tradingagents login chatgpt`: it prints an address to open in your browser, which
returns to this machine on `127.0.0.1`. When the browser runs on another machine, or
TradingAgents runs in Docker (`docker compose run --rm tradingagents login chatgpt`),
paste the address the browser ends up on. The sign-in is kept in
`~/.tradingagents/chatgpt-auth.json`, and `tradingagents logout chatgpt` revokes it. Runs
count toward your plan's usage limits, and the plan does not accept `temperature` or
`max_tokens`.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set
`llm_provider: "bedrock"`, configure AWS credentials (environment variables,
`~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model
ID, e.g. `us.anthropic.claude-opus-5-5`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is
`http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`.
Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any
model not listed by default.

For OpenCode, choose "OpenCode (Console)" or "OpenCode Go (subscription)" in the provider
menu, or set `llm_provider: "opencode"` / `"opencode-go"`. Both use `OPENCODE_API_KEY`
from your [OpenCode Console](https://opencode.ai/console). The gateway serves its catalog
over Chat Completions, OpenAI Responses, Anthropic Messages and Google Gemini;
TradingAgents routes each model ID to the matching protocol, so GPT, Claude, Gemini and
the open models all work through one provider. The endpoint is pinned to the gateway
root, and any model ID the catalog serves is accepted — pick "Custom model ID" for the
long tail. The gateway catalog changes frequently; the picker lists a current shortlist.
The `*-free` IDs are not offered here: the Console free tier only answers OpenCode's own
requests, so a client using an API key is refused. Free models are reachable through the
OpenCode plugin in `.opencode/plugins/tradingagents` instead; otherwise use a paid model
or Go.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay),
use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or
`TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or
`http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key
is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires
one.

With `TYPESAFE_API_KEY` set, the Sentiment Analyst screens StockTwits and Reddit posts
with TypeSafe's Jev before reading them. Posts that are not about the company are dropped,
and each source opens with a count of the remaining posts by stance: bullish, bearish,
neutral, or unclear. Without the key, posts pass through unscreened. `jev-latest` moves
with new releases; set `TYPESAFE_DEFAULT_MODEL` to a versioned ID such as `jev-1.13.0` to
hold it fixed across runs. To reach Jev through OpenRouter, put an OpenRouter key in
`TYPESAFE_API_KEY` and set `TYPESAFE_BASE_URL=https://openrouter.ai/api`.

Alternatively, copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

## CLI Usage

Launch the interactive CLI:

```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```

You will see a screen where you can select your desired tickers, analysis date, LLM
provider, research depth, and more. Your previous run's answers come back as the
defaults, so pressing Enter accepts them. The `TRADINGAGENTS_*` variables in `.env` still
skip their step entirely.

To run without questions, for a scheduled job or a script, answer the per-run steps with
flags and the rest with `TRADINGAGENTS_*` variables:

```bash
export TRADINGAGENTS_LLM_PROVIDER=openai TRADINGAGENTS_QUICK_THINK_LLM=gpt-6-luna TRADINGAGENTS_DEEP_THINK_LLM=gpt-6-sol
export TRADINGAGENTS_OUTPUT_LANGUAGE=English TRADINGAGENTS_MAX_DEBATE_ROUNDS=1 TRADINGAGENTS_MAX_RISK_ROUNDS=1
tradingagents --ticker NVDA --date 2026-09-23 --analysts market,news,fundamentals --save --no-show
```

The same command takes an Indian listing with its exchange suffix; nothing else changes:

```bash
tradingagents --ticker RELIANCE.NS --date 2026-09-23 --analysts market,news,fundamentals --save --no-show
```

See [India (NSE and BSE)](india.md) for what the suffix decides.

Each flag skips only its own question. Run without a terminal, a missing answer stops the
run before it starts and names the flag or variable to set.

At start-up the interactive CLI mentions a newer release and notices from Tauric Research.
It checks at most once a day, in the background, and sends nothing about your runs; set
`TRADINGAGENTS_NO_UPDATE_CHECK=1` to turn it off. Runs without a terminal never check.

A saved report also includes `complete_report.html`, the report as one page with its
sections listed beside the text, for reading in a browser, on a phone or in print.
Answering the save question at the prompt also asks about the page and can open it in your
browser; `--no-html` skips it, and so does `ta.save_reports(state, "NVDA", html=False)`
from Python.

## Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed
ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

NSE and BSE listings have their own coverage, filed-as-of rules and known gaps: see
[India (NSE and BSE)](india.md).

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework
supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba
DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China),
OpenRouter, OpenCode (Console and Go), Ollama for local models, Azure OpenAI for
enterprise, and your own ChatGPT plan.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and
initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a
decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
state, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)

# the same report tree the CLI saves, under results_dir/reports
ta.save_reports(state, "NVDA")
```

You can also adjust the default configuration to set your own choice of LLMs, debate
rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, opencode, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-6-sol"    # Model for complex reasoning
config["quick_think_llm"] = "gpt-6-luna"   # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)
```

The quick model serves the analysts, researchers, debaters and trader; the deep model
serves the research and portfolio managers. Each can run on its own provider, for example
the managers on Claude while the rest run on OpenAI:

```python
config["deep_think_provider"] = "anthropic"
config["deep_think_llm"] = "claude-opus-5-5"
```

A tier on its own provider uses that provider's key and default endpoint; set
`quick_think_backend_url` or `deep_think_backend_url` for a local or relay endpoint. The
`TRADINGAGENTS_DEEP_THINK_PROVIDER` and `TRADINGAGENTS_QUICK_THINK_PROVIDER` variables set
them for the CLI, together with the tier's model variable.

See `tradingagents/default_config.py` for all configuration options.

### Fundamentals as filed

US company statements come from SEC EDGAR, which records the date every figure was filed.
A run dated in the past reads the statements exactly as they stood that day: a fiscal year
that has ended but has not been filed yet is not served, and a figure restated later still
reads as first reported. Apple's 2008 total assets were filed as $39.6B and restated to
$36.2B in 2010, so a run dated in between reads $39.6B. EDGAR needs no account or API key.

For an NSE/BSE listing, statements and shareholding are read from NSE under their own
broadcast-date rules, and structured history starts with the April 2025 integrated filings
index. See [India (NSE and BSE)](india.md#fundamentals-as-filed) for what is filed, how
XBRL is handled and where coverage stops.

Other companies' statements come from Yahoo Finance, which dates a statement by the period
it covers rather than by when it was published. A run dated today reads them; a run dated
in the past is told they are withheld, since Yahoo cannot say which figures were public by
then.

Insider trades are dated by when they happened, not when they were filed, so a run dated in
the past is told they are withheld as well.

SEC asks callers to identify themselves and refuses requests that carry no contact address,
so a default one is sent. Set your own so SEC can reach you rather than the project:

```bash
SEC_EDGAR_USER_AGENT="Your Name your@email.com"
```

It covers companies that file with the SEC, including foreign companies listed in the US.
Anything else, such as Hong Kong or A-share listings, falls through to the next vendor in
the chain. EDGAR's machine-readable filings begin in 2009, and a fourth quarter is reported
as unavailable rather than derived, because filers publish it only inside the annual
figure.

### Current holdings

By default the agents do not know what you hold, so their guidance is written for a reader
who applies it to their own position. Pass a portfolio to have the trader, the risk
analysts and the portfolio manager work against your actual book.

```python
from tradingagents.portfolio import PortfolioContext

portfolio = PortfolioContext.model_validate({
    "cash": 25000.0,
    "currency": "USD",
    "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}],
})
_, decision = ta.propagate("NVDA", "2026-09-01", portfolio=portfolio)
```

The CLI takes the same content as a JSON file: `tradingagents --portfolio my_book.json`.

`currency` is an ISO code from a short allowlist — `USD`, `INR`, and the other
currencies the modelled markets quote in; `"inr"` is normalized to `INR`. An unknown label
such as `"dollars"` is rejected before the run starts, when it can still be fixed. The
average price is labelled in the instrument's currency, and when the book's currency differs
from the instrument's, the block names both and says they are not converted.

An empty `positions` list means a flat book, which is different from passing nothing. A
run without a portfolio is never treated as flat.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Memory log

The memory log is always on. Each completed run appends its decision to
`~/.tradingagents/memory/trading_memory.md`. While the analysts of a later run work,
TradingAgents settles every logged decision whose holding period has passed: it fetches
the realised return (raw, and alpha against the instrument's regional benchmark) and
generates a one-paragraph reflection. The Portfolio Manager then reads the most recent
decisions for the same ticker plus recent lessons from other tickers, so each analysis
carries forward what worked and what didn't. If settling fails, the run goes on and its
report says so.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

To settle decisions without running an analysis, for a scheduled job, call
`ta.settle_all_pending()`; it returns the decisions it settled and any it could not.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after
each node so a crashed or interrupted run resumes from the last successful step instead of
starting over. The run view says whether it resumed a saved run or started fresh.
Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db`
(override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all
of them before a run.

```bash
tradingagents --checkpoint           # enable for this run
tradingagents --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
```

## Evaluating decisions over time

One run gives one decision, which cannot tell you whether the system decides well.
`run_backtest` runs the same pipeline over a grid of tickers and dates, writes to a memory
log of its own, and scores the decisions whose holding window has since traded.

```python
from tradingagents.backtest import iter_grid, run_backtest, summarize

dates = iter_grid("2026-06-01", "2026-08-01", every_n_days=7)
result = run_backtest(["NVDA", "AAPL"], dates, config, selected_analysts=["market", "news"])
print(summarize(result).render())
```

From the CLI:

```bash
tradingagents backtest NVDA,AAPL --start 2026-06-01 --end 2026-08-01 --every 7
```

Each cell is scored on realized alpha against the instrument's regional benchmark, grouped
by rating. A decision is entered at the first close after its date, because a run reads
news published after that day's close. Your own memory log is never written to, and
re-running the same grid with `run_id=result.run_id` skips the cells that already ran, so
an interrupted sweep continues where it stopped.

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is
expected for a research tool built on language models, not a defect. The variation comes
from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do
not guarantee byte-identical output across calls, and reasoning models (the default GPT-6
family, and any thinking-mode model) vary the most because their internal reasoning is
itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a
run today sees different inputs than a run last week even for the same historical trade
date. Pin the analysis date to hold the price and indicator window fixed, but the social
and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your
config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it
more repeatable. The current curated models are reasoning-first and largely ignore
temperature, so for tighter reproducibility name a non-reasoning model in your config, or
in `TRADINGAGENTS_DEEP_THINK_LLM` and `TRADINGAGENTS_QUICK_THINK_LLM`. Any model ID your
provider serves is accepted, whether or not the picker lists it.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, name a
# non-reasoning model in deep_think_llm / quick_think_llm.
```

What does not vary anymore: the analyzed company identity is resolved deterministically
from the ticker before any agent runs, and the market analyst grounds exact price and
indicator claims in a verified data snapshot. Earlier reports of "different companies" or
fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the
model, the temperature, the date range, data quality, and the sampling above. Treat the
framework as a research scaffold for studying multi-agent analysis, not as a strategy with
a fixed, replicable return.
