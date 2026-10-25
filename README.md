<p align="center">
  <img src="tradingagents/assets/tauric-logo.svg" width="60%" alt="Tauric Research">
</p>

# TradingAgents for OpenCode

Run the TradingAgents multi-agent trading pipeline from inside OpenCode. `/ta-analyze NVDA
2026-10-23` collects point-in-time data, calls a model for every stage, validates each
report, and writes the same report tree the LangGraph pipeline produces. Free OpenCode
models work out of the box; `/ta-backtest` sweeps a date grid and `/ta-report` reads a finished
run without calling a model.

The plugin lives in this repository under `.opencode/plugins/tradingagents` and is
registered as the `trading-analysis` skill. TypeScript owns the stage order and every model
call; a Python worker reuses the repo's dataflows, prompts, memory log, and reports.

> Research tool, not financial advice. Trading performance varies with the model, the
> analysis date, and data quality.

## What you get

- **Full decision pyramid** — four analysts in parallel, bull/bear debate, research
  manager, trader, three-way risk debate, portfolio manager.
- **Point-in-time data** — each run fetches only what was published by the analysis date.
  SEC filings are read as filed; a run dated in the past does not see later restatements.
- **Checkpointed stages** — every stage records its prompt hash. Resuming an unchanged run
  costs **0 model calls**.
- **Memory** — past decisions are settled against realised returns and their reflections
  feed the next run's portfolio manager.
- **Reports** — markdown plus a one-page HTML report, the graph-shaped state log, and the
  run's decision in the memory log.
- **Backtests** — a ticker/date grid with its own memory log; rerunning a sweep skips the
  cells already logged.

## Requirements

