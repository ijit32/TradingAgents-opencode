/**
 * Parsing for the `/ta-*` commands: flags, tickers, dates, and free text.
 *
 * Tools receive structured input from the model; commands receive a line of
 * text from the user. Keeping both parsers here means the command surface can
 * be tested without loading the plugin.
 */

import type { AnalyzeInput, BacktestInput } from "./types"
import type { ProbeInput } from "./probe"

function parseAnalysisText(text: string): AnalyzeInput {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: Record<string, unknown> = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    const next = (): string | undefined => tokens[++index]
    switch (token) {
      case "--date":
      case "--trade-date":
        input.trade_date = next()
        break
      case "--depth":
        input.depth = Number(next())
        break
      case "--model":
        input.model = next()
        break
      case "--quick-model":
        input.quick_model = next()
        break
      case "--deep-model":
        input.deep_model = next()
        break
      case "--analysts":
        input.analysts = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--asset-type":
        input.asset_type = next()
        break
      case "--language":
        input.output_language = next()
        break
      case "--resume":
        input.resume_run = next() ?? ""
        break
      case "--rerun":
        input.rerun = next()
        break
      case "--rerun-from":
        input.rerun_from = next()
        break
      case "--run-dir":
        input.run_dir = next()
        break
      case "--no-memory":
        input.memory = false
        break
      case "--no-html":
        input.html = false
        break
      default:
        rest.push(token)
    }
  }
  if (rest[0] && !input.ticker) input.ticker = rest[0]
  if (rest[1] && !input.trade_date) input.trade_date = rest[1]
  return input as AnalyzeInput
}

function parseBacktestText(text: string): BacktestInput {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: Record<string, unknown> = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    const next = (): string | undefined => tokens[++index]
    switch (token) {
      case "--tickers":
        input.tickers = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--start":
        input.start = next()
        break
      case "--end":
        input.end = next()
        break
      case "--every":
      case "--every-n-days":
        input.every_n_days = Number(next())
        break
      case "--run-id":
        input.run_id = next()
        break
      case "--depth":
        input.depth = Number(next())
        break
      case "--model":
        input.model = next()
        break
      case "--quick-model":
        input.quick_model = next()
        break
      case "--deep-model":
        input.deep_model = next()
        break
      case "--analysts":
        input.analysts = (next() ?? "").split(",").map((item) => item.trim()).filter(Boolean)
        break
      case "--language":
        input.output_language = next()
        break
      case "--no-html":
        input.html = false
        break
      default:
        rest.push(token)
    }
  }
  if (rest[0] && !input.tickers) {
    input.tickers = rest[0].split(",").map((item) => item.trim()).filter(Boolean)
  }
  if (rest[1] && !input.start) input.start = rest[1]
  if (rest[2] && !input.end) input.end = rest[2]
  return input as BacktestInput
}

type MutableProbeInput = { -readonly [K in keyof ProbeInput]: ProbeInput[K] }

function parseProbeText(text: string): { input: MutableProbeInput; prompt: string } {
  const tokens = text.split(/\s+/).filter(Boolean)
  const input: MutableProbeInput = {}
  const rest: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    if (token === "--model" && tokens[index + 1]) {
      input.model = tokens[++index]
    } else if (token === "--parallel" && tokens[index + 1]) {
      input.parallel = Number(tokens[++index])
    } else if (token === "--timeout-ms" && tokens[index + 1]) {
      input.timeoutMs = Number(tokens[++index])
    } else if (token === "--mode" && tokens[index + 1]) {
      input.mode = tokens[++index] === "text" ? "text" : "session"
    } else {
      rest.push(token)
    }
  }
  return { input, prompt: rest.join(" ") }
}

export { parseAnalysisText, parseBacktestText, parseProbeText }
