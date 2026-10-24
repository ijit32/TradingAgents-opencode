/**
 * The backtest sweep: many runs over one date grid, one memory log as results.
 *
 * `run_backtest` in the repo runs a whole grid in-process. Here the plugin owns
 * the model calls, so the worker only plans the grid (`backtest-plan`) and
 * summarises the sweep's memory log (`backtest-summary`); this module walks the
 * plan, runs one ordinary analysis per cell, and settles whatever became due at
 * the end.
 *
 * Three properties matter, and each comes from reusing the single-run path:
 *
 * - **Isolation.** Every cell writes under `<sweep>/runs`, with the sweep's own
 *   `results_dir` and `trading_memory.md`, exactly where `run_backtest` puts
 *   them. No cell can read another sweep's history.
 * - **Resume for free.** A cell whose decision is already in the sweep log is
 *   skipped by the worker's plan, so rerunning the same `run_id` continues
 *   instead of repeating model calls.
 * - **Settlement across cells.** Each cell records its decision as pending; a
 *   later cell settles it when its window has traded. One final pass on the last
 *   cell's directory settles whatever the last few cells left pending, which is
 *   what makes the summary score the tail of the sweep.
 */

import type { Plugin } from "@opencode/plugin"
import { mkdir } from "node:fs/promises"
import { join } from "node:path"
import { expandHome, readConfig } from "./config"
import { resolveCandidates, resolveModel } from "./models"
import {
  type PipelineHooks,
  PipelineError,
  modelForRun,
  runAnalysis,
  settleMemory,
} from "./pipeline"
import type {
  AnalyzeInput,
  BacktestCell,
  BacktestCellResult,
  BacktestInput,
  BacktestPlanReply,
  BacktestResult,
  BacktestSummaryReply,
} from "./types"
import { runWorker } from "./worker"

const DEFAULT_EVERY_N_DAYS = 7

function pad(value: number): string {
  return String(value).padStart(2, "0")
}

function today(): string {
  const now = new Date()
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/** `backtest_20261024_153012` — sortable, and safe as a path segment. */
function newSweepId(): string {
  const now = new Date()
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  return `backtest_${stamp}`
}

function dateOf(value: string | undefined, name: string): string {
  const raw = value?.trim()
  if (!raw) throw new PipelineError(`${name} is required (YYYY-MM-DD)`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    throw new PipelineError(`${name} must be YYYY-MM-DD, got '${raw}'`)
  }
  return raw
}

function tickersOf(input: BacktestInput): string[] {
  const listed = input.tickers?.length ? input.tickers : input.ticker ? [input.ticker] : []
  const clean = [...new Set(listed.map((item) => String(item).trim().toUpperCase()).filter(Boolean))]
  if (!clean.length) throw new PipelineError("backtest needs at least one ticker")
  for (const ticker of clean) {
    if (!/^[A-Za-z0-9._\-^=+]+$/.test(ticker)) {
      throw new PipelineError(`ticker '${ticker}' contains characters that are not allowed in a symbol`)
    }
  }
  return clean
}

function everyOf(input: BacktestInput): number {
  const every = input.every_n_days === undefined ? DEFAULT_EVERY_N_DAYS : Math.trunc(input.every_n_days)
  if (!Number.isFinite(every) || every < 1) {
    throw new PipelineError("every_n_days must be a positive integer")
  }
  return every
}

function cellsOf(value: unknown): BacktestCell[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): BacktestCell[] => {
    if (!item || typeof item !== "object") return []
    const row = item as Record<string, unknown>
    const ticker = typeof row.ticker === "string" ? row.ticker : ""
    const date = typeof row.date === "string" ? row.date : ""
    return ticker && date ? [{ ticker, date }] : []
  })
}

