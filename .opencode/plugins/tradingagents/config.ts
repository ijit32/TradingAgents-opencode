/**
 * Plugin configuration: defaults, option parsing, and model reference handling.
 *
 * Options come from the object form of the `plugins` entry in `opencode.json(c)`.
 * Plugins under `.opencode/plugins/` load automatically but get no options, so
 * every value here has a usable default.
 */

/** A model reference as OpenCode expects it. */
export interface ModelRef {
  readonly providerID: string
  readonly id: string
  readonly variant?: string
}

export interface TradingConfig {
  /** Model for analysts, debaters, and the trader. */
  readonly quickModel: ModelRef
  /** Model for the research manager and portfolio manager. */
  readonly deepModel: ModelRef
  /** Per-stage overrides, keyed by stage name. */
  readonly stageModels: Readonly<Record<string, ModelRef>>
  /** Tried in order when the primary model is unavailable or rate limited. */
  readonly fallbackModels: readonly ModelRef[]
  /** Maximum concurrent generation calls during the parallel analyst stage. */
  readonly concurrency: number
  /** Per-call timeout. */
  readonly callTimeoutMs: number
  /** Retries per call for retryable failures. */
  readonly maxRetries: number
  /** Python runner used by the worker. */
  readonly python: string
  /** Where run directories live. `~` is expanded. */
  readonly runRoot: string
  /** Settle past decisions and record this run's decision in the memory log. */
  readonly memory: boolean
  /** Write `complete_report.html` beside the markdown report. */
  readonly html: boolean
  /** Memory log path; empty uses the repo default. `~` is expanded. */
  readonly memoryLogPath: string
  /** Results directory for the state log; empty uses the repo default. */
  readonly resultsDir: string
}

export const DEFAULT_QUICK_MODEL = "opencode/deepseek-v4.1-flash-free"
export const DEFAULT_DEEP_MODEL = "opencode/deepseek-v4.1-flash-free"

/**
 * Parse `provider/model` or `provider/model#variant`.
 *
 * Model IDs contain slashes and dots (`opencode/qwen3.8-flash`), so the split is
 * on the first slash only.
 */
export function parseModelRef(spec: string): ModelRef {
  const trimmed = spec.trim()
  if (!trimmed) throw new Error("model reference is empty; expected provider/model")
  const hash = trimmed.indexOf("#")
  const variant = hash >= 0 ? trimmed.slice(hash + 1) : undefined
  const base = hash >= 0 ? trimmed.slice(0, hash) : trimmed
  const slash = base.indexOf("/")
  if (slash <= 0 || slash === base.length - 1) {
    throw new Error(`invalid model reference '${spec}'; expected provider/model`)
  }
  const providerID = base.slice(0, slash)
  const id = base.slice(slash + 1)
  return variant ? { providerID, id, variant } : { providerID, id }
}

export function formatModelRef(ref: ModelRef): string {
  return `${ref.providerID}/${ref.id}${ref.variant ? `#${ref.variant}` : ""}`
}

function stringOption(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback
}

function booleanOption(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value
  if (value === "true") return true
  if (value === "false") return false
  return fallback
}

function numberOption(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(parsed)))
}

/** Read `ctx.options` into a typed config. Unknown or malformed values fall back. */
export function readConfig(options: Readonly<Record<string, unknown>>): TradingConfig {
  const stageModels: Record<string, ModelRef> = {}
  const rawStages = options.stageModels
  if (rawStages && typeof rawStages === "object" && !Array.isArray(rawStages)) {
    for (const [stage, spec] of Object.entries(rawStages as Record<string, unknown>)) {
      if (typeof spec === "string") stageModels[stage] = parseModelRef(spec)
    }
  }

  const rawFallbacks = options.fallbackModels
  const fallbackModels = Array.isArray(rawFallbacks)
    ? rawFallbacks.filter((item): item is string => typeof item === "string").map(parseModelRef)
    : []

  return {
    quickModel: parseModelRef(stringOption(options.quickModel, DEFAULT_QUICK_MODEL)),
    deepModel: parseModelRef(stringOption(options.deepModel, DEFAULT_DEEP_MODEL)),
    stageModels,
    fallbackModels,
    // Four analysts ship in the parallel group; four lanes keeps one wave.
    concurrency: numberOption(options.concurrency, 4, 1, 8),
    callTimeoutMs: numberOption(options.callTimeoutMs, 180_000, 5_000, 900_000),
    maxRetries: numberOption(options.maxRetries, 2, 0, 5),
    python: stringOption(options.python, "uv"),
    runRoot: stringOption(options.runRoot, "~/.tradingagents/opencode"),
    memory: booleanOption(options.memory, true),
    html: booleanOption(options.html, true),
    memoryLogPath: stringOption(options.memoryLogPath, ""),
    resultsDir: stringOption(options.resultsDir, ""),
  }
}

/** Expand a leading `~` (the plugin may run outside a shell that would). */
export function expandHome(path: string): string {
  if (path === "~") return process.env.HOME ?? path
  if (path.startsWith("~/")) return `${process.env.HOME ?? "~"}${path.slice(1)}`
  return path
}

/** The model a stage should use: explicit override, then stage map, then tier default. */
export function modelForStage(
  config: TradingConfig,
  stage: string,
  tier: "quick" | "deep" = "quick",
): ModelRef {
  return config.stageModels[stage] ?? (tier === "deep" ? config.deepModel : config.quickModel)
}
