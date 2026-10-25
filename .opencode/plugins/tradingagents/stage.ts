/**
 * One stage: collect its pack, render the prompt, generate, validate, repair.
 *
 * A stage never decides the run's order; it only knows how to turn one planned
 * stage into a checkpointed record.
 */

import { readFile, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { shouldSkip } from "./checkpoint"
import type { ModelRef } from "./config"
import { formatModelRef, parseModelRef } from "./config"
import { GenerateError, generateWithFallback } from "./generate"
import type { StageRecord, WorkerReply } from "./types"
import { EXIT_VALIDATION, WorkerError, runWorker } from "./worker"
import type { RunContext, StageSpec } from "./runtime"
import { PipelineError, ensurePack, formatReply, progress, replyHash, throwIfAborted } from "./runtime"

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

export { type StageOutcome, type StagePlan, executeStage, prepareStage, runWithConcurrency }
