/**
 * The analysis run: one run directory, one checkpoint, one ordered stage plan.
 *
 * The worker owns the stage manifest; this module owns the order, the model
 * choice, the memory pass, resume, and the final report handoff.
 */

import type { Plugin } from "@opencode/plugin"
import { writeFile } from "node:fs/promises"
import { join } from "node:path"
import { createCheckpoint, hashJson, invalidateFrom, readCheckpoint } from "./checkpoint"
import { readConfig } from "./config"
import { resolveCandidates, resolveModel } from "./models"
import type { AnalyzeInput, RunSummary, SettleReply } from "./types"
import { runWorker } from "./worker"
import type { PipelineHooks, RunContext } from "./runtime"
import {
  CheckpointStore,
  PipelineError,
  analystsOf,
  dateOf,
  depthOf,
  formatReply,
  initRun,
  modelForRun,
  progress,
  readPlan,
  readTextOrNull,
  throwIfAborted,
  tickerOf,
  toSpec,
} from "./runtime"
import type { StagePlan } from "./stage"
import { executeStage, prepareStage, runWithConcurrency } from "./stage"
import { buildPayload } from "./payload"
import { readJson, resolveRunDir } from "./run-dir"
import type { MemoryOutcome } from "./memory-pass"
import { settleMemory } from "./memory-pass"
import { writeFinalState } from "./report"

/** Human name for a plan group; unknown groups read as their id with spaces. */
const GROUP_LABELS: Readonly<Record<string, string>> = {
  analysts: "analysts",
  research_debate: "research debate",
  research_manager: "research manager",
  trader: "trader",
  risk_debate: "risk debate",
  portfolio_manager: "portfolio manager",
}

function groupLabel(name: string): string {
  return GROUP_LABELS[name] ?? name.replace(/_/g, " ")
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

  await progress(hooks, {
    phase: "start",
    title: ticker,
    message: resumed
      ? `resuming ${String(init.run_id ?? "")}`
      : `starting ${String(init.run_id ?? "")} in ${runDir}`,
  })

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
  let groupNumber = 0

  for (const group of groups) {
    if (aborted) break
    throwIfAborted(hooks.signal)

    groupNumber += 1
    const label = groupLabel(group.name)
    await progress(hooks, {
      phase: "group",
      group: group.name,
      title: label,
      index: groupNumber,
      total: groups.length,
      message: `phase ${groupNumber}/${groups.length}: ${label}`,
    })

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
  const groupOf = new Map(specs.map((spec): [string, string] => [spec.stage, spec.group]))
  for (const stage of order) {
    const record = context.store.value.stages[stage]
    if (!record) continue
    const chars = record.out
      ? ((await readTextOrNull(join(runDir, record.out))) ?? "").trim().length
      : 0
    summaryStages.push({
      stage,
      group: groupOf.get(stage) ?? "",
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
