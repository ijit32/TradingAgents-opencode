/**
 * The analysis pipeline: one run, one run directory, one checkpoint.
 *
 * The worker owns the stage manifest (`plan`): analyst reports, the two
 * debates, and the three structured decisions, in the graph's order. Groups
 * marked parallel prepare sequentially and generate concurrently; the debate
 * and decision groups run one stage at a time, because each prompt reads the
 * previous turn. Every step that already exists in the repo — point-in-time
 * collection, prompt rendering, validation — is a Python worker action; this
 * module owns the order, the model choice, the retry/repair policy, and the
 * resume decision.
 *
 * A stage is skipped when it succeeded and its prompt hash is unchanged, which
 * is what makes a second run of the same ticker and date free of model calls.
 */

import type { Plugin } from "@opencode/plugin"
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises"
import { isAbsolute, join, relative, resolve } from "node:path"
import {
  createCheckpoint,
  hashJson,
  invalidateFrom,
  markStage,
  newRunId,
  readCheckpoint,
  shouldSkip,
  uniqueRunDir,
  writeCheckpoint,
} from "./checkpoint"
import type { ModelRef, TradingConfig } from "./config"
import { expandHome, formatModelRef, modelForStage, parseModelRef, readConfig } from "./config"
import { GenerateError, generateWithFallback } from "./generate"
import { resolveCandidates, resolveModel } from "./models"
import type {
  AnalyzeInput,
  Checkpoint,
  MemoryApplyReply,
  MemoryBeginReply,
  MemoryEntry,
  MemoryFailure,
  PlanGroup,
  PlanStage,
  RunSummary,
  SettleReply,
  StageRecord,
  WorkerReply,
} from "./types"
import { EXIT_VALIDATION, WorkerError, runWorker } from "./worker"

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

interface StagePlan {
  readonly spec: StageSpec
  readonly model: ModelRef
  readonly hash: string
  readonly promptText: string
  /** The worker appended a JSON schema, so a rejection can be repaired twice. */
  readonly structured: boolean
}

const REPAIR_HEADER =
  "\n\n### Repair\n\nYour previous answer was rejected by the validator:\n\n"

const REPAIR_FOOTER =
  "\n\nWrite the complete answer again. Do not explain the rejection and do not " +
  "apologize — return only the requested output.\n"

/** Collect the pack, render the prompt, and decide whether this stage can skip. */
async function prepareStage(
  context: RunContext,
  spec: StageSpec,
  model: ModelRef,
): Promise<StagePlan | "skipped"> {
  const stage = spec.stage
  const forced =
    context.input.rerun === stage ||
    (context.input.rerun_from
      ? context.order.indexOf(stage) >= context.order.indexOf(context.input.rerun_from)
      : false)

  await ensurePack(context, spec)
  throwIfAborted(context.hooks.signal)

  await progress(context.hooks, `rendering ${spec.title.toLowerCase()} prompt`)
  const promptReply = await runWorker(
    { action: "prompt", runDir: context.runDir, stage },
    context.worker,
  )
  if (!promptReply.ok) {
    throw new PipelineError(`prompt failed: ${formatReply(promptReply.error)}`, stage)
  }
  const hash = replyHash(promptReply)
  const promptPath = join(context.runDir, String(promptReply.prompt_path))
  const promptText = await readFile(promptPath, "utf8")

  if (!forced && shouldSkip(context.store.value, stage, hash)) {
    await progress(context.hooks, `${spec.title}: up to date, skipping`)
    await context.store.commit(stage, {
      status: "skipped",
      hash,
      attempts: 0,
      model: formatModelRef(model),
      out: `stages/${stage}.md`,
      ms: 0,
      warnings: [],
    })
    return "skipped"
  }

  return { spec, model, hash, promptText, structured: Boolean(promptReply.schema) }
}

interface StageOutcome {
  readonly record: StageRecord
  readonly rating?: string
}