- [OpenCode](https://opencode.ai) — the plugin uses the V2 plugin API
  (`@opencode/plugin`, `ctx.session.generate`) and is tested against **2.1.4**; the
  test suite installs that exact API version.
- [uv](https://docs.astral.sh/uv/) and Python 3.11+ — the plugin runs the worker with
  `uv run python -m tradingagents.opencode_worker` from the project root.
- **No required data or LLM key.** Prices, indicators, and news come from Yahoo Finance by
  default; SEC EDGAR filings need no account. Optional keys in `.env`:
  - `FRED_API_KEY` for macro data (free).
  - `ALPHA_VANTAGE_API_KEY` if you switch a vendor to Alpha Vantage.
  - `TYPESAFE_API_KEY` for Jev social-post screening (posts pass through unscreened
    without it).
  - `SEC_EDGAR_USER_AGENT` with your contact address, so SEC can reach you rather than
    the project.
- The default `*-free` model is reached through OpenCode itself; the free tier refuses
  API-key clients, and the plugin never sets client headers to work around that.

## Setup

```bash
git clone https://github.com/ijit32/TradingAgents-rs.git
cd TradingAgents-rs
uv sync
cp .env.example .env    # add the data keys above
opencode                # open OpenCode in this directory
```

The plugin loads automatically from `.opencode/plugins/`. The first run asks for
permission to run the worker command (`uv run python -m tradingagents.opencode_worker`);
allow it once and save, and later runs start without a prompt. Check the transport before
a long run:

```text
/ta-probe
/ta-probe --parallel 4
```

The probe is expected to pass on the session path. `/ta-probe --mode text` is a
diagnostic: the gateway refuses it with `403 FreeTierError`, which separates "model
unavailable" from "transport regressed".

## Quickstart

```text
/ta-analyze NVDA 2026-10-23
/ta-analyze NVDA 2026-10-23 --analysts market,news --depth 2
/ta-analyze BTC-USD 2026-10-23 --asset-type crypto
/ta-analyze NVDA 2026-10-23 --resume NVDA_2026-10-23_20261024_1152
/ta-report
/ta-backtest NVDA,AAPL 2026-01-02 2026-03-01 --every 7 --run-id q1
```

The same runs are available as tools for chat use: `trading_analyze`, `trading_backtest`,
`trading_report`, `trading_status`, and `trading_probe`.
`trading_report` and `trading_status` read the run directory and never call a model.

### `/ta-analyze` arguments

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

### `/ta-backtest` arguments

| Argument | Meaning |
|---|---|
| `tickers` | Required. Comma-separated symbols. |
| `start`, `end` | Required `YYYY-MM-DD` bounds; `end` defaults to today. |
| `--every` | Trading days between analysis dates. Defaults to `7`. |
| `--run-id` | Sweep id. Reuse it to resume; generated when omitted. |
| `--depth`, `--analysts`, `--model`, `--quick-model`, `--deep-model`, `--language` | As for `/ta-analyze`. |
| `--no-html` | Skip the HTML page per cell. |

## How a run works

```text
init → plan → (collect → prompt → generate → validate → checkpoint) × stages
```

| Group | Stages | Model tier |
|---|---|---|
| Analysts (parallel) | `market`, `sentiment`, `news`, `fundamentals` | quick |
| Research debate | `bull_researcher_N`, `bear_researcher_N` | quick |
| Research manager | investment plan and `recommendation` | deep |
| Trader | transaction proposal and `action` | quick |
| Risk debate | `aggressive_analyst_N`, `conservative_analyst_N`, `neutral_analyst_N` | quick |
| Portfolio manager | final decision and `rating` | deep |

`--depth` sets the rounds: `1` = 1 research + 1 risk round (12 stages), `2` = 2 + 1,
`3` = 2 + 2. The debate transcript keeps the last two turns verbatim and trims earlier
ones with an explicit marker, so extra rounds cost bounded prompt size.

The four analysts collect data one at a time — the vendors are the rate-limited part —
then generate concurrently up to `concurrency`. Every other stage runs one at a time,
because each prompt reads the previous turn.

The worker holds the stage list and renders the same prompts the LangGraph nodes use
(`tests/test_prompt_snapshots.py` pins all twelve), so the plugin cannot drift from the
repo's agent code. The three decision stages append a JSON schema and are parsed with the
repo's pydantic models. A rejected parse gets a repair prompt, twice, then a lenient
acceptance marked `> [review]` rather than a lost stage.

## Configuration

Plugins under `.opencode/plugins/` receive no options; every value has a working default.
To override, use the object form of the `plugins` entry in `opencode.json(c)`:

```jsonc
{
  "plugins": {
    ".opencode/plugins/tradingagents": {
      "quickModel": "opencode/deepseek-v4.1-flash-free",
      "deepModel": "opencode/deepseek-v4.1-flash-free",
      "stageModels": { "research_manager": "opencode/glm-5.3-flash-free" },
      "fallbackModels": ["opencode/qwen3.8-flash-free"],
      "concurrency": 4,
      "callTimeoutMs": 180000,
      "maxRetries": 2,
      "python": "uv",
      "runRoot": "~/.tradingagents/opencode",
      "memory": true,
      "html": true,
      "memoryLogPath": "",
      "resultsDir": ""
    }
  }
}
```

Defaults: depth 1, one debate round, concurrency 4, the free DeepSeek model for both tiers.
A paid or Go model works by naming it in `quickModel`/`deepModel` or per stage in
`stageModels`; the pipeline does not care which tier a model comes from.

`memory` settles past decisions and records this one. `html` writes `complete_report.html`
beside the markdown. `memoryLogPath` and `resultsDir` override the repo defaults
(`~/.tradingagents/memory/trading_memory.md`, `~/.tradingagents/logs`); empty means the
default. A backtest sets both per sweep, so its cells never touch your own log.

## Where the output is

```text
~/.tradingagents/opencode/<ticker>_<date>_<stamp>/
  request.json  run.json  meta.json  checkpoint.json  final_state.json
  data/market.md  data/social.md  data/news.md  data/fundamentals.md
  data/index.json
  stages/<stage>.prompt.md  .out.txt  .md
  stages/investment_debate.json  risk_debate.json
  stages/research_manager.json  trader.json  portfolio_manager.json
  memory/pending.json  reflections.json  past_context.md  note.txt
  reports/1_analysts/…  2_research/…  3_trading/…  4_risk/…  5_portfolio/…
  reports/complete_report.md  complete_report.html  run_notes.md
```

The state log goes to `<resultsDir>/<TICKER>/TradingAgentsStrategy_logs/`, and the decision
to `<memoryLogPath>`. `/ta-report` returns the latest run's `complete_report.md`; pass
`--html` for the rendered page, or a run id for an older run.

A pack that could not reach a vendor keeps an `<… unavailable>` placeholder and a note in
`data/index.json`; a failure degrades one section instead of losing the pack. Warnings,
truncation markers, and lenient acceptances are listed in `reports/run_notes.md` and the
run summary.

## Resume and memory

A stage is skipped when it succeeded (or was already skipped) **and** its rendered prompt
hash is unchanged. Upstream edits shift downstream hashes, so only the affected tail
reruns; resuming the same run repeatedly stays at 0 model calls.

Memory runs beside the analyst wave, as it does in the graph: `memory-begin` finds pending
decisions whose holding window has traded and renders the graph's reflection prompt, the
plugin generates one reflection per entry, and `memory-apply` writes them back and reads
the ticker's lessons. The portfolio manager waits for that file; a memory failure is a
warning, not a failed run.

After the last stage, `settle` writes the reports, the state log, and this run's decision
as a pending memory entry. Resuming rewrites the artefacts without duplicating the entry.

## Backtests

A sweep is a plan plus ordinary runs. `backtest-plan` expands the grid and skips cells
already in the sweep's own `trading_memory.md`; each remaining cell runs with its own run
root, results directory, and memory log. Reusing `--run-id` continues an interrupted sweep
without repeating logged cells. One final memory pass settles the tail, then the worker
scores the log exactly as the CLI backtest does: resolved/pending, hit rate, and mean alpha
by rating.

A failed cell is reported and the sweep continues; it is not logged, so the next attempt
retries it in a fresh run directory. Cancellation stops immediately and leaves completed
cells in the log.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `freetier` | Generation used a transport the gateway refuses | Keep the session path; run `/ta-probe` |
| `auth` | Missing or invalid provider key | Fix credentials; retry will not help |
| `ratelimit` | Provider throttled | The run backs off; resume later |
| `timeout` | No answer within `callTimeoutMs` | Resume the run; completed stages skip |
| `quota` | The free allowance is spent | Wait for it to reset, or configure a paid/Go model |
| `validation failed` after repairs | Report too short, or the decision JSON does not match the schema | Check `stages/<stage>.out.txt`; the run records a lenient fallback marked `> [review]` |
| `no <analyst> data could be collected` | Every vendor for that pack failed | Check the ticker and data keys; see `data/index.json` notes |
| `project directory missing` / `no pyproject.toml` | OpenCode is not running at the repo root | Reopen OpenCode in the repository, or set `runRoot` only for runs, not for the project |
| `uv could not be started` | `uv` is not on `PATH` | Install uv, or point `python` at an interpreter |

## Current limits

- The free tier has a usage allowance. Once it is spent, every model call fails with
  `quota` and points at OpenCode Go. A paid or Go model avoids the wait but is no longer
  free.
- On the free model expect roughly 45–60 s per analyst stage, and 10–12 min for a fresh
  depth-1 run. Resume costs seconds. A four-analyst wave was measured at 413 s including a
  single 342 s retry.
- Social packs for past dates are not point-in-time: StockTwits and Reddit serve recent
  items only. The pack says so, and the sentiment report lowers confidence; do not present
  those quotes as a historical record.
- A leniently accepted stage is marked `> [review]` and listed in the warnings. Treat its
  numbers as provisional.
- A decision whose rating cannot be read is `REVIEW`, never a silent `Hold`.

## Learn more

- [`.opencode/plugins/tradingagents/README.md`](.opencode/plugins/tradingagents/README.md)
  — architecture, phase results, worker protocol, and the measured runs.
- [`.opencode/skills/trading-analysis/SKILL.md`](.opencode/skills/trading-analysis/SKILL.md)
  — the agent-facing contract for `/ta-analyze`, `/ta-backtest`, and `/ta-report`.
- [`docs/cli.md`](docs/cli.md) — the CLI, Python API, Docker, and provider setup, for runs
  outside OpenCode.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes.

## Credits

Built on [TradingAgents](https://github.com/TauricResearch/TradingAgents) by Tauric
Research; this repository adds the OpenCode plugin, worker, and provider support. Please
cite the original work:

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework},
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138},
}
```
