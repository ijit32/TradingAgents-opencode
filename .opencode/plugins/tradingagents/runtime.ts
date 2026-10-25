/**
 * Shared runtime for one run: input normalization, the run context, and the
 * worker helpers every stage uses.
 *
 * Keeping these here means the stage, memory, and report modules depend on one
 * small surface instead of on each other.
 */

import type { Plugin } from "@opencode/plugin"
import { readFile } from "node:fs/promises"
import { join } from "node:path"
import { markStage, writeCheckpoint } from "./checkpoint"
import type { ModelRef, TradingConfig } from "./config"
import { modelForStage, parseModelRef } from "./config"
import type {
  AnalyzeInput,
  Checkpoint,
  PlanGroup,
  PlanStage,
  StageRecord,
  WorkerReply,
} from "./types"
import { runWorker } from "./worker"

export const ANALYST_KEYS = ["market", "social", "news", "fundamentals"] as const

export interface StageSpec {
  readonly stage: string
  /** Request analyst key, for report stages only. */
  readonly analyst?: string
  readonly title: string
  readonly tier: "quick" | "deep"
  readonly kind: "report" | "argument" | "structured"
  readonly group: string
  /** `final_state.json` key this stage fills, when it has one. */
  readonly reportKey?: string
}

export interface PipelineHooks {
  /** Progress line for the user; failures here must not fail the run. */
  readonly progress?: (message: string) => Promise<void> | void
  readonly signal?: AbortSignal
}

export class PipelineError extends Error {
  readonly stage?: string

  constructor(message: string, stage?: string) {
    super(message)
    this.name = "PipelineError"
    this.stage = stage
  }
}

/** Everything a run is, before the worker normalizes it. */
interface RequestPayload {
  readonly ticker: string
  readonly trade_date: string
  readonly asset_type: string
  readonly depth: number
  readonly analysts: readonly string[]
  readonly output_language: string
  readonly portfolio: string
  readonly models: Readonly<Record<string, string>>
  readonly results_dir: string
  readonly memory_log_path: string
}

function today(): string {
  const now = new Date()
  const pad = (value: number): string => String(value).padStart(2, "0")
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function tickerOf(input: AnalyzeInput): string {
  const raw = input.ticker?.trim()
  if (!raw) throw new PipelineError("ticker is required")
  if (!/^[A-Za-z0-9._\-^=+]+$/.test(raw)) {
    throw new PipelineError(
      `ticker '${raw}' contains characters that are not allowed in a symbol`,
    )
  }
  return raw.toUpperCase()
}

function dateOf(input: AnalyzeInput): string {
  const raw = input.trade_date?.trim() || today()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    throw new PipelineError(`trade_date must be YYYY-MM-DD, got '${raw}'`)
  }
  const parsed = new Date(`${raw}T00:00:00Z`)
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== raw) {
    throw new PipelineError(`trade_date is not a real date: '${raw}'`)
  }
  if (raw > today()) throw new PipelineError(`trade_date cannot be in the future: ${raw}`)
  return raw
}

function depthOf(input: AnalyzeInput): number {
  const depth = input.depth === undefined ? 1 : Math.trunc(input.depth)
  if (![1, 2, 3].includes(depth)) throw new PipelineError("depth must be 1, 2, or 3")
  return depth
}

function analystsOf(input: AnalyzeInput): string[] {
  if (!input.analysts?.length) return [...ANALYST_KEYS]
  const wanted = new Set(input.analysts.map((item) => String(item).toLowerCase()))
  const unknown = [...wanted].filter((key) => !(ANALYST_KEYS as readonly string[]).includes(key))
  if (unknown.length) throw new PipelineError(`unknown analyst(s): ${unknown.join(", ")}`)
  const selected = ANALYST_KEYS.filter((key) => wanted.has(key))
  if (!selected.length) throw new PipelineError("analysts cannot be empty")
  return selected
}

/**
 * Portfolio input as the worker receives it: a JSON object when the caller gave
 * one (so `PortfolioContext.render` can label it), prose otherwise.
 */
function portfolioOf(input: AnalyzeInput): string {
  if (input.portfolio === undefined || input.portfolio === null) return ""
  if (typeof input.portfolio === "string") return input.portfolio.trim()
  return JSON.stringify(input.portfolio)
}

/** Stage override → per-run override → tier argument → plugin default. */
export function modelForRun(
  config: TradingConfig,
  input: AnalyzeInput,
  stage: string,
  tier: "quick" | "deep",
): ModelRef {
  if (input.model?.trim()) return parseModelRef(input.model)
  const configured = config.stageModels[stage]
  if (configured) return configured
  const perRun = tier === "deep" ? input.deep_model : input.quick_model
  if (perRun?.trim()) return parseModelRef(perRun)
  return modelForStage(config, stage, tier)
}

/** Check cancellation between steps, where it is cheap and safe. */
function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new PipelineError("run was cancelled")
}

