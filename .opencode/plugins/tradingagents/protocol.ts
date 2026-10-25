/**
 * Framing helpers for the worker protocol.
 *
 * The canonical contract lives in `protocol.schema.json`; `protocol.generated.ts`
 * is generated from it and must not be edited by hand. This module adds the two
 * things a schema cannot express: newline framing and a version check.
 */

import type {
  TradingAgentsWorkerProtocol,
  WorkerAction,
  WorkerReplyFrame,
  WorkerRequestFrame,
} from "./protocol.generated"

export const PROTOCOL_VERSION = 1 as const

export type { TradingAgentsWorkerProtocol, WorkerAction, WorkerReplyFrame, WorkerRequestFrame }

/** One request line. */
export function encodeFrame(frame: WorkerRequestFrame): string {
  return `${JSON.stringify(frame)}\n`
}

/**
 * Decode one reply line, or `null` when the line is not a v1 reply.
 *
 * A worker may print nothing else on stdout, but a stray line is ignored
 * rather than failing the session: the id mapping still finds the real reply.
 */
export function decodeFrame(line: string): WorkerReplyFrame | null {
  const text = line.trim()
  if (!text.startsWith("{")) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  return asReplyFrame(parsed)
}

export function asReplyFrame(value: unknown): WorkerReplyFrame | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const frame = value as Record<string, unknown>
  if (frame.protocol_version !== PROTOCOL_VERSION) return null
  if (typeof frame.ok !== "boolean") return null
  const id = frame.id
  if (typeof id !== "number" && id !== null) return null
  return frame as unknown as WorkerReplyFrame
}

export function requestFrame(
  id: number,
  action: WorkerAction,
  fields: Omit<WorkerRequestFrame, "protocol_version" | "id" | "action"> = {},
): WorkerRequestFrame {
  return { protocol_version: PROTOCOL_VERSION, id, action, ...fields }
}

/** A human-readable version mismatch, or `null` when the reply is compatible. */
export function versionMismatch(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const raw = (value as Record<string, unknown>).protocol_version
  if (raw === undefined || raw === PROTOCOL_VERSION) return null
  return `worker speaks protocol ${String(raw)}, this plugin speaks ${PROTOCOL_VERSION}; update both`
}
