# TradingAgents OpenCode plugin

Reimplements the TradingAgents pipeline as an OpenCode plugin. TypeScript orchestrates the
stages and calls OpenCode models; the existing Python data/reporting layer is reused through
a worker instead of being ported.

## Layout

| File | Role |
|---|---|
| `index.ts` | `Plugin.define`; wires the tool and command registrations and the dispose hook |
| `tools.ts` | Registers `trading_analyze`, `trading_backtest`, `trading_report`, `trading_status`, `trading_probe` |
| `commands.ts` | Registers `/ta-analyze`, `/ta-backtest`, `/ta-report`, `/ta-probe` |
| `parse.ts` | Command-line parsing for tickers, dates, flags, and the probe prompt |
| `permissions.ts` | The `bash` permission gate around the worker command |
| `render.ts` | Shared error markdown for tools and commands |
| `editor-types.ts` | Editor types derived from the public `Plugin.Context`, not internal dist paths |
| `config.ts` | Options, defaults, `provider/model[#variant]` parsing, per-stage model selection, run root |
| `types.ts` | Pipeline, checkpoint, and worker-reply types |
| `worker.ts` | The worker facade: shared sessions, one-shot fallback, `status` helper, protocol re-exports |
| `session.ts` | Persistent `serve` child: request ids, reply routing, timeouts, abort, crash restart, structured stderr logs |
| `sessions.ts` | The session registry: one persistent worker per project and runner, plus the one-shot switch |
| `oneshot.ts` | One action per subprocess (`TA_OPENCODE_WORKER=oneshot`) and the spawn/environment checks |
| `protocol.ts` | Frame encoding/decoding and the version check |
| `protocol.schema.json` | Canonical worker protocol (v1) request/reply schema |
| `protocol.generated.ts` | Generated TypeScript types; do not edit by hand |
| `checkpoint.ts` | Atomic `checkpoint.json`, prompt/request hashing, run-id naming, resume rule |
| `pipeline.ts` | Public facade over the pipeline modules below |
| `runtime.ts` | Input normalization, run context, checkpoint store, worker helpers |
| `stage.ts` | Prepare, generate, validate, and repair one stage |
| `run.ts` | The ordered run: analyst wave, debate chain, memory pass, resume, final handoff |
| `run-dir.ts` | Run directory creation, id resolution, latest-run lookup |
| `payload.ts` | `request.json`, merged with a resumed run's saved settings |
| `memory-pass.ts` | Settle past decisions, generate reflections, inject the lessons |
| `report.ts` | `final_state.json`, terminal summary, report and status readers |
| `backtest.ts` | Plans a date grid, runs one isolated analysis per cell, settles the tail, renders the summary |
| `sweep.ts` | Pure backtest helpers: ids, dates, tickers, cells, per-cell input |
| `generate.ts` | Generation wrapper: session-bound requests, timeouts, retry/backoff, fallback |
| `models.ts` | Model discovery and validation against the active registry |
| `probe.ts` | Phase 0 diagnostic: reachability, latency, concurrency, error class |

## Phase 0 result: free-tier generation

**G0 passed.** A Console free-tier model (`opencode/deepseek-v4.1-flash-free`) can be called
from inside OpenCode through `ctx.session.generate`, including 4 concurrent calls.

The plugin must use the session path. The two paths behave differently:

| Path | What the server sends | Free tier |
|---|---|---|
| `ctx.generate.text` | `http: { headers: { "x-opencode-session": <random> } }` — replaces the whole header set | `403 FreeTierError` |
| `ctx.session.generate` | normal request builder: `x-opencode-client`, `x-opencode-project`, `x-opencode-session`, `x-session-affinity`, `User-Agent: opencode/latest/<version>/cli` | works |

`Generate.text` builds its own request and overwrites `http.headers`, so the client identity
never reaches the gateway. `SessionGenerate` and the agent loop share the prepared request, so
both carry it. Measured headers for the two kinds are identical apart from the per-request
session id, which confirms the difference is the path, not the model.

Replaying the observed headers from `curl` still returns `403`, so the gate is not a bare
header check — it accepts OpenCode's own in-process requests only. That is the intended
control, and it is why this plugin does **not** set `x-opencode-client` or a `User-Agent` by
hand: copying those headers would be spoofing, and there is a supported path that already
satisfies the gate.

Consequence for the pipeline: every model call creates a short-lived session pinned to the
target model, generates once, then deletes it. There is no history and no tool access, so the
prompt contract is still "one prompt in, one string out" — only the transport changed.

