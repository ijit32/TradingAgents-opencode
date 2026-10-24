/**
 * Text generation through OpenCode.
 *
 * The Console free tier is gated on request provenance: `ctx.generate.text`
 * sends a transient request that carries only its own session id, and the
 * gateway answers `403 FreeTierError`. `ctx.session.generate` runs through the
 * normal session request builder, which stamps OpenCode's client identity
 * (`x-opencode-client`, `x-opencode-project`, `User-Agent`, session affinity)
 * onto the request, so the same model works there.
 *
 * Every call therefore runs on a short-lived session: create with the target
 * model, generate, delete. The session carries no history and no tools, so this
 * is still a single-shot prompt — only the transport differs.
 */

import type { Plugin } from "@opencode/plugin"
import type { ModelRef } from "./config"
import { formatModelRef } from "./config"

export type GenerateContext = Pick<Plugin.Context, "session">

export type FailureKind =
  | "freetier"
  | "quota"
  | "auth"
  | "ratelimit"
  | "timeout"
  | "server"
  | "other"

const RETRYABLE: ReadonlySet<FailureKind> = new Set<FailureKind>([
  "ratelimit",
  "timeout",
  "server",
])

export class GenerateError extends Error {
  readonly kind: FailureKind
  readonly model: string
  readonly attempts: number

  constructor(message: string, kind: FailureKind, model: string, attempts: number) {
    super(message)
    this.name = "GenerateError"
    this.kind = kind
    this.model = model
    this.attempts = attempts
  }
}

export interface GenerateResult {
  readonly text: string
  readonly model: string
  readonly attempts: number
  readonly ms: number
}

export interface GenerateOptions {
  readonly timeoutMs?: number
  readonly maxRetries?: number
  /** Reuse an existing session instead of creating one per call. Probes do not. */
  readonly sessionID?: string
}

/** Everything an error object might carry, flattened into one searchable string. */
function describeError(error: unknown): { message: string; haystack: string; status?: number } {
  if (error instanceof Error) {
    const extra = error as Error & { status?: number; data?: unknown; body?: unknown }
    return {
      message: error.message,
      haystack: [error.name, error.message, safeJson(extra.data), safeJson(extra.body)]
        .filter(Boolean)
        .join(" "),
      status: typeof extra.status === "number" ? extra.status : undefined,
    }
  }
  if (typeof error === "string") return { message: error, haystack: error }
  return { message: "unknown generation failure", haystack: safeJson(error) }
}

