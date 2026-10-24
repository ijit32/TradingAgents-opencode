/**
 * Phase 0 probe: can this plugin reach an OpenCode model, including a Console
 * free-tier model, without the gateway refusing the call?
 *
 * Two paths are compared because they run different code in the server:
 *   session — `ctx.session.generate`, the normal request builder. Carries the
 *             OpenCode client identity headers. This is the supported route.
 *   text    — `ctx.generate.text`, a bare transient call. The gateway answers
 *             `403 FreeTierError` for free-tier models, so it stays here only
 *             as a diagnostic and is never used by the pipeline.
 */

import type { Plugin } from "@opencode/plugin"
import type { ModelRef } from "./config"
import { formatModelRef, modelForStage, parseModelRef, readConfig } from "./config"
import { GenerateError, classifyFailure, generate } from "./generate"
import { resolveModel } from "./models"

export const DEFAULT_PROBE_PROMPT =
  "Reply with exactly one line: OpenCode free model reachable."
const MAX_PREVIEW = 600

export type ProbeMode = "text" | "session"

export interface ProbeInput {
  readonly prompt?: string
  readonly model?: string
  readonly parallel?: number
  readonly timeoutMs?: number
  readonly mode?: ProbeMode
}

export interface ProbeRun {
  readonly ok: boolean
  readonly mode: ProbeMode
  readonly model: string
  readonly attempts: number
  readonly ms: number
  readonly chars: number
  readonly preview: string
  readonly error?: { kind: string; message: string }
}

function preview(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim()
  return flat.length > MAX_PREVIEW ? `${flat.slice(0, MAX_PREVIEW)}…` : flat
}

function failure(error: unknown, mode: ProbeMode, model: string, attempts = 0): ProbeRun {
  return {
    ok: false,
    mode,
    model,
    attempts,
    ms: 0,
    chars: 0,
    preview: "",
    error: {
      kind: error instanceof GenerateError ? error.kind : classifyFailure(error),
      message: error instanceof Error ? error.message : String(error),
    },
  }
}

/** The bare transient call. Expected to be refused for free-tier models. */
async function transientGenerate(
  ctx: Plugin.Context,
  ref: ModelRef,
  prompt: string,
  timeoutMs: number,
): Promise<{ text: string; attempts: number; ms: number }> {
  const started = Date.now()
  const response = await Promise.race([
    ctx.generate.text({ prompt, model: ref }),
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error(`generation did not finish within ${timeoutMs}ms`)), timeoutMs),
    ),
  ])
  const text = typeof response === "string" ? response : response?.text
  if (typeof text !== "string") throw new Error("generation returned no text")
  return { text, attempts: 1, ms: Date.now() - started }
}

export async function runProbe(
  ctx: Plugin.Context,
  input: ProbeInput,
): Promise<{ runs: ProbeRun[]; model: string; mode: ProbeMode; report: string }> {
  const config = readConfig(ctx.options)
  const spec = input.model ?? formatModelRef(modelForStage(config, "probe", "quick"))
  const ref = await resolveModel(ctx, parseModelRef(spec))
  const prompt = input.prompt?.trim() ? input.prompt : DEFAULT_PROBE_PROMPT
  const parallel = Math.min(8, Math.max(1, Math.trunc(input.parallel ?? 1)))
  const timeoutMs = Math.min(
    900_000,
    Math.max(5_000, Math.trunc(input.timeoutMs ?? config.callTimeoutMs)),
  )
  const mode: ProbeMode = input.mode === "text" ? "text" : "session"
  const model = formatModelRef(ref)

  const call = async (): Promise<ProbeRun> => {
    try {
      if (mode === "text") {
        const result = await transientGenerate(ctx, ref, prompt, timeoutMs)
        return {
          ok: true,
          mode,
          model,
          attempts: result.attempts,
          ms: result.ms,
          chars: result.text.length,
          preview: preview(result.text),
        }
      }
      const result = await generate(ctx, ref, prompt, {
        timeoutMs,
        maxRetries: config.maxRetries,
      })
      return {
        ok: true,
        mode,
        model: result.model,
        attempts: result.attempts,
        ms: result.ms,
        chars: result.text.length,
        preview: preview(result.text),
      }
    } catch (error) {
      return failure(error, mode, model, error instanceof GenerateError ? error.attempts : 0)
    }
  }

  const runs = await Promise.all(Array.from({ length: parallel }, call))
  return { runs, model, mode, report: renderReport(model, mode, runs) }
}

export function renderReport(
  model: string,
  mode: ProbeMode,
  runs: readonly ProbeRun[],
): string {
  const lines = [
    "### OpenCode generation probe",
    "",
    `Model: \`${model}\`  ·  Path: \`${mode === "session" ? "ctx.session.generate" : "ctx.generate.text"}\``,
    "",
    "| # | ok | ms | chars | attempts | note |",
    "|---|---|---|---|---|---|",
  ]
  runs.forEach((run, index) => {
    const note = run.ok ? "—" : `${run.error?.kind}: ${run.error?.message ?? "failed"}`
    lines.push(
      `| ${index + 1} | ${run.ok ? "yes" : "no"} | ${run.ms} | ${run.chars} | ${run.attempts} | ${note} |`,
    )
  })
  const passed = runs.length > 0 && runs.every((run) => run.ok)
  lines.push("")
  lines.push(
    passed
      ? "**G0 passed** — this path reaches the model from inside OpenCode."
      : "**G0 failed** — this path is refused or unavailable.",
  )
  if (mode === "text" && !passed) {
    lines.push(
      "",
      "`ctx.generate.text` is expected to fail for free-tier models: it sends no client",
      "identity, so the gateway rejects it. Use the session path.",
    )
  }
  if (runs[0]?.ok) lines.push("", "First response:", "", "```", runs[0].preview, "```")
  const first = runs.find((run) => !run.ok)
  if (first?.error) {
    lines.push("", "First failure:", "", "```", first.error.message, "```")
  }
  return lines.join("\n")
}