/**
 * Generate, validate, and repair one stage.
 *
 * A validator rejection gets a fresh call on the same prompt plus the
 * validator's complaints: once for prose, twice for a structured decision.
 * If every attempt fails, the validator runs lenient so a terse answer still
 * lands on disk with a review marker instead of being lost.
 */
async function executeStage(context: RunContext, plan: StagePlan): Promise<StageOutcome> {
  const { spec, model, hash, promptText } = plan
  const stage = spec.stage
  const startedAt = Date.now()
  const title = spec.title.toLowerCase()

  throwIfAborted(context.hooks.signal)
  await progress(context.hooks, `generating ${title} with ${formatModelRef(model)}`)

  const candidates = [
    model,
    ...context.fallbacks.filter((item) => formatModelRef(item) !== formatModelRef(model)),
  ]

  let text: string
  let attempts: number
  let usedModel = model
  try {
    const result = await generateWithFallback(context.ctx, candidates, promptText, {
      timeoutMs: context.config.callTimeoutMs,
      maxRetries: context.config.maxRetries,
    })
    text = result.text
    attempts = result.attempts
    usedModel = parseModelRef(result.model)
    context.calls += 1
  } catch (error) {
    const message =
      error instanceof GenerateError
        ? `${error.kind}: ${error.message}`
        : error instanceof Error
          ? error.message
          : String(error)
    const record: StageRecord = {
      status: "failed",
      hash,
      attempts: error instanceof GenerateError ? error.attempts : 0,
      model: formatModelRef(model),
      out: "",
      ms: Date.now() - startedAt,
      error: message,
    }
    await context.store.commit(stage, record, [`${stage} failed: ${message}`])
    throw new PipelineError(`${spec.title} generation failed — ${message}`, stage)
  }

  const outPath = join(context.runDir, "stages", `${stage}.out.txt`)
  const warnings: string[] = []
  const maxRepairs = plan.structured ? 2 : 1
  let validated: WorkerReply | null = null
  let lastErrors: readonly string[] = []

  for (let repair = 0; ; repair += 1) {
    await writeFile(outPath, text, "utf8")
    await progress(context.hooks, `validating ${title} report`)
    try {
      validated = await runWorker(
        { action: "validate", runDir: context.runDir, stage },
        context.worker,
      )
      break
    } catch (error) {
      if (!(error instanceof WorkerError) || error.code !== EXIT_VALIDATION) {
        warnings.push(
          `${stage} validation could not run: ${error instanceof Error ? error.message : String(error)}`,
        )
        validated = null
        break
      }
      lastErrors = error.validationErrors
      validated = null
      if (repair >= maxRepairs) break

      await progress(context.hooks, `${spec.title}: repair ${repair + 1} of ${maxRepairs}`)
      throwIfAborted(context.hooks.signal)
      const repairPrompt = `${promptText}${REPAIR_HEADER}${lastErrors
        .map((item) => `- ${item}`)
        .join("\n")}${REPAIR_FOOTER}`
      try {
        const repaired = await generateWithFallback(context.ctx, candidates, repairPrompt, {
          timeoutMs: context.config.callTimeoutMs,
          maxRetries: context.config.maxRetries,
        })
        context.calls += 1
        attempts += repaired.attempts
        usedModel = parseModelRef(repaired.model)
        text = repaired.text
      } catch (error) {
        warnings.push(
          `${stage} repair failed: ${error instanceof Error ? error.message : String(error)}`,
        )
        break
      }
    }
  }

  if (!validated) {
    warnings.push(...(lastErrors.length ? lastErrors : ["output did not validate"]))
    await progress(context.hooks, `${spec.title}: accepting output with a review marker`)
    validated = await runWorker(
      { action: "validate", runDir: context.runDir, stage, lenient: true },
      context.worker,
    )
    warnings.push(`${stage} accepted leniently; the report is marked for review`)
  }

  const validatedWarnings = Array.isArray(validated.warnings)
    ? validated.warnings.map(String)
    : []
  warnings.push(...validatedWarnings)

  const record: StageRecord = {
    status: "ok",
    hash,
    attempts,
    model: formatModelRef(usedModel),
    out: `stages/${stage}.md`,
    ms: Date.now() - startedAt,
    warnings,
  }
  await context.store.commit(stage, record, warnings)
  await progress(context.hooks, `${spec.title}: done`)
  const rating = typeof validated.rating === "string" ? validated.rating : undefined
  return { record, rating }
}

