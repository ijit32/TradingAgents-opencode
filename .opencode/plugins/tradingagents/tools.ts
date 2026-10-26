/**
 * The `trading_*` tools: structured input, one handler per capability.
 */

import type { Plugin } from "@opencode/plugin"
import type { ToolEditor } from "./editor-types"
import { renderBacktest, runBacktest } from "./backtest"
import { formatModelRef, readConfig } from "./config"
import { describeRun, readReport, renderSummary, runAnalysis } from "./pipeline"
import { type ProbeInput, runProbe } from "./probe"
import { assertWorkerPermission } from "./permissions"
import { errorMarkdown } from "./render"
import type { ProgressEvent } from "./runtime"
import type { AnalyzeInput, BacktestInput } from "./types"

/** The structured half of a progress update, minus the human line. */
function progressMetadata(event: ProgressEvent): Record<string, unknown> {
  const metadata: Record<string, unknown> = { phase: event.phase }
  if (event.stage) metadata.stage = event.stage
  if (event.title) metadata.title = event.title
  if (event.group) metadata.group = event.group
  if (event.index !== undefined) metadata.index = event.index
  if (event.total !== undefined) metadata.total = event.total
  if (event.model) metadata.model = event.model
  return metadata
}

/** Tool metadata without the keys the run never filled. */
function compact(metadata: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(metadata).filter(([, value]) => value !== undefined))
}

export function registerTradingTools(ctx: Plugin.Context, editor: ToolEditor): void {
  const config = readConfig(ctx.options)
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
      const progress = async (event: ProgressEvent): Promise<void> => {
        await context.progress({ status: event.message, ...progressMetadata(event) })
      }
      try {
        await assertWorkerPermission(ctx, context.sessionID, {
          type: "tool",
          messageID: context.messageID,
          id: context.id,
        })
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
          metadata: compact({
            run_id: summary.runId,
            run_dir: summary.runDir,
            calls: summary.calls,
            stages: summary.stages.length,
            warnings: summary.warnings.length,
            ticker: summary.ticker,
            trade_date: summary.tradeDate,
            rating: summary.rating,
            elapsed_ms: summary.elapsedMs,
            stages_done: summary.stages.filter((stage) => stage.status === "ok").length,
            report_md: summary.reportMd,
            report_html: summary.reportHtml,
          }),
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
      const progress = async (event: ProgressEvent): Promise<void> => {
        await context.progress({ status: event.message, ...progressMetadata(event) })
      }
      try {
        await assertWorkerPermission(ctx, context.sessionID, {
          type: "tool",
          messageID: context.messageID,
          id: context.id,
        })
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
          metadata: compact({
            run_id: result.runId,
            sweep_dir: result.sweepDir,
            log_path: result.logPath,
            calls: result.calls,
            cells: result.cells.length,
            total: result.total,
            ran: result.cells.length,
            skipped: result.skipped,
            failed: result.cells.filter((cell) => cell.status === "failed").length,
            elapsed_ms: result.elapsedMs,
            warnings: result.warnings.length,
          }),
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
}
