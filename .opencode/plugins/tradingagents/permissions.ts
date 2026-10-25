/**
 * The worker permission gate.
 *
 * The plugin executes local Python, so it goes through the session's `bash`
 * rules: a deny blocks the run, and an `ask` shows the usual prompt. Allowing
 * once with save adds the command to the project's allow list.
 */

import type { Plugin } from "@opencode/plugin"

/** The command the worker runs; also the resource checked against permission rules. */
export const WORKER_COMMAND = "uv run python -m tradingagents.opencode_worker"

export async function assertWorkerPermission(
  ctx: Plugin.Context,
  sessionID: string,
  source?: { type: "tool"; messageID: string; id: string },
): Promise<void> {
  await ctx.permission.assert({
    sessionID,
    ...(source ? { source } : {}),
    action: "bash",
    resources: [WORKER_COMMAND],
    save: [WORKER_COMMAND],
  })
}
