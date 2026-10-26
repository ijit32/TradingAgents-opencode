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
 *
 * The pure parts (ids, grid, cell input) live in `sweep.ts`.
 */

import type { Plugin } from "@opencode/plugin"
import { mkdir } from "node:fs/promises"
import { join } from "node:path"
import { expandHome, readConfig } from "./config"
import { resolveCandidates, resolveModel } from "./models"
import { formatDuration } from "./report"
import type { PipelineHooks } from "./runtime"
import { PipelineError, modelForRun, progress } from "./runtime"
import { runAnalysis, settleMemory } from "./pipeline"
import {
  cellInput,
  cellsOf,
  dateOf,
  everyOf,
  messageOf,
  newSweepId,
  tickersOf,
  today,
} from "./sweep"
import type {
  BacktestCellResult,
  BacktestInput,
  BacktestPlanReply,
  BacktestResult,
  BacktestSummaryReply,
} from "./types"
import { runWorker } from "./worker"

/** Warning bullets shown before the remainder is folded into one line. */
const MAX_BACKTEST_WARNINGS = 6

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

  await progress(hooks, {
    phase: "note",
    message: `${total} cell(s) in ${sweepId}; ${todo.length} to run${skipped ? `, ${skipped} already logged` : ""}`,
  })

  const warnings: string[] = []
  const cells: BacktestCellResult[] = []
  let calls = 0

  for (let index = 0; index < todo.length; index += 1) {
    if (hooks.signal?.aborted) throw new PipelineError("backtest was cancelled")
    const cell = todo[index]
    await progress(hooks, {
      phase: "note",
      index: index + 1,
      total: todo.length,
      message: `cell ${index + 1}/${todo.length}: ${cell.ticker} ${cell.date}`,
    })
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
      await progress(hooks, {
        phase: "settle",
        message: "settling the sweep's remaining decisions",
      })
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

/** The tool/command reply: sweep identity, what ran, then the scores. */
export function renderBacktest(result: BacktestResult): string {
  const ran = result.cells.length
  const failedCells = result.cells.filter((cell) => cell.status === "failed")
  const lines = [
    `### Backtest \`${result.runId}\``,
    "",
    `\`${result.sweepDir}\` · ${result.total} cells · ${ran} ran · ${result.skipped} already logged · ${failedCells.length} failed`,
    "",
    `Model calls ${result.calls} · ${formatDuration(result.elapsedMs)}`,
  ]

  const markdown = result.markdown.trim()
  if (markdown) {
    lines.push("", markdown)
  } else if (ran) {
    lines.push("", "| # | Ticker | Date | Rating | Calls |", "|---|---|---|---|---|")
    result.cells.forEach((cell, index) => {
      lines.push(
        `| ${index + 1} | ${cell.ticker} | ${cell.date} | ${cell.rating ?? "—"} | ${cell.calls ?? "—"} |`,
      )
    })
  }

  if (failedCells.length) {
    lines.push("", `**Failed cells (${failedCells.length})**`)
    for (const cell of failedCells) {
      lines.push(`- ${cell.ticker} ${cell.date} — ${cell.error ?? "failed"}`)
    }
  }

  if (result.warnings.length) {
    lines.push("", `**Warnings (${result.warnings.length})**`)
    for (const warning of result.warnings.slice(0, MAX_BACKTEST_WARNINGS)) {
      lines.push(`- ${warning}`)
    }
    if (result.warnings.length > MAX_BACKTEST_WARNINGS) {
      const more = result.warnings.length - MAX_BACKTEST_WARNINGS
      lines.push(`_+${more} more in \`${result.sweepDir}\`_`)
    }
  }
  return lines.join("\n")
}