Observed cost of that wrapper: **1.8–2.4 s** per call, dominated by session create/delete.

Reproduce:

```text
/ta-probe                      # session path, the default
/ta-probe --parallel 4         # concurrency headroom
/ta-probe --mode text          # expected 403, proves the gate
```

Keep `/ta-probe --mode text` in the toolbox: it is the fastest way to tell "model is
unavailable" apart from "transport regressed".

## Phase 2: the four analysts

The run loop is `init → collect → prompt → generate → validate → checkpoint`, grouped by
stage. Phase 2 implements all four analysts in one group: their packs are collected one at a
time (the vendors are the rate-limited part), then the four reports are generated concurrently
up to `concurrency`. Phase 3 appends the decision stages; the loop itself never changed, because
the stage list comes from the worker's `plan` action rather than from a list in TypeScript.

```text
/ta-analyze NVDA 2026-10-23                    # all four analysts
/ta-analyze NVDA 2026-10-23 --analysts market,news
/ta-analyze NVDA 2026-10-23 --depth 2          # more debate rounds later
/ta-analyze NVDA 2026-10-23 --resume NVDA_2026-…  # skip completed stages
/ta-analyze NVDA 2026-10-23 --rerun market_analyst
```

Each analyst has one pack and one prompt builder, both taken from the graph's own code:

| Analyst key | Stage | Pack | Prompt builder |
|---|---|---|---|
| `market` | `market_analyst` | `data/market.md` | `build_market_analyst_messages` |
| `social` | `sentiment_analyst` | `data/social.md` | `build_sentiment_analyst_messages` |
| `news` | `news_analyst` | `data/news.md` | `build_news_analyst_messages` |
| `fundamentals` | `fundamentals_analyst` | `data/fundamentals.md` | `build_fundamentals_analyst_messages` |

The pack key `social` matches the request analyst key; the stage name and report field stay
`sentiment`/`sentiment_report`, as in the graph.

Packs are bounded deterministically: sections have a trim rank, the lowest trims first, and
the section that anchors identity trims last. A vendor failure becomes an `<… unavailable>`
placeholder plus a note recorded in `data/index.json`, so one dead feed degrades one section
instead of losing the pack. A social pack for a historical date also says in its own text that
StockTwits and Reddit are not archived, so the analyst lowers confidence instead of reading
today's chatter as history.

The model call happens in TypeScript; every other step is the Python worker reusing the repo:

| Action | What it does | Network |
|---|---|---|
| `init` | Normalize `request.json`, write `meta.json` + `run.json` | no |
| `plan` | Stage order, groups, tiers, and output targets for this request | no |
| `collect --analyst <key>` | Call the dated tool functions with explicit `trade_date`; write a bounded pack | yes |
| `prompt --stage <stage>` | Render the graph's messages minus tool calls, plus the pack; append the JSON schema for a structured stage | no |
| `validate --stage <stage>` | Length contract, argument transcript, or pydantic JSON; promote `out.txt` to `md` | no |
| `debates` | Both debate transcripts in `final_state.json` shape | no |
| `memory-begin` | Find pending decisions whose holding window has traded; render the graph's reflection prompts | yes |
| `memory-apply` | Write the reflections, then `memory/past_context.md` and `memory/note.txt` | no |
| `settle` | `complete_report.md`/`.html`, `reports/run_notes.md`, the graph-shaped state log, and this run's pending memory entry | no |
| `backtest-plan` | Which `(ticker, date)` cells the sweep still needs | no |
| `backtest-summary` | Score the sweep's memory log and render the summary | no |
| `status` | Stage list, checkpoint, data index | no |

Exit codes are the protocol: `0` ok, `2` validation failed (retryable), `3` bad input, `4`
vendor missing, `1` internal. The plugin turns `2` into a repair call — once for prose, twice
for a structured decision — then accepts the output leniently with a `> [review]` marker
rather than discarding the stage.

Resume rule: `status` is `ok` or `skipped` **and** the rendered prompt hash equals the saved
hash → skip. (`skipped` counts as success, so resuming a resumed run is still 0 calls rather
than rerunning what the last resume skipped.) The hash covers the pack and every upstream
report, so upstream changes invalidate exactly the tail. A resumed run with unchanged prompts
reports **0 model calls**.

