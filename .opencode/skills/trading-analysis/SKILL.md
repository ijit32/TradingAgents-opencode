---
name: trading-analysis
description: >
  Run a point-in-time TradingAgents analysis from inside OpenCode: collect market
  data, call a free OpenCode model for each stage, validate the report, and
  checkpoint the run so it can resume without repeating model calls, settle past
  decisions, write the report tree, and sweep a date grid. Use when the user asks
  to analyze a ticker, run a backtest, read a report, resume a run, or check a
  run's status — or invokes /ta-analyze, /ta-backtest, or /ta-report.
---

# Trading analysis

One instrument, one date, one run directory. The plugin owns the stage order and
the model calls; the Python worker owns data collection, prompt rendering, and
validation. Nothing here fetches data past the analysis date.

## Run it

```text
/ta-analyze NVDA 2026-10-23
/ta-analyze BTC-USD 2026-10-23 --asset-type crypto --depth 2
/ta-analyze NVDA 2026-10-23 --model opencode/glm-5.3-flash-free
/ta-analyze NVDA 2026-10-23 --resume NVDA_2026-10-23_20261024_1152
/ta-analyze NVDA 2026-10-23 --rerun market_analyst
/ta-analyze NVDA 2026-10-23 --no-memory --no-html
/ta-backtest NVDA,AAPL 2026-01-02 2026-03-01 --every 7 --run-id q1
/ta-backtest NVDA 2026-01-02 2026-03-01 --run-id q1     # resume the same sweep
/ta-report                                               # latest run's markdown
/ta-report NVDA_2026-10-22 --html
```

The same runs are available as `trading_analyze` and `trading_backtest` for chat
use. `trading_report` reads a finished run and `trading_status` reads any run,
neither calling a model.

The first run asks for permission to execute the worker command
(`uv run python -m tradingagents.opencode_worker`). A deny stops the run before
any model call; allow-once-with-save makes later runs prompt-free.

| Argument | Meaning |
|---|---|
| `ticker` | Required. `NVDA`, `BTC-USD`, `^GSPC`, `GC=F`. |
| `trade_date` | `YYYY-MM-DD`, never in the future. Defaults to today. |
| `--depth` | `1` (default), `2`, `3`. Sets debate and risk rounds. |
| `--analysts` | `market,social,news,fundamentals`. Defaults to all. |
| `--model` | Override every stage. `provider/model[#variant]`. |
| `--quick-model`, `--deep-model` | Per-tier override. |
| `--asset-type` | `stock` (default) or `crypto`. |
| `--language` | Report language. Defaults to English. |
| `--resume` | Run id or unique prefix. With no value, the newest run for that ticker and date. |
| `--run-dir` | Absolute path of an existing run, inside the plugin's run root. |
| `--rerun` | Force one stage even if its prompt is unchanged. |
| `--rerun-from` | Force that stage and everything after it. |
| `--no-memory` | Skip settlement and decision recording for this run. |
| `--no-html` | Write only `complete_report.md`. |

### Backtest arguments

| Argument | Meaning |
|---|---|
| `tickers` | Required. Comma-separated symbols. |
| `start`, `end` | Required `YYYY-MM-DD` bounds; `end` defaults to today. |
| `--every` | Trading days between analysis dates. Defaults to `7`. |
| `--run-id` | Sweep id. Reuse it to resume; generated when omitted. |
| `--depth`, `--analysts`, `--model`, `--quick-model`, `--deep-model`, `--language` | As for `/ta-analyze`. |
| `--no-html` | Skip the HTML page per cell. |

## What happens

```text
init → plan → (collect → prompt → generate → validate → checkpoint) × stages
```

1. `request.json` is written, then the worker normalizes it (ticker safety, date
   bounds, analyst list, depth).
2. `plan` returns the stage order and groups, so the order lives in the repo's
   own stage manifest rather than in the plugin.
