/**
 * The bridge to the Python worker.
 *
 * The default transport is a persistent `serve` process (`session.ts`): one
 * spawn per OpenCode session, one newline-delimited JSON frame per action. Set
 * `TA_OPENCODE_WORKER=oneshot` to fall back to one subprocess per action, which
 * is useful when debugging a single action in isolation.
 *
 * The worker never calls a model — every generation happens in `generate.ts`.
 */

import { existsSync } from "node:fs"
import { EXIT_BAD_INPUT, WorkerError, type WorkerOptions } from "./oneshot"
import { runWorker } from "./session"
import type { WorkerReply } from "./types"

export {
  EXIT_BAD_INPUT,
  EXIT_INTERNAL,
  EXIT_OK,
  EXIT_VALIDATION,
  EXIT_VENDOR,
  WorkerError,
  assertWorkerEnvironment,
  executable,
  pythonCommand,
  runWorkerOnce,
  timeoutFor,
} from "./oneshot"
export type { Command, WorkerInvocation, WorkerOptions } from "./oneshot"
export {
  PROTOCOL_VERSION,
  WorkerSession,
  closeWorkerSessions,
  getWorkerSession,
  runWorker,
} from "./session"
export type { SpawnFn } from "./session"
export {
  asReplyFrame,
  decodeFrame,
  encodeFrame,
  requestFrame,
  versionMismatch,
} from "./protocol"
export type { WorkerAction, WorkerReplyFrame, WorkerRequestFrame } from "./protocol"

/** `status` over a run directory, or `null` when the directory is gone. */
export async function workerStatus(
  runDir: string,
  options: WorkerOptions,
): Promise<WorkerReply | null> {
  if (!existsSync(runDir)) return null
  try {
    return await runWorker({ action: "status", runDir }, options)
  } catch (error) {
    if (error instanceof WorkerError && error.code === EXIT_BAD_INPUT) return null
    throw error
  }
}