Run directories live under `~/.tradingagents/opencode/<ticker>_<date>_<stamp>/`, matching the
layout the plan specifies: `request.json`, `run.json`, `meta.json`, `checkpoint.json`,
`data/`, `stages/`, `memory/`, `reports/`, `final_state.json`.

Measured on the free model for `NVDA 2026-10-23`, all four analysts in one wave: **4 model
calls, 413 s** total (market 54 s, sentiment 52 s, news 48 s, fundamentals 342 s — a retry).
The market pack hit the 20k cap and was trimmed; the other three were 3.6k–9.7k chars. A
resume of the same run: **0 calls, ~8 s**, all stages `skipped`. Expect roughly 1–7 min for a
fresh four-analyst run, dominated by model latency rather than collection.

## Phase 3: the decision pyramid

The worker owns the stage list, because the graph owns the order. `plan` returns groups, and
the plugin runs them: `parallel: true` groups prepare their prompts one at a time and then
generate concurrently; every other group runs turn by turn, because each prompt reads the
previous turn. This is the same route `ConditionalLogic` takes, with the round counts from
`depth`.

| Group | Stages | Tier |
|---|---|---|
| `analysts` (parallel) | `market_analyst`, `sentiment_analyst`, `news_analyst`, `fundamentals_analyst` | quick |
| `research_debate` | `bull_researcher_1`, `bear_researcher_1`, … `2` rounds at depth 2 | quick |
| `research_manager` | one stage | deep |
| `trader` | one stage | quick |
| `risk_debate` | `aggressive_analyst_1`, `conservative_analyst_1`, `neutral_analyst_1`, … `2` rounds at depth 3 | quick |
| `portfolio_manager` | one stage | deep |

Depth sets the rounds: `1 → (1 research, 1 risk)`, `2 → (2, 1)`, `3 → (2, 2)`. A depth-1 run
is 12 stages, which is the graph's minimum.

**Prompts come from the repo.** Every agent module now exposes the prompt builder the graph
node calls (`build_bull_researcher_prompt`, `build_trader_messages`, …), and the node invokes
that same builder, so the worker cannot drift from the CLI. `tests/test_prompt_snapshots.py`
pins the rendered prompt of all twelve stages.

**Structured stages return JSON.** The Research Manager, Trader, and Portfolio Manager use the
repo's pydantic models (`ResearchPlan`, `TraderProposal`, `PortfolioDecision`). The worker
appends a `## Structured output` block containing the model's JSON schema; the plugin sends
that prompt, and `validate` parses the reply, renders it to the same markdown the graph
produces, and writes `stages/<stage>.json` next to `stages/<stage>.md`. A rejected parse gets
a repair prompt with the validator's complaints (`### Repair`), twice, then a lenient pass
that keeps the prose and marks it.

**Rating flow.** RM `recommendation` → trader `action` → PM `rating`. The PM rating is the
run's `final_rating`; it is read back from `stages/portfolio_manager.json`, so a resumed run
reports the same rating without a model call. An unreadable lenient decision becomes `REVIEW`,
never a silent `Hold`.

**Debate storage.** Turns live in `stages/investment_debate.json` and `stages/risk_debate.json`
as `{turns: [{stage, speaker, argument}]}`. Writing turn *n* truncates the tail, so a rerun of
an earlier turn cannot leave a stale later turn in the transcript the next prompt reads.

**Transcript compaction.** The last two turns stay verbatim; earlier turns keep their first 800
chars plus `[… N chars of this earlier turn omitted for length …]`. Deterministic, no extra
model call. A depth-1 run's debate prompts are already 55–83k chars, so this matters as soon as
rounds are added.

**Failure policy.** A failed analyst does not stop the wave; the run reports every failure at
the end. A failed debate turn or decision stops the chain, because every later prompt would be
reasoning from a missing input. Either way the completed stages are checkpointed.

Measured on the free model, `NVDA 2026-10-23`, depth 1: **12 model calls, 567 s**, all stages
`ok`, final rating `Underweight`. Latency ranged 9 s (trader) to 152 s (fundamentals). The
immediate resume cost **0 calls, 21 s**. A depth-2 chain rendered and validated end to end from
the same packs — four investment turns, three risk turns, compaction active — with no model
call, which is the shape to expect when rounds increase.

## Phase 4: memory, reports, and backtests

The graph settles old decisions at the start, writes the reports at the end, and can sweep a
date grid. The plugin now does the same three things on the same run directory, with every
model call still in TypeScript.

### Memory