/** Run `worker` over `items` with at most `limit` in flight; preserves order. */
async function runWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length)
  let next = 0
  const lanes = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    for (;;) {
      const index = next
      next += 1
      if (index >= items.length) return
      results[index] = await worker(items[index])
    }
  })
  await Promise.all(lanes)
  return results
}

/** The parent for this run: a per-call override (a sweep), else the plugin root. */
export function runRootOf(config: TradingConfig, input: AnalyzeInput): string {
  const override = input.run_root?.trim()
  return expandHome(override || config.runRoot)
}

interface ResolvedRunDir {
  readonly runDir: string
  readonly resumed: boolean
}

/**
 * Confine an explicit run directory to the plugin's run root.
 *
 * `/ta-analyze --run-dir` may only reach a run this plugin created. Without this,
 * a prompt could point the worker at any directory on disk and have it write
 * reports, a memory entry, and a state log there.
 */
export function confineRunDir(base: string, candidate: string): string {
  const root = resolve(base)
  const target = resolve(candidate)
  const rel = relative(root, target)
  if (rel === "" || (!rel.startsWith("..") && !isAbsolute(rel))) return target
  throw new PipelineError(`run directory must be inside ${root}, got ${target}`)
}

async function resolveRunDir(context: {
  config: TradingConfig
  input: AnalyzeInput
  ticker: string
  tradeDate: string
}): Promise<ResolvedRunDir> {
  const explicit = context.input.run_dir?.trim() || context.input.resume_run?.trim()
  if (!explicit && context.input.resume_run !== undefined) {
    // `--resume` with no value: the newest run for this ticker and date.
    const base = runRootOf(context.config, context.input)
    const runDir = await latestRunFor(base, `${context.ticker}_${context.tradeDate}_`)
    return { runDir, resumed: true }
  }
  if (explicit) {
    const base = runRootOf(context.config, context.input)
    const runDir = explicit.includes("/") || explicit.includes("\\")
      ? confineRunDir(base, expandHome(explicit))
      : await resolveRunId(base, explicit)
    const probe = await readTextOrNull(join(runDir, "request.json"))
    if (!probe) throw new PipelineError(`no run directory with a request.json at ${runDir}`)
    return { runDir, resumed: true }
  }
  const base = runRootOf(context.config, context.input)
  const runDir = uniqueRunDir(base, newRunId(context.ticker, context.tradeDate))
  await mkdir(runDir, { recursive: true })
  return { runDir, resumed: false }
}

