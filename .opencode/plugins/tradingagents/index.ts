/**
 * TradingAgents OpenCode plugin.
 *
 * The plugin owns the analysis pipeline: it creates a run directory, shells out
 * to the Python worker for data collection and prompt rendering, calls OpenCode
 * models for every generation, and checkpoints each stage so a run resumes
 * without repeating model calls.
 *
 * Registered here:
 *   `/analyze`          — run the pipeline from a command line
 *   `trading_analyze`   — the same run, callable from chat
 *   `trading_backtest`  — a sweep over a date grid, one isolated memory log
 *   `trading_report`    — the written report of a finished run
 *   `trading_status`    — what a run has produced so far
 *   `trading_probe`     — Phase 0 diagnostic (generation reachability)
 */

import { Plugin } from "@opencode/plugin"
import type { AnalyzeInput, BacktestInput } from "./types"
import { formatModelRef, readConfig } from "./config"
import { renderBacktest, runBacktest } from "./backtest"
import { describeRun, readReport, renderSummary, runAnalysis } from "./pipeline"
import { type ProbeInput, runProbe } from "./probe"

function parseAnalysisText(text: string): AnalyzeInput {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: Record<string, unknown> = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    const next = (): string | undefined => tokens[++index]
    switch (token) {
      case "--date":
      case "--trade-date":
        input.trade_date = next()
        break
      case "--depth":
        input.depth = Number(next())
        break
      case "--model":
        input.model = next()
        break
      case "--quick-model":
        input.quick_model = next()
        break
      case "--deep-model":
        input.deep_model = next()
        break
      case "--analysts":
        input.analysts = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--asset-type":
        input.asset_type = next()
        break
      case "--language":
        input.output_language = next()
        break
      case "--resume":
        input.resume_run = next() ?? ""
        break
      case "--rerun":
        input.rerun = next()
        break
      case "--rerun-from":
        input.rerun_from = next()
        break
      case "--run-dir":
        input.run_dir = next()
        break
      case "--no-memory":
        input.memory = false
        break
      case "--no-html":
        input.html = false
        break
      default:
        rest.push(token)
    }
  }
  if (rest[0] && !input.ticker) input.ticker = rest[0]
  if (rest[1] && !input.trade_date) input.trade_date = rest[1]
  return input as AnalyzeInput
}

function parseBacktestText(text: string): BacktestInput {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: Record<string, unknown> = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    const next = (): string | undefined => tokens[++index]
    switch (token) {
      case "--tickers":
        input.tickers = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--start":
        input.start = next()
        break
      case "--end":
        input.end = next()
        break
      case "--every":
      case "--every-n-days":
        input.every_n_days = Number(next())
        break
      case "--run-id":
        input.run_id = next()
        break
      case "--depth":
        input.depth = Number(next())
        break
      case "--model":
        input.model = next()
        break
      case "--quick-model":
        input.quick_model = next()
        break
      case "--deep-model":
        input.deep_model = next()
        break
      case "--analysts":
        input.analysts = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--language":
        input.output_language = next()
        break
      case "--no-html":
        input.html = false
        break
      default:
        rest.push(token)
    }
  }
  if (rest[0] && !input.tickers) {
    input.tickers = rest[0].split(",").map((item) => item.trim()).filter(Boolean)
  }
  if (rest[1] && !input.start) input.start = rest[1]
  if (rest[2] && !input.end) input.end = rest[2]
  return input as BacktestInput
}

type MutableProbeInput = { -readonly [K in keyof ProbeInput]: ProbeInput[K] }

function parseProbeText(text: string): { input: MutableProbeInput; prompt: string } {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: MutableProbeInput = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    if (token === "--model" && tokens[index + 1]) {
      input.model = tokens[++index]
    } else if (token === "--parallel" && tokens[index + 1]) {
      input.parallel = Number(tokens[++index])
    } else if (token === "--timeout-ms" && tokens[index + 1]) {
      input.timeoutMs = Number(tokens[++index])
    } else if (token === "--mode" && tokens[index + 1]) {
      input.mode = tokens[++index] === "text" ? "text" : "session"
    } else {
      rest.push(token)
    }
  }
  return { input, prompt: rest.join(" ") }
}

function errorMarkdown(title: string, error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return `### ${title}\n\n**Failed** — ${message}`
}

