/**
 * The session registry: one persistent worker per project and runner.
 *
 * Actions share the registry, so a run gets the same child for every stage.
 * `TA_OPENCODE_WORKER=oneshot` bypasses it for debugging.
 */

import type { WorkerReply } from "./types"
import { runWorkerOnce, type WorkerInvocation, type WorkerOptions } from "./oneshot"
import { WorkerSession } from "./session"

const sessions = new Map<string, WorkerSession>()

function sessionKey(options: WorkerOptions): string {
  return `${options.directory}\u0000${options.python ?? "uv"}`
}

/** The session for this project and runner, creating it on first use. */
export function getWorkerSession(options: WorkerOptions): WorkerSession {
  const key = sessionKey(options)
  let session = sessions.get(key)
  if (!session) {
    session = new WorkerSession(options)
    sessions.set(key, session)
  }
  if (options.onLog) session.setLogHandler(options.onLog)
  return session
}

/** Run one action over the shared session. */
export function runWorker(
  invocation: WorkerInvocation,
  options: WorkerOptions,
): Promise<WorkerReply> {
  if (process.env.TA_OPENCODE_WORKER === "oneshot") {
    return runWorkerOnce(invocation, options)
  }
  return getWorkerSession(options).request(invocation, options)
}

/** Close every shared session; safe to call when none were opened. */
export async function closeWorkerSessions(): Promise<void> {
  const open = [...sessions.values()]
  sessions.clear()
  await Promise.allSettled(open.map((session) => session.close()))
}
