/**
 * The `/ta-*` commands: a line of text in, a synthetic message out.
 *
 * A run posts at most two chat messages — a delayed start line and one final
 * card — so progress text stays out of chat.
 */

import type { Plugin } from "@opencode/plugin"
import type { CommandEditor } from "./editor-types"
import { renderBacktest, runBacktest } from "./backtest"
import { CommandReporter, type SyntheticNotice } from "./notify"
import { parseAnalysisText, parseBacktestText, parseProbeText } from "./parse"
import { readReport, renderSummary, runAnalysis } from "./pipeline"
import { assertWorkerPermission } from "./permissions"
import { runProbe } from "./probe"
import { errorMarkdown } from "./render"

export function registerTaCommands(ctx: Plugin.Context, editor: CommandEditor): void {
  const send = (notice: SyntheticNotice): Promise<unknown> | unknown =>
    ctx.session.synthetic?.(notice) ?? Promise.resolve()

  editor.add({
    name: "ta-analyze",
    description:
      "Run the TradingAgents pipeline: /ta-analyze NVDA 2026-10-23 [--depth 2] [--model provider/model]",
    execute: async ({ sessionID, prompt }) => {
      let header = "TradingAgents run"
      const reporter = new CommandReporter({ sessionID, header: () => header, send })
      try {
        const input = parseAnalysisText(prompt.text ?? "")
        header = `TradingAgents · ${input.ticker ?? "run"} · ${input.trade_date ?? "today"}`
        await assertWorkerPermission(ctx, sessionID)
        const summary = await runAnalysis(ctx, input, {
          progress: (event) => reporter.event(event),
        })
        await reporter.finish(renderSummary(summary))
      } catch (error) {
        await reporter.fail("TradingAgents run", error)
      }
    },
  })

  editor.add({
    name: "ta-backtest",
    description:
      "Run a sweep: /ta-backtest NVDA,AAPL 2026-01-02 2026-03-01 [--every 7] [--run-id id]",
    execute: async ({ sessionID, prompt }) => {
      let header = "TradingAgents backtest"
      const reporter = new CommandReporter({ sessionID, header: () => header, send })
      try {
        const input = parseBacktestText(prompt.text ?? "")
        const tickers = (input.tickers ?? [input.ticker]).filter(Boolean).join(", ")
        header = `TradingAgents backtest · ${tickers || "sweep"} · ${input.start ?? ""}`
        await assertWorkerPermission(ctx, sessionID)
        const result = await runBacktest(ctx, input, {
          progress: (event) => reporter.event(event),
        })
        await reporter.finish(renderBacktest(result))
      } catch (error) {
        await reporter.fail("TradingAgents backtest", error)
      }
    },
  })

  editor.add({
    name: "ta-report",
    description: "Show a run's written report: /ta-report [run-id] [--html]",
    execute: async ({ sessionID, prompt }) => {
      const tokens = (prompt.text ?? "").split(/\s+/).filter(Boolean)
      const format = tokens.includes("--html") ? "html" : "md"
      const runId = tokens.find((token) => !token.startsWith("--"))
      try {
        const view = await readReport(ctx, runId, format)
        await ctx.session.synthetic?.({ sessionID, text: view.content, resume: false })
      } catch (error) {
        await ctx.session.synthetic?.({
          sessionID,
          text: errorMarkdown("TradingAgents report", error),
          resume: false,
        })
      }
    },
  })

  editor.add({
    name: "ta-probe",
    description:
      "Check that OpenCode generation works for a model (default: the plugin's free quick model)",
    execute: async ({ sessionID, prompt }) => {
      const { input, prompt: text } = parseProbeText(prompt.text ?? "")
      if (text) input.prompt = text
      try {
        const { report } = await runProbe(ctx, input)
        await ctx.session.synthetic?.({ sessionID, text: report, resume: false })
      } catch (error) {
        await ctx.session.synthetic?.({
          sessionID,
          text: errorMarkdown("OpenCode generation probe", error),
          resume: false,
        })
      }
    },
  })
}