The pass starts beside the analyst wave — `memory-begin` decides which pending entries are due
by fetching the same prices the graph uses, and renders the graph's own reflection prompt
(`reflection_system_prompt` + `build_reflection_prompt`, shared with `Reflector`). The plugin
generates one reflection per entry, writes `memory/reflections.json`, then `memory-apply` writes
them back in one batched read-modify-write and reads the ticker's lessons into
`memory/past_context.md`. The Portfolio Manager waits for that file; nothing else does.

Failures are warnings, never stage failures. An entry whose window has not traded stays
pending. An entry whose prices cannot be fetched is reported and stays pending. A missing
reflection leaves the entry pending and puts a line in `memory/note.txt`, which the report
header shows.

### Reports and the state log

`settle` runs after `final_state.json` and writes the same artefacts as `save_reports` +
`_log_state`:

- `reports/1_analysts/…` through `reports/5_portfolio/…`
- `reports/complete_report.md` and `complete_report.html`
- `reports/run_notes.md` — the run's warnings, so the report can stay byte-comparable
- `<resultsDir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`
- the run's decision in the memory log, as a pending entry

It is idempotent: a resume rewrites the reports, overwrites the state log, and `store_decision`
refuses a duplicate entry. Read them with `trading_report` or `/ta-report`.

### Backtest

A sweep is a plan plus ordinary runs. `backtest-plan` expands the grid with `iter_grid` and
skips cells already in the sweep's own `trading_memory.md`; the plugin runs each remaining cell
with `run_root=<sweep>/runs`, `results_dir=<sweep>`, and `memory_log_path=<sweep>/trading_memory.md`.
Reusing `run_id` therefore continues a sweep with no repeated model calls. One final memory pass
on the last cell settles the tail, then `backtest-summary` scores the log exactly as
`run_backtest` reports it.

```text
/ta-backtest NVDA,AAPL 2026-01-02 2026-03-01 --every 7 --run-id q1
/ta-backtest NVDA 2026-01-02 2026-03-01 --run-id q1 --no-html   # resume; logged cells skip
```

A failed cell is reported and the sweep continues; the cell is *not* logged, so the next run of
the same sweep retries it in a new run directory. Cancellation stops immediately and leaves the
completed cells in the log.

### What matches a CLI run

| Artefact | CLI / graph | Plugin |
|---|---|---|
| Section reports | `save_reports` → `1_analysts`…`5_portfolio` | `settle` → `write_report_tree`, same function |
| Markdown report | `write_report_tree` → `complete_report.md` | same call, same five sections and header |
| HTML page | `write_report_tree(html=True)` | same call; `--no-html`/`html: false` skips it |
| State log | `_log_state` → `full_states_log_<date>.json` | `settle` writes the same keys, including `run_settings` |
| Memory entry | `record_decision` on the log's path | `store_decision` with the same rating and decision text |
| Reflection | `Reflector.reflect_on_final_decision` | same system/user prompt from `reflection.py` |
| Sweep layout | `<results_dir>/backtest/<run_id>/trading_memory.md` | same path, planned by `backtest-plan` |
| Sweep summary | `summarize(...).render()` | `backtest-summary` calls the same `summarize` |

The only deliberate difference is where the text comes from: the plugin's model
call, not the graph's, so wording varies run to run while the structure, files,
and scores do not.

Measured on the free model, `NVDA 2026-10-22`, depth 1, with one seeded pending decision:
**13 model calls, 714 s** (12 stages plus one reflection), rating `Underweight`. The reports
were 114.5k chars of markdown plus the HTML page, the state log 180.5k, and the Portfolio
Manager quoted the settled lesson in its decision. The resume cost **0 calls, 25 s** and did
not duplicate the decision.

## Configuration

Options come from the object form of the `plugins` entry in `opencode.json(c)`. Plugins under
`.opencode/plugins/` load automatically but receive no options, so every value has a working
default.

```jsonc
{
  "plugins": {
    ".opencode/plugins/tradingagents": {
      "quickModel": "opencode/deepseek-v4.1-flash-free",
      "deepModel": "opencode/deepseek-v4.1-flash-free",
      "stageModels": { "research_manager": "opencode/glm-5.3-flash-free" },
      "concurrency": 4,
      "callTimeoutMs": 180000,
      "maxRetries": 2,
      "runRoot": "~/.tradingagents/opencode",
      "memory": true,
      "html": true,
      "memoryLogPath": "",
      "resultsDir": ""
    }
  }
}
```

