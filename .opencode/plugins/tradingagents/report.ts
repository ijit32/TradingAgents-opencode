/**
 * What a run leaves behind: `final_state.json`, the terminal summary, and the
 * readers behind `trading_report` and `trading_status`.
 */

import type { Plugin } from "@opencode/plugin"
import { readdir, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { readCheckpoint } from "./checkpoint"
import { expandHome, readConfig } from "./config"
import type { RunSummary } from "./types"
import { runWorker } from "./worker"
import type { RunContext, StageSpec } from "./runtime"
import { PipelineError, readTextOrNull } from "./runtime"
import { latestRun, readJson, resolveRunId } from "./run-dir"

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

export { writeFinalState }
