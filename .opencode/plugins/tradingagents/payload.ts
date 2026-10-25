/**
 * `request.json`: what the run is, merged with a resumed run's saved settings.
 */

import type { TradingConfig } from "./config"
import { expandHome, formatModelRef, parseModelRef } from "./config"
import type { AnalyzeInput } from "./types"
import type { RequestPayload } from "./runtime"
import { portfolioOf } from "./runtime"

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

export { buildPayload }