Defaults: depth 1, one debate round, concurrency 4 (one wave for the four analysts), free
DeepSeek for both tiers. A paid or Go model works by naming it in `quickModel`/`deepModel`; the
generation code does not care which tier it is.

India needs no configuration either. A `.NS` or `.BO` suffix is what marks an NSE or BSE
listing — a bare symbol stays US — and from that suffix the prompts and packs get INR
reporting and the India macro, news and social coverage on their own. For India, the only
optional key is `FRED_API_KEY`, used for the India CPI, g-sec, IIP and GDP series; without it
those series say they are missing. The key is free from
<https://fred.stlouisfed.org/docs/api/api_key.html> and, besides those series, lets a
historical run read macro values as they were published rather than as later revised.
Nothing in the protocol changes: the worker protocol,
`protocol.schema.json` and the generated types are untouched, and a US ticker behaves as
before. A portfolio given as an object takes an ISO currency code, and an INR book on an NSE
listing is labelled in INR rather than converted.

Coverage, the filed-as-of rules and the known gaps are documented in
[docs/india.md](../../../docs/india.md).

`memory` settles past decisions and records this one; `html` writes `complete_report.html`
beside the markdown. `memoryLogPath` and `resultsDir` override the repo defaults
(`~/.tradingagents/memory/trading_memory.md` and `~/.tradingagents/logs`); empty means the
default. A backtest sets both per sweep, so its cells never read the default log.

`concurrency` bounds only the generation calls; packs are always collected sequentially, since
the vendors are the part that rate-limits.

## Worker transport and protocol

The plugin spawns the worker once per OpenCode session:

```
uv run python -m tradingagents.opencode_worker serve
```

and sends one newline-delimited JSON request per line on stdin. Stdout carries only reply
frames; logs go to stderr as JSON lines. Every reply echoes the request `id`, so a slow
`collect` cannot be mistaken for the `status` sent after it. The server answers on a small
thread pool (`TA_WORKER_THREADS`, default 4), which lets a status poll run while a vendor
call is in flight.

`protocol.schema.json` is the canonical contract. `protocol.generated.ts` is generated from
it and is what `session.ts` writes against; the Python side validates the same frames and
`tests/test_opencode_protocol.py` checks both against the schema. Version 1 is enforced in
both directions: a mismatch is refused rather than misread.

Exit codes survive the transport as `exit_code` on the reply: `0` ok, `2` validation failed
(retryable), `3` bad input, `4` vendor missing, `1` internal. A failed action rejects with
`WorkerError`, carrying the same code and `errors` list the one-shot path produced. A
malformed frame, unknown action, or version mismatch fails only that request; the server
stays up.

One bad process is not reused. A timeout, abort, crash, or protocol mismatch kills the
session and rejects everything in flight; the next request starts a fresh worker. `close()`
sends `shutdown` and is called from the plugin's dispose hook, so no child outlives OpenCode.

```bash
# Regenerate the TypeScript types after editing the schema.
bun run protocol:types

# One process per action, useful when debugging a single action.
TA_OPENCODE_WORKER=oneshot bun test
```

## Safety and environment

The plugin asks before it shells out: `/ta-analyze` and `/ta-backtest` call
`ctx.permission.assert` with action `bash` and the resource
`uv run python -m tradingagents.opencode_worker`. A deny stops the run before any model
call; allow-once-with-save writes that command to the project's allow list.

An explicit `--run-dir` is confined to the plugin's run root. A path that resolves outside
`runRoot` (or a sweep's own `runs/` directory) is rejected before the worker starts, so a
run cannot write reports, a memory entry, or a state log into an unrelated directory.

Before spawning, the worker checks that the project directory exists, that it holds
`pyproject.toml`, and that a custom `python` path exists. On Windows `uv.exe` and
`python.exe` are resolved explicitly. Each failure names the fix instead of surfacing a bare
`spawn ENOENT`.

## Version and tests

Tested against **OpenCode 2.1.4**; `package.json` pins `@opencode/plugin@2.1.4` so the
type-check runs against that API. The suite covers model-reference parsing, config
defaults and clamping, checkpoint hashing/resume, run-id resolution, run-dir confinement,
spawn preflight, command parsing, frame encoding/version checks, reply-id routing,
structured errors, timeouts, aborts, crash restart, and log forwarding:

```bash
cd .opencode/plugins/tradingagents
bun install
bun test          # 58 tests
bun x tsc --noEmit
```

CI runs the same three steps in the `plugin` job.

