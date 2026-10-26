/**
 * The memory pass: settle past decisions, generate reflections, inject the
 * lessons into the Portfolio Manager. Failures are warnings, never stage
 * failures.
 */

import type { Plugin } from "@opencode/plugin"
import { writeFile } from "node:fs/promises"
import { join } from "node:path"
import type { ModelRef, TradingConfig } from "./config"
import { formatModelRef } from "./config"
import { generateWithFallback } from "./generate"
import type {
  AnalyzeInput,
  MemoryApplyReply,
  MemoryBeginReply,
  MemoryEntry,
  MemoryFailure,
  WorkerReply,
} from "./types"
import { runWorker } from "./worker"
import type { PipelineHooks } from "./runtime"
import { formatReply, modelForRun, progress } from "./runtime"

/**
 * One reflection prompt: the graph's system and user messages, labelled the
 * same way the worker labels every other prompt.
 */
function reflectionPrompt(system: string, user: string): string {
  return `### system\n\n${system.trim()}\n\n### user\n\n${user.trim()}\n`
}

export interface MemoryOutcome {
  readonly due: number
  readonly settled: number
  readonly note: string
  readonly warnings: readonly string[]
  /** Reflection calls this pass made. */
  readonly calls: number
  readonly logPath?: string
}

function memoryEntries(value: unknown): MemoryEntry[] {
  if (!Array.isArray(value)) return []
  const entries: MemoryEntry[] = []
  for (const item of value) {
    if (!item || typeof item !== "object") continue
    const row = item as Record<string, unknown>
    const ticker = typeof row.ticker === "string" ? row.ticker : ""
    const date = typeof row.date === "string" ? row.date : ""
    const system = typeof row.system === "string" ? row.system : ""
    const user = typeof row.user === "string" ? row.user : ""
    if (!ticker || !date || !system || !user) continue
    entries.push({
      ticker,
      date,
      rating: typeof row.rating === "string" ? row.rating : "",
      raw_return: Number(row.raw_return ?? 0),
      alpha_return: Number(row.alpha_return ?? 0),
      holding_days: Number(row.holding_days ?? 0),
      system,
      user,
    })
  }
  return entries
}

/** Everything the memory pass reads; a backtest's final pass has no RunContext. */
export interface MemoryPassOptions {
  readonly ctx: Plugin.Context
  readonly config: TradingConfig
  readonly input: AnalyzeInput
  readonly hooks: PipelineHooks
  readonly runDir: string
  readonly worker: { directory: string; python: string; signal?: AbortSignal }
  readonly fallbacks: readonly ModelRef[]
}

/**
 * Settle past decisions beside the analysts, the way the graph's memory step
 * does: the worker decides what is due and renders the prompts, the plugin
 * generates the reflections, the worker writes them back and reads the lessons.
 *
 * Failures are warnings, never stage failures: a memory log that cannot be read
 * costs the run its history, not its analysis. Exported so a backtest can run
 * the same pass once more after its last cell, settling the cells it just added.
 */
export async function settleMemory(options: MemoryPassOptions): Promise<MemoryOutcome> {
  const warnings: string[] = []
  let calls = 0
  let begin: WorkerReply
  try {
    begin = await runWorker(
      { action: "memory-begin", runDir: options.runDir },
      options.worker,
    )
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    return { due: 0, settled: 0, note: "", warnings: [`memory settlement skipped: ${message}`], calls }
  }
  if (!begin.ok) {
    return {
      due: 0,
      settled: 0,
      note: "",
      warnings: [`memory settlement skipped: ${formatReply(begin.error)}`],
      calls,
    }
  }

  const payload = begin as MemoryBeginReply
  const entries = memoryEntries(payload.entries)
  const failures = Array.isArray(payload.failed) ? (payload.failed as MemoryFailure[]) : []
  for (const failure of failures) {
    warnings.push(`${failure.ticker} ${failure.date}: ${failure.reason}`)
  }

  const reflections: { ticker: string; date: string; text: string }[] = []
  if (entries.length) {
    const model = modelForRun(options.config, options.input, "memory", "quick")
    const candidates = [
      model,
      ...options.fallbacks.filter((item) => formatModelRef(item) !== formatModelRef(model)),
    ]
    await progress(options.hooks, {
      phase: "settle",
      message: `settling ${entries.length} past decision(s)`,
    })
    for (const entry of entries) {
      try {
        const result = await generateWithFallback(
          options.ctx,
          candidates,
          reflectionPrompt(entry.system, entry.user),
          { timeoutMs: options.config.callTimeoutMs, maxRetries: options.config.maxRetries },
        )
        calls += 1
        reflections.push({ ticker: entry.ticker, date: entry.date, text: result.text.trim() })
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        warnings.push(`reflection for ${entry.ticker} ${entry.date} failed: ${message}`)
      }
    }
    await writeFile(
      join(options.runDir, "memory", "reflections.json"),
      `${JSON.stringify({ reflections }, null, 2)}\n`,
      "utf8",
    )
  }

  let apply: WorkerReply
  try {
    apply = await runWorker(
      { action: "memory-apply", runDir: options.runDir },
      options.worker,
    )
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    return {
      due: entries.length,
      settled: 0,
      note: "",
      warnings: [...warnings, `memory settlement failed: ${message}`],
      calls,
    }
  }
  if (!apply.ok) {
    warnings.push(`memory settlement failed: ${formatReply(apply.error)}`)
    return { due: entries.length, settled: 0, note: "", warnings, calls }
  }

  const applied = apply as MemoryApplyReply
  const settled = Array.isArray(applied.settled) ? applied.settled.length : 0
  const note = typeof applied.note === "string" ? applied.note : ""
  if (note) warnings.push(note)
  return {
    due: entries.length,
    settled,
    note,
    warnings,
    calls,
    logPath: typeof applied.log_path === "string" ? applied.log_path : undefined,
  }
}
