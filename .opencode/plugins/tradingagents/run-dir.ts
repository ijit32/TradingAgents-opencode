/**
 * Run directories: where a run lives, how an id resolves, and how "latest" is
 * found. Nothing here calls a model or reads a stage's output.
 */

import { mkdir, readdir } from "node:fs/promises"
import { isAbsolute, join, relative, resolve } from "node:path"
import { newRunId, uniqueRunDir } from "./checkpoint"
import type { TradingConfig } from "./config"
import { expandHome } from "./config"
import type { AnalyzeInput } from "./types"
import { PipelineError, readTextOrNull } from "./runtime"

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

export {
  type ResolvedRunDir,
  latestRun,
  latestRunFor,
  readJson,
  resolveRunDir,
  runDirs,
}