function safeJson(value: unknown): string {
  if (value === undefined || value === null) return ""
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/**
 * Map a provider/client error onto the retry policy.
 *
 * `FreeTierError` is not retried: it means the request used a transport the
 * gateway refuses, which a second attempt will not change.
 */
export function classifyFailure(error: unknown): FailureKind {
  if (error instanceof GenerateTimeout) return "timeout"
  const { haystack, message, status } = describeError(error)
  const text = `${haystack} ${message}`.toLowerCase()

  // A spent free allowance is not a transient rate limit: waiting out a retry
  // window will not help, and the user needs the upgrade message, not `other`.
  if (
    text.includes("free usage limit") ||
    text.includes("usage limit reached") ||
    text.includes("quota exceeded") ||
    text.includes("insufficient quota")
  ) {
    return "quota"
  }
  if (text.includes("freetier") || text.includes("free tier") || status === 403) return "freetier"
  if (
    status === 401 ||
    text.includes("unauthorized") ||
    text.includes("authentication") ||
    text.includes("invalid api key") ||
    text.includes("missing api key")
  ) {
    return "auth"
  }
  if (status === 429 || text.includes("rate limit") || text.includes("too many requests")) {
    return "ratelimit"
  }
  if (status !== undefined && status >= 500) return "server"
  if (text.includes("timeout") || text.includes("timed out") || text.includes("etimedout")) {
    return "timeout"
  }
  if (text.includes("fetch failed") || text.includes("econnreset") || text.includes("socket hang up")) {
    return "server"
  }
  return "other"
}

class GenerateTimeout extends Error {
  constructor(readonly ms: number) {
    super(`generation did not finish within ${ms}ms`)
    this.name = "GenerateTimeout"
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function backoffMs(attempt: number): number {
  const base = Math.min(15_000, 2_000 * 2 ** attempt)
  return base + Math.floor(Math.random() * 500)
}

async function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new GenerateTimeout(ms)), ms)
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

/** Create a throwaway session pinned to one model, run one prompt, clean up. */
async function generateOnFreshSession(
  ctx: GenerateContext,
  model: ModelRef,
  prompt: string,
  timeoutMs: number,
): Promise<string> {
  const session = await ctx.session.create({
    title: `tradingagents ${formatModelRef(model)}`,
    model,
    metadata: { tradingagents: { kind: "generation" } },
  })
  const sessionID = session.id
  try {
    const response = await withTimeout(ctx.session.generate({ sessionID, prompt }), timeoutMs)
    const text = typeof response === "string" ? response : response?.text
    if (typeof text !== "string") throw new Error("session generation returned no text")
    return text
  } finally {
    // `remove` is part of the runtime session API but not of the typed pick;
    // sessions are cheap, but leaving one behind per call is not.
    const removable = ctx.session as unknown as {
      remove?: (input: { sessionID: string }) => Promise<void>
    }
    await removable.remove?.({ sessionID }).catch(() => undefined)
  }
}

async function generateOnSession(
  ctx: GenerateContext,
  sessionID: string,
  prompt: string,
  timeoutMs: number,
): Promise<string> {
  const response = await withTimeout(ctx.session.generate({ sessionID, prompt }), timeoutMs)
  const text = typeof response === "string" ? response : response?.text
  if (typeof text !== "string") throw new Error("session generation returned no text")
  return text
}

/** One call, with retries for retryable failures. Throws `GenerateError`. */
export async function generate(
  ctx: GenerateContext,
  model: ModelRef,
  prompt: string,
  options: GenerateOptions = {},
): Promise<GenerateResult> {
  if (!prompt.trim()) throw new Error("generate() needs a non-empty prompt")
  const timeoutMs = options.timeoutMs ?? 180_000
  const maxRetries = options.maxRetries ?? 2
  const started = Date.now()

  let last: GenerateError | undefined
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const text = options.sessionID
        ? await generateOnSession(ctx, options.sessionID, prompt, timeoutMs)
        : await generateOnFreshSession(ctx, model, prompt, timeoutMs)
      return {
        text,
        model: formatModelRef(model),
        attempts: attempt + 1,
        ms: Date.now() - started,
      }
    } catch (error) {
      const kind = classifyFailure(error)
      const { message } = describeError(error)
      last = new GenerateError(message, kind, formatModelRef(model), attempt + 1)
      if (!RETRYABLE.has(kind) || attempt === maxRetries) throw last
      await sleep(backoffMs(attempt))
    }
  }
  throw last ?? new GenerateError("generation failed", "other", formatModelRef(model), 0)
}

export interface FallbackResult extends GenerateResult {
  /** Models that failed before the winning one, in order. */
  readonly skipped: readonly { model: string; kind: FailureKind; message: string }[]
}

/**
 * Try the primary model, then each fallback.
 *
 * Auth failures stop immediately: a second model will not fix a missing key.
 */
export async function generateWithFallback(
  ctx: GenerateContext,
  models: readonly ModelRef[],
  prompt: string,
  options: GenerateOptions = {},
): Promise<FallbackResult> {
  if (models.length === 0) throw new Error("generateWithFallback() needs at least one model")
  const skipped: { model: string; kind: FailureKind; message: string }[] = []

  for (const model of models) {
    try {
      const result = await generate(ctx, model, prompt, options)
      return { ...result, skipped }
    } catch (error) {
      if (!(error instanceof GenerateError)) throw error
      skipped.push({ model: error.model, kind: error.kind, message: error.message })
      if (error.kind === "auth") throw error
    }
  }
  const summary = skipped
    .map((item) => `${item.model}: ${item.kind}${item.message ? ` — ${item.message.slice(0, 180)}` : ""}`)
    .join("; ")
  throw new GenerateError(
    `every model failed (${summary})`,
    skipped[skipped.length - 1]?.kind ?? "other",
    models.map(formatModelRef).join(", "),
    skipped.length,
  )
}
