/**
 * The `/ta-*` commands: a line of text in, a synthetic message out.
 */

import type { Plugin } from "@opencode/plugin"
import type { CommandEditor } from "./editor-types"
import { renderBacktest, runBacktest } from "./backtest"
import { parseAnalysisText, parseBacktestText, parseProbeText } from "./parse"
import { readReport, renderSummary, runAnalysis } from "./pipeline"
import { assertWorkerPermission } from "./permissions"
import { runProbe } from "./probe"
import { errorMarkdown } from "./render"

export function registerTaCommands(ctx: Plugin.Context, editor: CommandEditor): void {
  editor.add({
    name: "ta-analyze",
    description:
      "Run the TradingAgents pipeline: /ta-analyze NVDA 2026-10-23 [--depth 2] [--model provider/model]",
    execute: async ({ sessionID, prompt }) => {
      const input = parseAnalysisText(prompt.text ?? "")
      const progress = async (message: string): Promise<void> => {
        if (!ctx.session.synthetic) return
        await ctx.session.synthetic({ sessionID, text: message })
      }
      try {
        await assertWorkerPermission(ctx, sessionID)
        await progress(`TradingAgents: starting ${input.ticker ?? "run"}…`)
        const summary = await runAnalysis(ctx, input, { progress })
        await progress(renderSummary(summary))
      } catch (error) {
        await progress(errorMarkdown("TradingAgents run", error))
      }
    },
  })

  editor.add({
    name: "ta-backtest",
    description:
      "Run a sweep: /ta-backtest NVDA,AAPL 2026-01-02 2026-03-01 [--every 7] [--run-id id]",
    execute: async ({ sessionID, prompt }) => {
      const input = parseBacktestText(prompt.text ?? "")
      const progress = async (message: string): Promise<void> => {
        if (!ctx.session.synthetic) return
        await ctx.session.synthetic({ sessionID, text: message })
      }
      try {
        await assertWorkerPermission(ctx, sessionID)
        await progress(`TradingAgents backtest: ${(input.tickers ?? []).join(", ") || "sweep"}…`)
        const result = await runBacktest(ctx, input, { progress })
        await progress(renderBacktest(result))
      } catch (error) {
        await progress(errorMarkdown("TradingAgents backtest", error))
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
        await ctx.session.synthetic?.({ sessionID, text: view.content })
      } catch (error) {
        await ctx.session.synthetic?.({
          sessionID,
          text: errorMarkdown("TradingAgents report", error),
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
      await ctx.session.synthetic?.({
        sessionID,
        text: "Running OpenCode generation probe…",
      })
      try {
        const { report } = await runProbe(ctx, input)
        await ctx.session.synthetic?.({ sessionID, text: report })
      } catch (error) {
        await ctx.session.synthetic?.({
          sessionID,
          text: errorMarkdown("OpenCode generation probe", error),
        })
      }
    },
  })
}
