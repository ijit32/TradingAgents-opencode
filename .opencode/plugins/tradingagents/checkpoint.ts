/**
 * Checkpointing and run-directory naming.
 *
 * The checkpoint is the resume contract: a stage is skipped when it succeeded
 * and the prompt hash it ran on still matches. Because every stage's prompt is
 * rendered from the run's data and the earlier stages' reports, any upstream
 * change shifts the downstream hashes and invalidates exactly the right work.
 *
 * Writes are atomic (`tmp` + rename), so a crash mid-stage leaves the previous
 * checkpoint intact.
 */

import { createHash } from "node:crypto"
import { existsSync } from "node:fs"
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises"
import { join } from "node:path"
import type { Checkpoint, StageRecord } from "./types"

const VERSION = 1 as const

export function hashText(text: string): string {
  return `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`
}

/** Key-order-independent JSON hash, so a rewritten request is not a new run. */
export function hashJson(value: unknown): string {
  return hashText(stableStringify(value))
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null"
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`).join(",")}}`
}

export function checkpointPath(runDir: string): string {
  return join(runDir, "checkpoint.json")
}

export async function readCheckpoint(runDir: string): Promise<Checkpoint | null> {
  const path = checkpointPath(runDir)
  if (!existsSync(path)) return null
  try {
    const parsed = JSON.parse(await readFile(path, "utf8")) as Checkpoint
    if (!parsed || typeof parsed !== "object" || parsed.version !== VERSION) return null
    return { ...parsed, stages: parsed.stages ?? {}, warnings: parsed.warnings ?? [] }
  } catch {
    // A truncated checkpoint is not fatal: the stages it would have described
    // are still on disk, and they will simply run again.
    return null
  }
}

export async function writeCheckpoint(runDir: string, checkpoint: Checkpoint): Promise<void> {
  await mkdir(runDir, { recursive: true })
  const path = checkpointPath(runDir)
  const tmp = `${path}.${process.pid}.${Date.now()}.tmp`
  await writeFile(tmp, `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8")
  try {
    await rename(tmp, path)
  } catch (error) {
    await unlink(tmp).catch(() => undefined)
    throw error
  }
}

export function createCheckpoint(runId: string, requestHash: string): Checkpoint {
  return { version: VERSION, run_id: runId, request_hash: requestHash, stages: {}, warnings: [] }
}

/** Record a stage's outcome, preserving earlier stages. */
export function markStage(
  checkpoint: Checkpoint,
  stage: string,
  record: StageRecord,
  warnings: readonly string[] = [],
): Checkpoint {
  return {
    ...checkpoint,
    stages: { ...checkpoint.stages, [stage]: record },
    warnings: [...new Set([...checkpoint.warnings, ...warnings])],
  }
}

/**
 * Succeeding with the same prompt hash is the whole resume rule.
 *
 * `skipped` counts as success: the record is rewritten to `skipped` on every
 * resume so the current run's summary can say so, and without this the next
 * resume would see `skipped` instead of `ok` and rerun everything.
 */
export function shouldSkip(checkpoint: Checkpoint | null, stage: string, hash: string): boolean {
  const record = checkpoint?.stages[stage]
  if (!record || record.hash !== hash) return false
  return record.status === "ok" || record.status === "skipped"
}

/**
 * Drop `stage` and everything in `order` after it, so `--rerun-from` reruns the
 * tail instead of the whole run.
 */
export function invalidateFrom(
  checkpoint: Checkpoint,
  order: readonly string[],
  from: string,
): Checkpoint {
  const index = order.indexOf(from)
  if (index < 0) return checkpoint
  const doomed = new Set(order.slice(index))
  const stages = Object.fromEntries(
    Object.entries(checkpoint.stages).filter(([stage]) => !doomed.has(stage)),
  )
  return { ...checkpoint, stages }
}

/** `NVDA_2026-10-23_20261024_1130` — the Python worker's stamp format. */
export function newRunId(ticker: string, tradeDate: string, now = new Date()): string {
  const pad = (value: number): string => String(value).padStart(2, "0")
  const stamp =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}` +
    `_${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}`
  return `${ticker}_${tradeDate}_${stamp}`
}

/** Never overwrite a run: resume is explicit, so a name clash gets a suffix. */
export function uniqueRunDir(base: string, runId: string): string {
  const first = join(base, runId)
  if (!existsSync(first)) return first
  for (let index = 2; index < 100; index += 1) {
    const candidate = join(base, `${runId}_${index}`)
    if (!existsSync(candidate)) return candidate
  }
  throw new Error(`too many runs named ${runId}; clean up ${base}`)
}

/** `stages/<stage>.md`, the validated report. */
export function stageReportPath(runDir: string, stage: string): string {
  return join(runDir, "stages", `${stage}.md`)
}

/** The name a stage's report takes in `final_state.json`, matching reporting.py. */
export const STAGE_STATE_KEY: Readonly<Record<string, string>> = {
  market_analyst: "market_report",
  sentiment_analyst: "sentiment_report",
  news_analyst: "news_report",
  fundamentals_analyst: "fundamentals_report",
  research_manager: "investment_plan",
  trader: "trader_investment_plan",
  portfolio_manager: "final_trade_decision",
}