async function progress(hooks: PipelineHooks, message: string): Promise<void> {
  if (!hooks.progress) return
  try {
    await hooks.progress(message)
  } catch {
    // A failed progress update is not a sweep failure.
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** The analysis input for one cell: same options, isolated paths, memory forced on. */
function cellInput(
  input: BacktestInput,
  cell: BacktestCell,
  sweepDir: string,
  logPath: string,
): AnalyzeInput {
  return {
    ticker: cell.ticker,
    trade_date: cell.date,
    depth: input.depth,
    analysts: input.analysts,
    model: input.model,
    quick_model: input.quick_model,
    deep_model: input.deep_model,
    output_language: input.output_language,
    html: input.html,
    run_root: join(sweepDir, "runs"),
    results_dir: sweepDir,
    memory_log_path: logPath,
    // The sweep's memory log is the results table: a cell that does not record
    // its decision would be rerun forever.
    memory: true,
  }
}

/**
 * Run every cell the sweep has not already logged.
 *
 * A failed cell is recorded and the sweep continues: one vendor hiccup must not
 * throw away the cells before it. Cancellation is the exception — it stops the
 * sweep immediately, leaving the completed cells in the memory log.
 */
export async function runBacktest(
  ctx: Plugin.Context,
  input: BacktestInput,
  hooks: PipelineHooks = {},
): Promise<BacktestResult> {
  const config = readConfig(ctx.options)
  const started = Date.now()
  const base = expandHome(config.runRoot)
  const start = dateOf(input.start, "start")
  const end = dateOf(input.end || today(), "end")
  if (start > end) throw new PipelineError(`start ${start} is after end ${end}`)
  const every = everyOf(input)
  const tickers = tickersOf(input)
  const runId = input.run_id?.trim() || newSweepId()

  await mkdir(base, { recursive: true })
  const worker = {
    directory: ctx.location.directory,
    python: config.python,
    signal: hooks.signal,
  }

  const planReply = await runWorker(
    {
      action: "backtest-plan",
      runDir: base,
      jsonArgs: { base, run_id: runId, start, end, every_n_days: every, tickers },
    },
    worker,
  )
  if (!planReply.ok) {
    throw new PipelineError(`backtest plan failed: ${messageOf(planReply.error)}`)
  }
  const plan = planReply as BacktestPlanReply
  const sweepId = plan.run_id || runId
  const sweepDir = plan.sweep_dir || join(base, "backtest", sweepId)
  const logPath = plan.log_path || join(sweepDir, "trading_memory.md")
  const todo = cellsOf(plan.todo)
  const total = Number(plan.total ?? todo.length)
  const skipped = Number(plan.skipped ?? 0)

  await progress(
    hooks,
    `${total} cell(s) in ${sweepId}; ${todo.length} to run${skipped ? `, ${skipped} already logged` : ""}`,
  )

  const warnings: string[] = []
  const cells: BacktestCellResult[] = []
  let calls = 0

  for (let index = 0; index < todo.length; index += 1) {
    if (hooks.signal?.aborted) throw new PipelineError("backtest was cancelled")
    const cell = todo[index]
    await progress(hooks, `cell ${index + 1}/${todo.length}: ${cell.ticker} ${cell.date}`)
    try {
      const summary = await runAnalysis(
        ctx,
        cellInput(input, cell, sweepDir, logPath),
        hooks,
      )
      calls += summary.calls
      warnings.push(...summary.warnings)
      cells.push({
        ticker: cell.ticker,
        date: cell.date,
        status: "ok",
        rating: summary.rating,
        calls: summary.calls,
        runDir: summary.runDir,
      })
    } catch (error) {
      if (hooks.signal?.aborted) throw error
      cells.push({
        ticker: cell.ticker,
        date: cell.date,
        status: "failed",
        error: messageOf(error),
      })
    }
  }

  // Settle the tail: cells added late have no later cell to trigger their
  // reflection, so one pass on the last run directory closes the window.
  const last = [...cells].reverse().find((cell) => cell.status === "ok" && cell.runDir)
  if (last?.runDir) {
    try {
      const lastInput = cellInput(input, { ticker: last.ticker, date: last.date }, sweepDir, logPath)
      const memoryModel = modelForRun(config, lastInput, "memory", "quick")
      await resolveModel(ctx, memoryModel)
      const fallbacks = await resolveCandidates(ctx, config.fallbackModels)
      await progress(hooks, "settling the sweep's remaining decisions")
      const outcome = await settleMemory({
        ctx,
        config,
        input: lastInput,
        hooks,
        runDir: last.runDir,
        worker,
        fallbacks,
      })
      calls += outcome.calls
      warnings.push(...outcome.warnings)
    } catch (error) {
      warnings.push(`final memory pass failed: ${messageOf(error)}`)
    }
  }

  let markdown = ""
  try {
    const summaryReply = await runWorker(
      { action: "backtest-summary", runDir: base, jsonArgs: { base, run_id: sweepId } },
      worker,
    )
    if (summaryReply.ok) {
      const reply = summaryReply as BacktestSummaryReply
      markdown = typeof reply.markdown === "string" ? reply.markdown : ""
    } else {
      warnings.push(`backtest summary failed: ${messageOf(summaryReply.error)}`)
    }
  } catch (error) {
    warnings.push(`backtest summary failed: ${messageOf(error)}`)
  }

  return {
    runId: sweepId,
    sweepDir,
    logPath,
    total,
    skipped,
    cells,
    calls,
    elapsedMs: Date.now() - started,
    markdown,
    warnings,
  }
}

/** The tool/command reply: sweep identity, per-cell outcomes, then the scores. */
export function renderBacktest(result: BacktestResult): string {
  const failed = result.cells.filter((cell) => cell.status === "failed").length
  const ran = result.cells.length
  const lines = [
    "### TradingAgents backtest",
    "",
    `- Sweep: \`${result.runId}\``,
    `- Directory: \`${result.sweepDir}\``,
    `- Memory log: \`${result.logPath}\``,
    `- Cells: ${result.total} planned · ${ran} ran · ${result.skipped} already logged · ${failed} failed`,
    `- Model calls: ${result.calls} · ${(result.elapsedMs / 1000).toFixed(1)}s`,
  ]

  if (ran) {
    lines.push("", "| # | Ticker | Date | Status | Rating | Calls |", "|---|---|---|---|---|---|")
    result.cells.forEach((cell, index) => {
      lines.push(
        `| ${index + 1} | ${cell.ticker} | ${cell.date} | ${cell.status} | ${cell.rating ?? "—"} | ${cell.calls ?? "—"} |`,
      )
    })
  } else {
    lines.push("", "Every cell in the grid was already logged; the summary below covers them.")
  }

  for (const cell of result.cells) {
    if (cell.status === "failed" && cell.error) {
      lines.push("", `**${cell.ticker} ${cell.date} failed** — ${cell.error}`)
    }
  }

  if (result.markdown.trim()) lines.push("", result.markdown.trim())

  if (result.warnings.length) {
    lines.push("", "**Warnings**", "")
    for (const warning of result.warnings) lines.push(`- ${warning}`)
  }
  return lines.join("\n")
}