3. `collect` calls the repo's existing dated tools with the run's `trade_date`
   and writes one bounded pack per analyst to `data/<key>.md` (12k chars
   target, 20k cap). Packs are collected one at a time; a vendor failure
   becomes an `<… unavailable>` placeholder and a note in `data/index.json`,
   never a lost pack.
4. `prompt` renders the same messages the LangGraph agent would receive, minus
   the tool-call instructions, plus the pack (or the earlier stages' outputs).
   For the three decision stages it also appends the repo's JSON schema.
5. The plugin generates on a short-lived OpenCode session pinned to the stage's
   model. The four analysts' reports are generated concurrently up to
   `concurrency` (default 4); the debates and decisions run one at a time,
   because each prompt reads the previous turn. This is the only path the free
   tier accepts — see the plugin README.
6. `validate` enforces the stage contract: report length, one debate turn with
   its speaker, or JSON parsed with the repo's pydantic model. A failure
   triggers a repair call — once for prose, twice for a decision — then a
   lenient pass with a `> [review]` marker, and the run records a warning
   instead of losing the output.
7. `checkpoint.json` records the stage's prompt hash, model, attempts, and time.

Memory runs beside step 5, as it does in the graph: `memory-begin` finds pending
decisions whose holding window has traded and renders the graph's reflection
prompt; the plugin generates one reflection per entry; `memory-apply` writes them
back and reads the ticker's lessons into `memory/past_context.md`. The Portfolio
Manager waits for that file. Nothing else does, and a memory failure is a
warning, not a failed run.

After the last stage the worker's `settle` writes `reports/complete_report.md`,
`complete_report.html`, `reports/run_notes.md`, the graph-shaped state log under
the results directory, and this run's decision as a pending memory entry. A
resume rewrites them without duplicating the entry.

## Stages

| Group | Stages | Model tier |
|---|---|---|
| Analysts (parallel) | `market`, `sentiment`, `news`, `fundamentals` | quick |
| Research debate | `bull_researcher_N`, `bear_researcher_N` | quick |
| Research manager | `research_manager` → investment plan, `recommendation` | deep |
| Trader | `trader` → transaction proposal, `action` | quick |
| Risk debate | `aggressive_analyst_N`, `conservative_analyst_N`, `neutral_analyst_N` | quick |
| Portfolio manager | `portfolio_manager` → final decision, `rating` | deep |

`--depth` sets the rounds: `1` = 1 research + 1 risk round (12 stages total),
`2` = 2 + 1, `3` = 2 + 2. The debate transcript keeps the last two turns
verbatim and trims earlier ones with an explicit marker, so more rounds cost
bounded prompt size rather than quadratic growth.

The run's rating is the Portfolio Manager's. A decision whose rating cannot be
read is `REVIEW`, which is not a tradeable position — say so rather than
flattening it to `Hold`.

## Resume rules

A stage is skipped when it succeeded (or was already skipped) **and** its
rendered prompt hash is unchanged. Upstream edits shift downstream hashes, so
only the affected tail reruns; resuming the same run repeatedly stays at 0 model
calls. `--rerun` forces one stage; `--rerun-from` forces a tail. Without
`--resume` or `--run-dir`, every invocation starts a new run directory, even for
the same ticker and date.

## Where the output is

```text
~/.tradingagents/opencode/<ticker>_<date>_<stamp>/
  request.json  run.json  meta.json  checkpoint.json  final_state.json
  data/market.md  data/social.md  data/news.md  data/fundamentals.md
  data/index.json
  stages/<stage>.prompt.md  .out.txt  .md
  stages/research_manager.json  trader.json  portfolio_manager.json
  stages/investment_debate.json  risk_debate.json
  memory/pending.json  reflections.json  past_context.md  note.txt
  reports/1_analysts/…  2_research/…  3_trading/…  4_risk/…  5_portfolio/…
  reports/complete_report.md  complete_report.html  run_notes.md
```

The state log goes to `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/`, and
the decision to `<memory_log_path>` (`~/.tradingagents/memory/trading_memory.md`
by default). `/ta-report` returns the latest run's `complete_report.md`; pass
`--html` for the rendered page.

The pack key `social` is the sentiment analyst's; the stage stays
`sentiment_analyst` and `final_state.json` keeps `sentiment_report`, as in the
graph.

A resumed run with unchanged prompts costs **0 model calls**. On the free
DeepSeek model expect roughly 45–60 s per analyst stage in the common case, but
a four-analyst run can take several minutes: one live run took 413 s with a
single 342 s retry. `skipped` stages cost nothing.

## Reading the result

The summary table lists each stage, its status, model, time, and report size.
`status: skipped` means the checkpoint matched — not a failure. `warnings` lists
vendor gaps, truncation, and lenient acceptances; quote them when you summarize,
because they bound what the report can claim.

Treat the report as research input, not investment advice. Do not invent numbers
the report does not contain, and do not claim tool access the pipeline did not
have.

## Backtests

A sweep is planned by the worker (`iter_grid`, ticker-major) and run as ordinary
analyses. Each cell is isolated: runs under `<sweep>/runs`, reports and state
logs under `<sweep>/`, and decisions in `<sweep>/trading_memory.md`. The sweep's
log is its results table, so rerunning the same `run_id` skips every cell already
logged — a resumed sweep costs only the cells that are still missing. One final
memory pass settles the cells added at the tail, then the worker scores the log
(resolved/pending, hit rate, mean alpha) with `summarize`, the same function the
CLI backtest prints.

A failed cell is recorded, the sweep continues, and the next attempt retries it
in a fresh run directory. Cancellation stops the sweep immediately; settled cells
stay in the log.

## Current limits (Phase 4)

- The full decision pyramid runs: four analysts, bull/bear debate, research
  manager, trader, three-way risk debate, portfolio manager. Requesting an
  analyst subset runs exactly those analysts, but the debate and decision stages
  always run, on whatever reports exist.
- Memory, reports, and backtests are in. A backtest retries a failed cell from
  a fresh run directory, not from its checkpoint; resume the cell's own run dir
  with `/ta-analyze` if you want to keep its completed stages.
- The free tier has a usage allowance. Once it is spent, every model call fails
  with `quota` and the message points at OpenCode Go; a paid or Go model avoids
  the wait but is no longer free.
- A leniently accepted stage is marked `> [review]` at the top of its file and
  listed in `warnings`. Treat its numbers as provisional.
- Social packs for past dates are not point-in-time: StockTwits and Reddit serve
  recent items only. The pack says so, and the sentiment report should lower its
  confidence; do not present those quotes as a historical record.
- A pack that hits the 20k cap is trimmed by trim rank, and each trimmed section
  keeps a `[truncated: N chars]` marker. Mention the marker when the report
  leans on that section.

## When something fails

| Symptom | Meaning | Action |
|---|---|---|
| `freetier` | Generation used a transport the gateway refuses | Keep the session path; run `/ta-probe` |
| `auth` | Missing or invalid provider key | Fix credentials; retry will not help |
| `ratelimit` | Provider throttled | The run backs off; resume later |
| `timeout` | No answer within `callTimeoutMs` | Resume the run; completed stages skip |
| `quota` | The free allowance is spent | Wait for it to reset, or configure a paid/Go model |
| `validation failed` on every repair | Report too short, or the decision JSON does not match the schema | Check `stages/<stage>.out.txt`; the run records a lenient fallback marked `> [review]` |
| `no <analyst> data could be collected` | Every vendor for that pack failed | Check the ticker and vendor keys; see `data/index.json` notes |
| `<… unavailable>` in a pack | One feed failed; the rest of the pack is intact | Note the gap when summarizing; the analyst was told to lower confidence |

`/ta-probe --mode text` is the fastest way to tell "model unavailable" from
"transport regressed": it is expected to fail with `403 FreeTierError`.