/** Exact run directory name, or a unique prefix of one. */
export async function resolveRunId(base: string, query: string): Promise<string> {
  const trimmed = query.trim().replace(/\/+$/, "")
  if (!trimmed) throw new PipelineError("run id is empty")
  let names: string[] = []
  try {
    names = (await readdir(base, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
  } catch {
    throw new PipelineError(`no runs directory at ${base}`)
  }
  if (names.includes(trimmed)) return join(base, trimmed)
  const matches = names.filter((name) => name.startsWith(trimmed))
  if (matches.length === 1) return join(base, matches[0])
  if (matches.length === 0) throw new PipelineError(`no run matches '${trimmed}' in ${base}`)
  throw new PipelineError(
    `'${trimmed}' matches ${matches.length} runs; be more specific: ${matches.slice(0, 8).join(", ")}`,
  )
}

/**
 * Merge every validated stage into `final_state.json`, in one write.
 *
 * The analyst reports, the three decisions, both debate transcripts, and the
 * final rating land under the same keys `reporting.py` reads, so the report and
 * memory phases can consume this file without translating it.
 */
async function writeFinalState(
  context: RunContext,
  specs: readonly StageSpec[],
): Promise<Record<string, string>> {
  const statePath = join(context.runDir, "final_state.json")
  const existing = await readTextOrNull(statePath)
  let state: Record<string, unknown> = {}
  if (existing) {
    try {
      state = JSON.parse(existing) as Record<string, unknown>
    } catch {
      state = {}
    }
  }
  state.ticker = context.payload.ticker
  state.trade_date = context.payload.trade_date

  const reports: Record<string, string> = {}
  for (const spec of specs) {
    if (!spec.reportKey) continue
    const report = await readTextOrNull(join(context.runDir, "stages", `${spec.stage}.md`))
    if (!report?.trim()) continue
    state[spec.reportKey] = report.trim()
    reports[spec.reportKey] = report.trim()
  }

  const debates = await runWorker({ action: "debates", runDir: context.runDir }, context.worker)
  if (debates.ok) {
    for (const key of ["investment_debate_state", "risk_debate_state"] as const) {
      const value = debates[key]
      if (value && typeof value === "object") state[key] = value
    }
  }
  if (context.payload.portfolio) state.portfolio_context = context.payload.portfolio
  const pastContext = await readTextOrNull(join(context.runDir, "memory", "past_context.md"))
  if (pastContext?.trim()) state.past_context = pastContext
  const memoryNote = await readTextOrNull(join(context.runDir, "memory", "note.txt"))
  if (memoryNote?.trim()) state.memory_note = memoryNote.trim()

  // The rating is typed by the Portfolio Manager; read the validated JSON so a
  // resumed run reports the same rating without a model call.
  const decision = await readTextOrNull(join(context.runDir, "stages", "portfolio_manager.json"))
  if (decision) {
    try {
      const parsed = JSON.parse(decision) as { rating?: unknown }
      if (typeof parsed.rating === "string") state.final_rating = parsed.rating
    } catch {
      // A malformed decision JSON means the stage was accepted leniently.
    }
  }

  await writeFile(statePath, `${JSON.stringify(state, null, 2)}\n`, "utf8")
  return reports
}

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
    await progress(options.hooks, `settling ${entries.length} past decision(s)`)
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

function savedText(saved: Record<string, unknown> | null, key: string): string | undefined {
  const value = saved?.[key]
  return typeof value === "string" && value ? value : undefined
}

/**
 * The request to run, with a resumed run's own settings as the baseline.
 *
 * An explicitly supplied option wins, so `--depth 2` can deliberately invalidate
 * the tail; everything else comes from `request.json`. Without this, a resume
 * that left out a sweep's `results_dir` or memory path would rewrite the request,
 * invalidate every stage hash, and move the run's outputs.
 */
function buildPayload(
  config: TradingConfig,
  input: AnalyzeInput,
  ticker: string,
  tradeDate: string,
  depth: number,
  analysts: readonly string[],
  saved: Record<string, unknown> | null,
): RequestPayload {
  const savedModels =
    saved && typeof saved.models === "object" && saved.models
      ? (saved.models as Record<string, unknown>)
      : {}
  const models: Record<string, string> = {}
  if (input.model?.trim()) {
    const ref = formatModelRef(parseModelRef(input.model))
    models.quick = ref
    models.deep = ref
  } else {
    models.quick = input.quick_model?.trim()
      ? formatModelRef(parseModelRef(input.quick_model))
      : typeof savedModels.quick === "string"
        ? savedModels.quick
        : formatModelRef(config.quickModel)
    models.deep = input.deep_model?.trim()
      ? formatModelRef(parseModelRef(input.deep_model))
      : typeof savedModels.deep === "string"
        ? savedModels.deep
        : formatModelRef(config.deepModel)
  }

  const savedDepth = typeof saved?.depth === "number" ? saved.depth : undefined
  const savedAnalysts = Array.isArray(saved?.analysts) ? saved.analysts.map(String) : undefined
  const inputResults = input.results_dir?.trim()
  const inputMemory = input.memory_log_path?.trim()

  return {
    ticker,
    trade_date: tradeDate,
    asset_type: input.asset_type ?? savedText(saved, "asset_type") ?? "stock",
    depth: input.depth !== undefined ? depth : (savedDepth ?? depth),
    analysts: input.analysts?.length ? analysts : (savedAnalysts ?? analysts),
    output_language: input.output_language?.trim() || savedText(saved, "output_language") || "English",
    portfolio: input.portfolio !== undefined ? portfolioOf(input) : (savedText(saved, "portfolio") ?? ""),
    models,
    // A backtest sweep overrides both; empty means the repo defaults.
    results_dir: inputResults
      ? expandHome(inputResults)
      : (savedText(saved, "results_dir") ?? config.resultsDir),
    memory_log_path: inputMemory
      ? expandHome(inputMemory)
      : (savedText(saved, "memory_log_path") ?? config.memoryLogPath),
  }
}

export async function runAnalysis(
  ctx: Plugin.Context,
  input: AnalyzeInput,
  hooks: PipelineHooks = {},
): Promise<RunSummary> {
  const config = readConfig(ctx.options)
  const started = Date.now()
  const ticker = tickerOf(input)
  const tradeDate = dateOf(input)
  const depth = depthOf(input)
  const analysts = analystsOf(input)

  const { runDir, resumed } = await resolveRunDir({ config, input, ticker, tradeDate })

  // A resumed run keeps the request it started with: options the caller did not
  // repeat (a sweep's isolated paths, for instance) come from the saved request,
  // so resuming cannot silently change the run into a different one.
  const saved = resumed ? await readJson(join(runDir, "request.json")) : null
  if (saved) {
    const savedTicker = typeof saved.ticker === "string" ? saved.ticker : ""
    const savedDate = typeof saved.trade_date === "string" ? saved.trade_date : ""
    if (savedTicker && savedTicker.toUpperCase() !== ticker) {
      throw new PipelineError(
        `run ${runDir} is for ${savedTicker}, not ${ticker}; resume it with its own ticker`,
      )
    }
    if (savedDate && savedDate !== tradeDate) {
      throw new PipelineError(
        `run ${runDir} is for ${savedDate}, not ${tradeDate}; resume it with its own date`,
      )
    }
  }
  const payload = buildPayload(config, input, ticker, tradeDate, depth, analysts, saved)

  const worker = {
    directory: ctx.location.directory,
    python: config.python,
    signal: hooks.signal,
  }
  const placeholder = createCheckpoint(runDir.split(/[\\/]/).pop() ?? runDir, "")
  const context: RunContext = {
    ctx,
    config,
    input,
    hooks,
    runDir,
    payload,
    worker,
    store: new CheckpointStore(runDir, placeholder),
    order: [],
    fallbacks: [],
    calls: 0,
  }

  await writeFile(join(runDir, "request.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8")
  const init = await initRun(context)

  const requestHash = hashJson(init.request ?? payload)
  const previous = await readCheckpoint(runDir)
  const warnings: string[] = []
  if (previous && previous.request_hash === requestHash) {
    context.store.value = previous
  } else {
    if (previous) warnings.push("request changed; earlier stage records were invalidated")
    await context.store.replace(
      createCheckpoint(String(init.run_id ?? runDir.split(/[\\/]/).pop()), requestHash),
    )
  }

  const { groups, order } = await readPlan(context)
  context.order = order
  const specs = groups.flatMap((group) => group.stages.map(toSpec))

  // Fail on an unavailable model before any data or generation work. Fallbacks
  // are optional, so unknown ones are dropped rather than failing the run.
  for (const spec of specs) {
    await resolveModel(ctx, modelForRun(config, input, spec.stage, spec.tier))
  }
  context.fallbacks = await resolveCandidates(ctx, config.fallbackModels)

  // Settle past decisions while the analysts work, as the graph does. The
  // reflections are model calls, so the pass starts here and the Portfolio
  // Manager waits for it — the only prompt that reads `past_context`.
  const memoryEnabled = input.memory ?? config.memory
  let memoryTask: Promise<MemoryOutcome> | null = null
  if (memoryEnabled) {
    await resolveModel(ctx, modelForRun(config, input, "memory", "quick"))
    memoryTask = settleMemory({
      ctx,
      config,
      input,
      hooks,
      runDir,
      worker,
      fallbacks: context.fallbacks,
    })
  }

  await progress(
    hooks,
    resumed
      ? `resuming ${String(init.run_id ?? "")}`
      : `starting ${String(init.run_id ?? "")} in ${runDir}`,
  )

  if (input.rerun_from) {
    if (!order.includes(input.rerun_from)) {
      throw new PipelineError(
        `--rerun-from '${input.rerun_from}' is not a stage of this run (${order.join(", ")})`,
      )
    }
    await context.store.replace(invalidateFrom(context.store.value, order, input.rerun_from))
  }
  if (input.rerun && !order.includes(input.rerun)) {
    throw new PipelineError(
      `--rerun '${input.rerun}' is not a stage of this run (${order.join(", ")})`,
    )
  }

  const failures: string[] = []
  let rating: string | undefined
  let aborted = false

  for (const group of groups) {
    if (aborted) break
    throwIfAborted(hooks.signal)

    if (group.parallel) {
      const plans: (StagePlan | "skipped")[] = []
      for (const raw of group.stages) {
        const spec = toSpec(raw)
        const model = modelForRun(config, input, spec.stage, spec.tier)
        plans.push(await prepareStage(context, spec, model))
      }
      const pending = plans.filter((plan): plan is StagePlan => plan !== "skipped")
      await runWithConcurrency(pending, config.concurrency, async (plan) => {
        try {
          const outcome = await executeStage(context, plan)
          if (outcome.rating) rating = outcome.rating
        } catch (error) {
          // The failed record is already checkpointed by executeStage; collect
          // the message here so the run reports every failure at the end.
          failures.push(error instanceof Error ? error.message : String(error))
        }
      })
      // Drain in-flight checkpoint writes before the next group reads files.
      await context.store.replace(context.store.value)
      continue
    }

    for (const raw of group.stages) {
      const spec = toSpec(raw)
      if (spec.stage === "portfolio_manager" && memoryTask) {
        const outcome = await memoryTask
        memoryTask = null
        warnings.push(...outcome.warnings)
        context.calls += outcome.calls
      }
      const model = modelForRun(config, input, spec.stage, spec.tier)
      const plan = await prepareStage(context, spec, model)
      if (plan === "skipped") continue
      try {
        const outcome = await executeStage(context, plan)
        if (outcome.rating) rating = outcome.rating
      } catch (error) {
        // A debate or decision stage reads the previous stage's output, so a
        // failure here makes every later prompt in the chain guess; stop and
        // let a resume retry from the failed stage.
        failures.push(error instanceof Error ? error.message : String(error))
        aborted = true
        break
      }
    }
  }

  if (memoryTask) {
    const outcome = await memoryTask
    memoryTask = null
    warnings.push(...outcome.warnings)
    context.calls += outcome.calls
  }
  await context.store.replace(context.store.value)

  const summaryStages = []
  for (const stage of order) {
    const record = context.store.value.stages[stage]
    if (!record) continue
    const chars = record.out
      ? ((await readTextOrNull(join(runDir, record.out))) ?? "").trim().length
      : 0
    summaryStages.push({
      stage,
      status: record.status,
      model: record.model,
      ms: record.ms,
      chars,
    })
  }

  const reports = await writeFinalState(context, specs)

  let settled: SettleReply | null = null
  if (!failures.length) {
    try {
      const reply = await runWorker(
        {
          action: "settle",
          runDir,
          jsonArgs: { html: input.html ?? config.html, warnings },
        },
        context.worker,
      )
      if (reply.ok) settled = reply as SettleReply
      else warnings.push(`reports were not written: ${formatReply(reply.error)}`)
    } catch (error) {
      warnings.push(
        `reports were not written: ${error instanceof Error ? error.message : String(error)}`,
      )
    }
  }

  const finalCheckpoint = await readCheckpoint(runDir)
  for (const warning of finalCheckpoint?.warnings ?? []) {
    if (!warnings.includes(warning)) warnings.push(warning)
  }

  if (failures.length) {
    const unique = [...new Set(failures)]
    throw new PipelineError(
      `${unique.length} stage(s) failed: ${unique.join("; ")}. Completed stages are checkpointed; resume to retry.`,
    )
  }

  return {
    runId: String(init.run_id ?? context.store.value.run_id),
    runDir,
    ticker,
    tradeDate,
    depth,
    stages: summaryStages,
    warnings,
    calls: context.calls,
    elapsedMs: Date.now() - started,
    rating: settled?.rating ?? rating,
    reports,
    reportMd: settled?.report_md,
    reportHtml: settled?.report_html,
    stateLog: settled?.state_log,
    memoryLog: settled?.memory_log,
    memoryNote: (await readTextOrNull(join(runDir, "memory", "note.txt")))?.trim() || undefined,
  }
}

const MAX_PREVIEW = 1_500

export function renderSummary(summary: RunSummary): string {
  const lines = [
    "### TradingAgents run",
    "",
    `- Ticker: **${summary.ticker}**`,
    `- Analysis date: ${summary.tradeDate}`,
    `- Depth: ${summary.depth}`,
    `- Run: \`${summary.runId}\``,
    `- Directory: \`${summary.runDir}\``,
    `- Model calls: ${summary.calls} · ${(summary.elapsedMs / 1000).toFixed(1)}s`,
  ]
  if (summary.rating) lines.push(`- Rating: **${summary.rating}**`)
  if (summary.reportMd) lines.push(`- Report: \`${summary.reportMd}\``)
  if (summary.reportHtml) lines.push(`- HTML: \`${summary.reportHtml}\``)
  if (summary.stateLog) lines.push(`- State log: \`${summary.stateLog}\``)
  if (summary.memoryLog) lines.push(`- Memory log: \`${summary.memoryLog}\``)
  lines.push("", "| Stage | Status | Model | Time | Chars |", "|---|---|---|---|---|")
  for (const stage of summary.stages) {
    lines.push(
      `| ${stage.stage} | ${stage.status} | \`${stage.model}\` | ${stage.ms}ms | ${stage.chars} |`,
    )
  }
  if (summary.warnings.length) {
    lines.push("", "**Warnings**", "")
    for (const warning of summary.warnings) lines.push(`- ${warning}`)
  }
  for (const [key, report] of Object.entries(summary.reports)) {
    lines.push("", `#### ${key}`, "")
    lines.push(report.length > MAX_PREVIEW ? `${report.slice(0, MAX_PREVIEW)}…` : report)
    if (report.length > MAX_PREVIEW) {
      lines.push("", `_(truncated — full report in \`${summary.runDir}\`)_`)
    }
  }
  return lines.join("\n")
}

/** A written report, read back from a run directory. */
export interface ReportView {
  readonly runDir: string
  readonly path: string
  readonly format: "md" | "html"
  readonly content: string
}

/**
 * `trading_report`: hand back `complete_report.md` (or the HTML page).
 *
 * Reports are written by the worker's `settle` step, so this only reads; a run
 * that stopped before settle gets a message naming the missing file instead of
 * a half-rendered report stitched together from stages.
 */
export async function readReport(
  ctx: Plugin.Context,
  query: string | undefined,
  format: "md" | "html" = "md",
): Promise<ReportView> {
  const config = readConfig(ctx.options)
  const base = expandHome(config.runRoot)
  const runDir = query?.trim() ? await resolveRunId(base, query) : await latestRun(base)
  const path = join(runDir, "reports", `complete_report.${format}`)
  const content = await readTextOrNull(path)
  if (!content) {
    throw new PipelineError(
      `no reports/complete_report.${format} in ${runDir}. The run has not settled yet; ` +
        "finish it (or resume it) first.",
    )
  }
  return { runDir, path, format, content }
}

/** `trading_status`: what a run has produced so far, without calling a model. */
export async function describeRun(
  ctx: Plugin.Context,
  query: string | undefined,
): Promise<string> {
  const config = readConfig(ctx.options)
  const base = expandHome(config.runRoot)
  const runDir = query?.trim() ? await resolveRunId(base, query) : await latestRun(base)
  const request = await readJson(join(runDir, "request.json"))
  const checkpoint = await readCheckpoint(runDir)
  const stagesDir = join(runDir, "stages")
  const files = (await readdir(stagesDir).catch(() => [] as string[]))
    .filter((name: string) => name.endsWith(".md") && !name.endsWith(".prompt.md"))
    .sort()

  const lines = [
    `### Run \`${runDir.split("/").pop()}\``,
    "",
    `- Directory: \`${runDir}\``,
    request
      ? `- ${String(request.ticker ?? "?")} on ${String(request.trade_date ?? "?")} · depth ${String(request.depth ?? "?")}`
      : "- request.json is missing",
    `- Model calls made: ${checkpoint ? "recorded below" : "none recorded"}`,
    "",
    "| Stage | Status | Model | Attempts | Time |",
    "|---|---|---|---|---|",
  ]

  if (checkpoint) {
    for (const [stage, record] of Object.entries(checkpoint.stages).sort()) {
      lines.push(
        `| ${stage} | ${record.status} | \`${record.model}\` | ${record.attempts} | ${record.ms}ms |`,
      )
    }
    if (!Object.keys(checkpoint.stages).length) lines.push("| — | no stages recorded | — | — | — |")
  } else {
    lines.push("| — | no checkpoint | — | — | — |")
  }

  if (files.length) {
    lines.push("", "**Reports**", "")
    for (const file of files) lines.push(`- \`stages/${file}\``)
  }

  const warnings = checkpoint?.warnings ?? []
  if (warnings.length) {
    lines.push("", "**Warnings**", "")
    for (const warning of warnings) lines.push(`- ${warning}`)
  }

  return lines.join("\n")
}

async function readJson(path: string): Promise<Record<string, unknown> | null> {
  const text = await readTextOrNull(path)
  if (!text) return null
  try {
    const parsed = JSON.parse(text) as unknown
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/**
 * Run directories directly under `base`, oldest first.
 *
 * Only directories with a `request.json` count: `base` also holds `backtest/`
 * (the sweeps) and anything else a user dropped there, and "latest run" must
 * never resolve to one of those.
 */
async function runDirs(base: string): Promise<string[]> {
  const entries = await readdir(base, { withFileTypes: true }).catch(() => [])
  const names = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort()
  const runs: string[] = []
  for (const name of names) {
    if (await readTextOrNull(join(base, name, "request.json"))) runs.push(name)
  }
  return runs
}

async function latestRunFor(base: string, prefix: string): Promise<string> {
  const names = (await runDirs(base)).filter((name) => name.startsWith(prefix))
  if (!names.length) throw new PipelineError(`no runs for ${prefix.replace(/_$/, "")} in ${base}`)
  return join(base, names[names.length - 1])
}

async function latestRun(base: string): Promise<string> {
  const names = await runDirs(base)
  if (!names.length) throw new PipelineError(`no runs yet in ${base}`)
  return join(base, names[names.length - 1])
}
