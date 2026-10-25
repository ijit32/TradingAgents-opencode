/**
 * TradingAgents OpenCode plugin.
 *
 * The plugin owns the analysis pipeline: it creates a run directory, shells out
 * to the Python worker for data collection and prompt rendering, calls OpenCode
 * models for every generation, and checkpoints each stage so a run resumes
 * without repeating model calls.
 *
 * Registered here:
 *   `/ta-analyze`       — run the pipeline from a command line
 *   `trading_analyze`   — the same run, callable from chat
 *   `trading_backtest`  — a sweep over a date grid, one isolated memory log
 *   `trading_report`    — the written report of a finished run
 *   `trading_status`    — what a run has produced so far
 *   `trading_probe`     — Phase 0 diagnostic (generation reachability)
 *
 * Registration lives in `tools.ts` and `commands.ts`; parsing, rendering, and
 * permissions are separate modules so each can be tested on its own.
 */

import { Plugin } from "@opencode/plugin"
import { registerTaCommands } from "./commands"
import { registerTradingTools } from "./tools"
import { closeWorkerSessions } from "./worker"

export default Plugin.define({
  id: "tradingagents",

  async setup(ctx) {
    const toolRegistration = await ctx.tool.transform((editor) =>
      registerTradingTools(ctx, editor),
    )

    const commandRegistration = await ctx.command.transform((editor) =>
      registerTaCommands(ctx, editor),
    )

    return async () => {
      await commandRegistration.dispose()
      await toolRegistration.dispose()
      await closeWorkerSessions()
    }
  },
})