export default Plugin.define({
  id: "tradingagents",

  async setup(ctx) {
    const config = readConfig(ctx.options)

    const toolRegistration = await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "trading",
        description: "TradingAgents analysis pipeline and diagnostics",
      })

      editor.add({
        name: "analyze",
        description:
          "Run the TradingAgents analysis pipeline for one instrument and date. Collects " +
          "point-in-time data, calls an OpenCode model for each stage, validates the output, " +
          "and checkpoints the run so it can resume. Runs the four analysts in parallel, " +
          "then the bull/bear debate, research manager, trader, risk debate, and portfolio " +
          "manager in order; returns the run's rating and report paths.",
        input: {
          type: "object",
          properties: {
            ticker: { type: "string", description: "Symbol, e.g. NVDA." },
            trade_date: {
              type: "string",
              description: "Analysis date in YYYY-MM-DD. Defaults to today.",
            },
            asset_type: { enum: ["stock", "crypto"] },
            analysts: {
              type: "array",
              items: { enum: ["market", "social", "news", "fundamentals"] },
              description: "Defaults to every analyst.",
            },
            depth: {
              type: "integer",
              minimum: 1,
              maximum: 3,
              description: "Research depth: 1 = one debate round, 3 = two debate and two risk rounds.",
            },
            model: {
              type: "string",
              description: "Override the model for every stage (provider/model[#variant]).",
            },
            quick_model: { type: "string", description: "Model for the quick tier." },
            deep_model: { type: "string", description: "Model for the deep tier." },
            output_language: { type: "string", description: "Report language. Defaults to English." },
            portfolio: { type: "object", description: "Portfolio context for the risk stage." },
            resume_run: {
              type: "string",
              description: "Resume an existing run by id or unique prefix.",
            },
            rerun: { type: "string", description: "Rerun one stage even if its prompt is unchanged." },
            rerun_from: {
              type: "string",
              description: "Rerun this stage and everything downstream of it.",
            },
            run_dir: { type: "string", description: "Resume an existing run directory." },
          },
          required: ["ticker"],
          additionalProperties: false,
        },
        options: { namespace: "trading", codemode: true },
        execute: async (raw, context) => {
          const input = (raw ?? {}) as AnalyzeInput
          const progress = async (message: string): Promise<void> => {
            await context.progress({ status: message })
          }
          try {
            const summary = await runAnalysis(ctx, input, {
              progress,
              signal: context.signal,
            })
            await ctx.storage.set("analysis/last", {
              at: new Date().toISOString(),
              runId: summary.runId,
              runDir: summary.runDir,
              ticker: summary.ticker,
              tradeDate: summary.tradeDate,
              calls: summary.calls,
            })
            return {
              content: renderSummary(summary),
              metadata: {
                run_id: summary.runId,
                run_dir: summary.runDir,
                calls: summary.calls,
                stages: summary.stages.length,
                warnings: summary.warnings.length,
              },
            }
          } catch (error) {
            return {
              content: errorMarkdown("TradingAgents run", error),
              metadata: { ok: false },
            }
          }
        },
      })

      editor.add({
        name: "backtest",
        description:
          "Run a TradingAgents backtest: every ticker on a date grid, each cell a full " +
          "analysis, with the sweep's own memory log as the results table. Rerun the same " +
          "run_id to continue without repeating logged cells. Returns per-cell ratings and " +
          "the sweep summary (resolved/pending, hit rate, mean alpha).",
        input: {
          type: "object",
          properties: {
            tickers: {
              type: "array",
              items: { type: "string" },
              description: "Symbols to sweep, e.g. [\"NVDA\", \"AAPL\"].",
            },
            ticker: { type: "string", description: "A single symbol, instead of tickers." },
            start: { type: "string", description: "First analysis date, YYYY-MM-DD." },
            end: {
              type: "string",
              description: "Last analysis date, YYYY-MM-DD. Defaults to today.",
            },
            every_n_days: {
              type: "integer",
              minimum: 1,
              description: "Trading days between analysis dates. Defaults to 7.",
            },
            run_id: {
              type: "string",
              description: "Sweep id; reuse it to resume. Generated when omitted.",
            },
            depth: { type: "integer", minimum: 1, maximum: 3 },
            analysts: {
              type: "array",
              items: { enum: ["market", "social", "news", "fundamentals"] },
            },
            model: { type: "string", description: "Override the model for every stage." },
            quick_model: { type: "string" },
            deep_model: { type: "string" },
            output_language: { type: "string" },
            html: { type: "boolean", description: "Write an HTML report per cell." },
          },
          required: ["start"],
          additionalProperties: false,
        },
        options: { namespace: "trading", codemode: true },
        execute: async (raw, context) => {
          const input = (raw ?? {}) as BacktestInput
          const progress = async (message: string): Promise<void> => {
            await context.progress({ status: message })
          }
          try {
            const result = await runBacktest(ctx, input, {
              progress,
              signal: context.signal,
            })
            await ctx.storage.set("backtest/last", {
              at: new Date().toISOString(),
              runId: result.runId,
              sweepDir: result.sweepDir,
              calls: result.calls,
              cells: result.cells.length,
            })
            return {
              content: renderBacktest(result),
              metadata: {
                run_id: result.runId,
                sweep_dir: result.sweepDir,
                calls: result.calls,
                cells: result.cells.length,
                failed: result.cells.filter((cell) => cell.status === "failed").length,
              },
            }
          } catch (error) {
            return {
              content: errorMarkdown("TradingAgents backtest", error),
              metadata: { ok: false },
            }
          }
        },
      })

      editor.add({
        name: "report",
        description:
          "Return the written report of a TradingAgents run (`complete_report.md`, or the " +
          "HTML page). Defaults to the latest run; never calls a model.",
        input: {
          type: "object",
          properties: {
            run_id: {
              type: "string",
              description: "Run id, unique prefix, or run directory. Defaults to the latest run.",
            },
            format: {
              enum: ["md", "html"],
              description: "Markdown (default) or the rendered HTML page.",
            },
          },
          additionalProperties: false,
        },
        options: { namespace: "trading", codemode: true },
        execute: async (raw) => {
          const input = (raw ?? {}) as { run_id?: string; format?: "md" | "html" }
          try {
            const view = await readReport(ctx, input.run_id, input.format ?? "md")
            return {
              content: view.content,
              metadata: { run_dir: view.runDir, path: view.path, format: view.format },
            }
          } catch (error) {
            return {
              content: errorMarkdown("TradingAgents report", error),
              metadata: { ok: false },
            }
          }
        },
      })

      editor.add({
        name: "status",
        description:
          "Report what a TradingAgents run has produced so far: stages, statuses, models, " +
          "attempts, warnings, and report files. Reads the run directory; never calls a model.",
        input: {
          type: "object",
          properties: {
            run_id: {
              type: "string",
              description: "Run id, unique prefix, or run directory. Defaults to the latest run.",
            },
          },
          additionalProperties: false,
        },
        options: { namespace: "trading", codemode: true },
        execute: async (raw) => {
          const input = (raw ?? {}) as { run_id?: string }
          try {
            const content = await describeRun(ctx, input.run_id)
            return { content, metadata: {} }
          } catch (error) {
            return {
              content: errorMarkdown("TradingAgents status", error),
              metadata: { ok: false },
            }
          }
        },
      })

      editor.add({
        name: "probe",
        description:
          "Phase 0 diagnostic. Generates text through OpenCode with an OpenCode model — " +
          "free-tier models included — to confirm in-OpenCode generation works before the " +
          "analysis pipeline is enabled. Reports latency, attempts, and any error class.",
        input: {
          type: "object",
          properties: {
            prompt: {
              type: "string",
              description: "Prompt to send. Defaults to a one-line reachability check.",
            },
            model: {
              type: "string",
              description:
                "Model as provider/model[#variant]. Defaults to the plugin's quick model " +
                `(${formatModelRef(config.quickModel)}).`,
            },
            mode: {
              enum: ["session", "text"],
              description:
                "session uses ctx.session.generate, the path that carries OpenCode's client " +
                "identity (default). text uses ctx.generate.text, which the gateway refuses for " +
                "free-tier models; keep it only as a diagnostic.",
            },
            parallel: {
              type: "integer",
              minimum: 1,
              maximum: 8,
              description: "Fire this many identical calls to measure concurrency headroom.",
            },
            timeoutMs: {
              type: "integer",
              minimum: 5000,
              maximum: 900000,
              description: "Per-call timeout in milliseconds.",
            },
          },
          additionalProperties: false,
        },
        options: { namespace: "trading", codemode: true },
        execute: async (raw, context) => {
          const input = (raw ?? {}) as ProbeInput
          await context.progress({ status: "probing model" })
          try {
            const { runs, model, mode, report } = await runProbe(ctx, input)
            await ctx.storage.set("probe/last", {
              at: new Date().toISOString(),
              mode,
              model,
              runs: runs.map((run) => ({
                ok: run.ok,
                mode: run.mode,
                model: run.model,
                attempts: run.attempts,
                ms: run.ms,
                chars: run.chars,
                error: run.error ? { ...run.error } : null,
              })),
            })
            return {
              content: report,
              metadata: {
                mode,
                model,
                calls: runs.length,
                ok: runs.filter((run) => run.ok).length,
                failed: runs.filter((run) => !run.ok).length,
              },
            }
          } catch (error) {
            return {
              content: errorMarkdown("OpenCode generation probe", error),
              metadata: { model: input.model ?? formatModelRef(config.quickModel), ok: 0 },
            }
          }
        },
      })
    })

    const commandRegistration = await ctx.command.transform((editor) => {
      editor.add({
        name: "analyze",
        description:
          "Run the TradingAgents pipeline: /analyze NVDA 2026-10-23 [--depth 2] [--model provider/model]",
        execute: async ({ sessionID, prompt }) => {
          const input = parseAnalysisText(prompt.text ?? "")
          const progress = async (message: string): Promise<void> => {
            if (!ctx.session.synthetic) return
            await ctx.session.synthetic({ sessionID, text: message })
          }
          try {
            await progress(`TradingAgents: starting ${input.ticker ?? "run"}…`)
            const summary = await runAnalysis(ctx, input, { progress })
            await progress(renderSummary(summary))
          } catch (error) {
            await progress(errorMarkdown("TradingAgents run", error))
          }
        },
      })

      editor.add({
        name: "backtest",
        description:
          "Run a sweep: /backtest NVDA,AAPL 2026-01-02 2026-03-01 [--every 7] [--run-id id]",
        execute: async ({ sessionID, prompt }) => {
          const input = parseBacktestText(prompt.text ?? "")
          const progress = async (message: string): Promise<void> => {
            if (!ctx.session.synthetic) return
            await ctx.session.synthetic({ sessionID, text: message })
          }
          try {
            await progress(`TradingAgents backtest: ${(input.tickers ?? []).join(", ") || "sweep"}…`)
            const result = await runBacktest(ctx, input, { progress })
            await progress(renderBacktest(result))
          } catch (error) {
            await progress(errorMarkdown("TradingAgents backtest", error))
          }
        },
      })

      editor.add({
        name: "report",
        description: "Show a run's written report: /report [run-id] [--html]",
        execute: async ({ sessionID, prompt }) => {
          const tokens = (prompt.text ?? "").split(/\s+/).filter(Boolean)
          const format = tokens.includes("--html") ? "html" : "md"
          const runId = tokens.find((token) => !token.startsWith("--"))
          try {
            const view = await readReport(ctx, runId, format)
            await ctx.session.synthetic?.({ sessionID, text: view.content })
          } catch (error) {
            await ctx.session.synthetic?.({
              sessionID,
              text: errorMarkdown("TradingAgents report", error),
            })
          }
        },
      })

      editor.add({
        name: "trading-probe",
        description:
          "Check that OpenCode generation works for a model (default: the plugin's free quick model)",
        execute: async ({ sessionID, prompt }) => {
          const { input, prompt: text } = parseProbeText(prompt.text ?? "")
          if (text) input.prompt = text
          await ctx.session.synthetic?.({
            sessionID,
            text: "Running OpenCode generation probe…",
          })
          try {
            const { report } = await runProbe(ctx, input)
            await ctx.session.synthetic?.({ sessionID, text: report })
          } catch (error) {
            await ctx.session.synthetic?.({
              sessionID,
              text: errorMarkdown("OpenCode generation probe", error),
            })
          }
        },
      })
    })

    return async () => {
      await commandRegistration.dispose()
      await toolRegistration.dispose()
    }
  },
})