async function progress(hooks: PipelineHooks, message: string): Promise<void> {
  if (!hooks.progress) return
  try {
    await hooks.progress(message)
  } catch {
    // A failed progress update is not a run failure.
  }
}

async function readTextOrNull(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8")
  } catch {
    return null
  }
}

function formatReply(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value)
}

/** `sha256:` or a placeholder when the worker did not report one. */
function replyHash(reply: WorkerReply): string {
  return typeof reply.hash === "string" ? reply.hash : `unhashed:${Date.now()}`
}

function titleOf(stage: string): string {
  return stage
    .replace(/_/g, " ")
    .replace(/\b[a-z]/g, (letter) => letter.toUpperCase())
}

function toSpec(stage: PlanStage): StageSpec {
  return {
    stage: stage.stage,
    analyst: stage.analyst,
    title: titleOf(stage.stage),
    tier: stage.tier,
    kind: stage.kind,
    group: stage.group,
    reportKey: stage.report_key,
  }
}

/**
 * Serialized checkpoint writes.
 *
 * Analyst stages finish concurrently, so several commits can be in flight at
 * once. `markStage` runs synchronously against the latest in-memory value, and
 * only the writes queue — no record is lost, and the last file on disk is the
 * newest superset.
 */
class CheckpointStore {
  value: Checkpoint
  private chain: Promise<void> = Promise.resolve()

  constructor(
    private readonly runDir: string,
    initial: Checkpoint,
  ) {
    this.value = initial
  }

  async commit(stage: string, record: StageRecord, warnings: readonly string[] = []): Promise<void> {
    this.value = markStage(this.value, stage, record, warnings)
    const snapshot = this.value
    this.chain = this.chain.then(() => writeCheckpoint(this.runDir, snapshot))
    await this.chain
  }

  async replace(next: Checkpoint): Promise<void> {
    this.value = next
    const snapshot = next
    this.chain = this.chain.then(() => writeCheckpoint(this.runDir, snapshot))
    await this.chain
  }
}

interface RunContext {
  readonly ctx: Plugin.Context
  readonly config: TradingConfig
  readonly input: AnalyzeInput
  readonly hooks: PipelineHooks
  readonly runDir: string
  readonly payload: RequestPayload
  readonly worker: { directory: string; python: string; signal?: AbortSignal }
  readonly store: CheckpointStore
  /** Every stage of this run, in the worker's order. */
  order: readonly string[]
  /** Tried after the stage's own model; unknown ones are dropped up front. */
  fallbacks: ModelRef[]
  calls: number
}

async function initRun(context: RunContext): Promise<WorkerReply> {
  const reply = await runWorker(
    {
      action: "init",
      runDir: context.runDir,
      jsonArgs: { request: context.payload },
    },
    context.worker,
  )
  if (!reply.ok) throw new PipelineError(`worker init failed: ${formatReply(reply.error)}`)
  return reply
}

async function readPlan(context: RunContext): Promise<{ groups: PlanGroup[]; order: string[] }> {
  const reply = await runWorker({ action: "plan", runDir: context.runDir }, context.worker)
  if (!reply.ok) throw new PipelineError(`worker plan failed: ${formatReply(reply.error)}`)
  const groups = Array.isArray(reply.groups) ? (reply.groups as PlanGroup[]) : []
  const order = Array.isArray(reply.order)
    ? reply.order.map(String)
    : groups.flatMap((group) => group.stages.map((stage) => stage.stage))
  if (!order.length) throw new PipelineError("the worker planned no stages")
  return { groups, order }
}

/** Collect one analyst's pack unless it is already on disk. */
async function ensurePack(context: RunContext, spec: StageSpec): Promise<void> {
  if (spec.kind !== "report" || !spec.analyst) return
  const path = join(context.runDir, "data", `${spec.analyst}.md`)
  const forced =
    context.input.rerun === spec.stage || context.input.rerun_from === spec.stage
  if (!forced && (await readTextOrNull(path))) return

  await progress(context.hooks, `collecting ${spec.title.toLowerCase()} data`)
  const reply = await runWorker(
    { action: "collect", runDir: context.runDir, analyst: spec.analyst },
    context.worker,
  )
  if (!reply.ok) {
    throw new PipelineError(`collect failed: ${formatReply(reply.error)}`, spec.stage)
  }
  const notes = Array.isArray(reply.notes) ? reply.notes.map(String) : []
  if (notes.length) await progress(context.hooks, `${spec.title}: data note — ${notes.join("; ")}`)
}

export {
  CheckpointStore,
  type RequestPayload,
  type RunContext,
  analystsOf,
  dateOf,
  depthOf,
  ensurePack,
  formatReply,
  initRun,
  portfolioOf,
  progress,
  readPlan,
  readTextOrNull,
  replyHash,
  throwIfAborted,
  tickerOf,
  titleOf,
  toSpec,
}
