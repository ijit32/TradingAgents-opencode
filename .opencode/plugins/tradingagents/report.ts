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
import { PipelineError, readTextOrNull, titleOf } from "./runtime"
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

type SummaryStage = RunSummary["stages"][number]

/** The six timeline groups, in display order. */
const GROUPS = [
  ["analysts", "Analysts"],
  ["research_debate", "Debate"],
  ["research_manager", "Manager"],
  ["trader", "Trader"],
  ["risk_debate", "Risk"],
  ["portfolio_manager", "PM"],
] as const

/** Analyst stages in pipeline order, with the short name shown in the card. */
const ANALYSTS: readonly (readonly [stage: string, short: string])[] = [
  ["market_analyst", "market"],
  ["sentiment_analyst", "sentiment"],
  ["news_analyst", "news"],
  ["fundamentals_analyst", "fundamentals"],
] as const

const MAX_WARNINGS = 6

/** `14.2s`, `6m 14s`, or `1h 2m`. */
export function formatDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 3_600_000) {
    const seconds = Math.round(ms / 1000)
    const rest = seconds % 60
    return rest === 0 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 60)}m ${rest}s`
  }
  const minutes = Math.round(ms / 60_000)
  const rest = minutes % 60
  return rest === 0 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 60)}h ${rest}m`
}

function glyph(status: string): string {
  if (status === "ok") return "✓"
  if (status === "failed") return "✗"
  return "~"
}

function rollup(stages: readonly SummaryStage[]): string {
  if (stages.some((stage) => stage.status === "failed")) return "✗"
  if (stages.some((stage) => stage.status === "ok")) return "✓"
  return "~"
}

/** `2 rounds`, `1 round`, … */
function roundsText(count: number): string {
  return count === 1 ? "1 round" : `${count} rounds`
}

/**
 * One timeline row.
 *
 * A table, not bare lines: GFM soft-wraps consecutive lines into one paragraph,
 * which would collapse the whole timeline into a single row in the TUI.
 */
function row(label: string, mark: string, detail: string): string {
  return `| ${label} | ${mark} | ${detail} |`
}

/**
 * The run card shown when an analysis finishes.
 *
 * Compact by design: the timeline rolls each stage group up to one glyph, and
 * the report bodies stay on disk — `trading_report` reads them back on demand.
 */
export function renderSummary(summary: RunSummary): string {
  const rating = summary.rating ?? "no rating"
  const lines = [
    `### ${summary.ticker} · ${summary.tradeDate} · ${rating}`,
    "",
    `Depth ${summary.depth} · ${summary.stages.length} stages · ${summary.calls} calls · ` +
      `${formatDuration(summary.elapsedMs)} · run \`${summary.runId}\``,
    "",
  ]

  const byGroup = new Map<string, SummaryStage[]>()
  for (const stage of summary.stages) {
    const group = stage.group || ""
    const bucket = byGroup.get(group)
    if (bucket) bucket.push(stage)
    else byGroup.set(group, [stage])
  }

  const timeline: string[] = []
  for (const [group, label] of GROUPS) {
    const stages = byGroup.get(group) ?? []
    if (!stages.length) continue
    if (group === "analysts") {
      const present = ANALYSTS.flatMap(([stage, short]) => {
        const entry = stages.find((candidate) => candidate.stage === stage)
        return entry ? [{ entry, short }] : []
      })
      const known = new Set<string>(ANALYSTS.map(([stage]) => stage))
      for (const entry of stages) {
        if (known.has(entry.stage)) continue
        present.push({ entry, short: entry.stage.replace(/_analyst$/, "") })
      }
      timeline.push(
        row(
          label,
          present.map(({ entry }) => glyph(entry.status)).join(""),
          present.map(({ short }) => short).join(" · "),
        ),
      )
      continue
    }
    if (group === "research_debate") {
      timeline.push(row(label, rollup(stages), roundsText(Math.max(1, Math.ceil(stages.length / 2)))))
      continue
    }
    if (group === "risk_debate") {
      timeline.push(row(label, rollup(stages), roundsText(Math.max(1, Math.ceil(stages.length / 3)))))
      continue
    }
    const detail =
      group === "research_manager"
        ? "Research manager"
        : group === "trader"
          ? "Trader"
          : rating
    timeline.push(row(label, rollup(stages), detail))
  }

  const slowest = summary.stages
    .filter((stage) => stage.status === "ok")
    .reduce<SummaryStage | null>(
      (best, stage) => (best && best.ms >= stage.ms ? best : stage),
      null,
    )
  if (timeline.length) {
    lines.push("| Phase | Status | Detail |", "|---|---|---|", ...timeline)
  }
  if (slowest && summary.elapsedMs >= 4_000 && slowest.ms * 4 >= summary.elapsedMs) {
    lines.push("", `_Slowest: ${titleOf(slowest.stage)} ${formatDuration(slowest.ms)}_`)
  }

  if (summary.warnings.length) {
    lines.push("", `**Warnings (${summary.warnings.length})**`)
    for (const warning of summary.warnings.slice(0, MAX_WARNINGS)) lines.push(`- ${warning}`)
    if (summary.warnings.length > MAX_WARNINGS) {
      const more = summary.warnings.length - MAX_WARNINGS
      lines.push(`_+${more} more in \`${summary.runDir}/checkpoint.json\`_`)
    }
  }

  const artifacts: string[] = []
  if (summary.reportMd) artifacts.push(`- Report \`${summary.reportMd}\``)
  if (summary.reportHtml) artifacts.push(`- HTML \`${summary.reportHtml}\``)
  if (summary.memoryLog) artifacts.push(`- Memory \`${summary.memoryLog}\``)
  if (summary.stateLog) artifacts.push(`- State \`${summary.stateLog}\``)
  if (artifacts.length) {
    lines.push("", "**Artifacts**", `- Directory \`${summary.runDir}\``, ...artifacts)
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
