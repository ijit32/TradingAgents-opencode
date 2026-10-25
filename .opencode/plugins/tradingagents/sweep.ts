/**
 * Pure helpers for a backtest sweep: ids, dates, tickers, cells, and the
 * analysis input for one cell. No worker calls and no model calls.
 */

import { join } from "node:path"
import { isSafeTicker, PipelineError } from "./runtime"
import type { AnalyzeInput, BacktestCell, BacktestInput } from "./types"

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
    if (!isSafeTicker(ticker)) {
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

export {
  cellInput,
  cellsOf,
  dateOf,
  everyOf,
  messageOf,
  newSweepId,
  tickersOf,
  today,
}
