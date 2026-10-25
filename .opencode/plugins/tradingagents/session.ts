/**
 * A persistent worker session.
 *
 * `uv run python -m tradingagents.opencode_worker serve` is spawned once and
 * kept alive for the rest of the OpenCode session. Requests are newline-
 * delimited JSON frames; every reply echoes the request id, so a slow `collect`
 * cannot be mistaken for the `status` sent after it.
 *
 * Failure policy: one bad request fails one request when the worker can answer
 * with a structured error. A timeout, abort, crash, or protocol mismatch kills
 * the session and rejects everything in flight; the next request starts a fresh
 * worker. A half-alive process is never reused.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import type { WorkerReply } from "./types"
import {
  EXIT_INTERNAL,
  WorkerError,
  assertWorkerEnvironment,
  childEnv,
  pythonCommand,
  timeoutFor,
  type WorkerInvocation,
  type WorkerOptions,
} from "./oneshot"
import {
  PROTOCOL_VERSION,
  decodeFrame,
  encodeFrame,
  requestFrame,
  versionMismatch,
  type WorkerRequestFrame,
} from "./protocol"

const HANDSHAKE_TIMEOUT_MS = 60_000
const CLOSE_TIMEOUT_MS = 5_000

export type SpawnFn = (
  file: string,
  args: readonly string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; stdio: ["pipe", "pipe", "pipe"] },
) => ChildProcessWithoutNullStreams

interface Pending {
  readonly action: string
  readonly resolve: (reply: WorkerReply) => void
  readonly reject: (error: Error) => void
  readonly timer: ReturnType<typeof setTimeout>
  readonly signal?: AbortSignal
  readonly onAbort?: () => void
}

export class WorkerSession {
  private readonly base: WorkerOptions
  private readonly spawnFn: SpawnFn
  private child: ChildProcessWithoutNullStreams | null = null
  private starting: Promise<void> | null = null
  private pending = new Map<number, Pending>()
  private buffer = ""
  private stderr = ""
  private nextId = 1
  private closed = false
  private logger?: (record: Record<string, unknown>) => void

  constructor(options: WorkerOptions, spawnFn: SpawnFn = defaultSpawn) {
    this.base = options
    this.spawnFn = spawnFn
    this.logger = options.onLog
  }

  get alive(): boolean {
    return this.child !== null && !this.closed
  }

  setLogHandler(handler?: (record: Record<string, unknown>) => void): void {
    this.logger = handler
  }

  /** Send one action and wait for its reply. Rejects with `WorkerError`. */
  async request(invocation: WorkerInvocation, options: WorkerOptions = this.base): Promise<WorkerReply> {
    if (this.closed) throw new WorkerError("worker session is closed", EXIT_INTERNAL, null, "")
    const signal = options.signal
    if (signal?.aborted) {
      throw new WorkerError("worker run was cancelled", EXIT_INTERNAL, null, this.stderr.trim())
    }

    await this.ensureStarted(options)

    const id = this.nextId++
    const frame = requestFrame(id, invocation.action, {
      run_dir: invocation.runDir,
      ...(invocation.stage ? { stage: invocation.stage } : {}),
      ...(invocation.analyst ? { analyst: invocation.analyst } : {}),
      ...(invocation.lenient ? { lenient: true } : {}),
      ...(invocation.jsonArgs ? { json_args: invocation.jsonArgs } : {}),
    })
    return this.sendRaw(frame, options.timeoutMs ?? timeoutFor(invocation.action), options)
  }

  /** Ask the worker to exit, then make sure it did. */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    const child = this.child
    if (!child) {
      this.rejectAll(new WorkerError("worker session is closed", EXIT_INTERNAL, null, ""))
      return
    }
    try {
      await this.sendRaw(
        requestFrame(this.nextId++, "shutdown"),
        CLOSE_TIMEOUT_MS,
        { ...this.base, signal: undefined },
      )
    } catch {
      // A worker that cannot answer shutdown is killed below anyway.
    }
    child.stdin.end()
    child.kill("SIGTERM")
    this.child = null
    this.rejectAll(new WorkerError("worker session is closed", EXIT_INTERNAL, null, this.stderr.trim()))
  }

  private async ensureStarted(options: WorkerOptions): Promise<void> {
    if (this.child) return
    if (!this.starting) {
      this.starting = this.start(options).finally(() => {
        this.starting = null
      })
    }
    await this.starting
  }

  private async start(options: WorkerOptions): Promise<void> {
    try {
      assertWorkerEnvironment(this.base)
    } catch (error) {
      throw new WorkerError(messageOf(error), EXIT_INTERNAL, null, "")
    }

    const { file, prefix } = pythonCommand(this.base.python ?? "uv")
    const argv = [...prefix, "-m", "tradingagents.opencode_worker", "serve"]
    let child: ChildProcessWithoutNullStreams
    try {
      child = this.spawnFn(file, argv, {
        cwd: this.base.directory,
        env: childEnv(),
        stdio: ["pipe", "pipe", "pipe"],
      })
    } catch (error) {
      throw new WorkerError(
        `${file} could not be started: ${messageOf(error)}`,
        EXIT_INTERNAL,
        null,
        "",
      )
    }

    this.child = child
    this.buffer = ""
    this.stderr = ""
    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (chunk: string) => this.onData(chunk))
    child.stderr.on("data", (chunk: string) => this.onStderr(chunk, options))
    child.on("error", (error) => {
      this.crash(
        `worker process failed: ${error.message}`,
        "worker.process_error",
      )
    })
    child.on("close", (code) => {
      if (this.closed) return
      this.crash(`worker exited ${code ?? "?"}${this.stderrTail()}`, "worker.exit_unexpected")
    })

    try {
      await this.sendRaw(
        requestFrame(this.nextId++, "ping"),
        HANDSHAKE_TIMEOUT_MS,
        { ...this.base, signal: undefined },
      )
    } catch (error) {
      this.killChild()
      throw error
    }
  }

  private sendRaw(
    frame: WorkerRequestFrame,
    timeoutMs: number,
    options: WorkerOptions,
  ): Promise<WorkerReply> {
    const child = this.child
    if (!child) {
      return Promise.reject(
        new WorkerError("worker session is not running", EXIT_INTERNAL, null, this.stderr.trim()),
      )
    }

    return new Promise<WorkerReply>((resolve, reject) => {
      const signal = options.signal
      const onAbort = (): void => {
        this.crash("worker run was cancelled", "worker.abort")
      }
      const timer = setTimeout(() => {
        this.crash(
          `worker action did not finish within ${timeoutMs}ms`,
          "worker.timeout",
          { action: frame.action, id: frame.id, timeout_ms: timeoutMs },
        )
      }, timeoutMs)

      this.pending.set(frame.id, { action: frame.action, resolve, reject, timer, signal, onAbort })
      signal?.addEventListener("abort", onAbort, { once: true })
      if (signal?.aborted) {
        onAbort()
        return
      }

      try {
        child.stdin.write(encodeFrame(frame))
      } catch (error) {
        this.crash(`could not write to the worker: ${messageOf(error)}`, "worker.write_error")
      }
    })
  }

  private onData(chunk: string): void {
    this.buffer += chunk
    const lines = this.buffer.split(/\r?\n/)
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      const text = line.trim()
      if (!text) continue
      const reply = decodeFrame(text)
      if (!reply) {
        const mismatch = versionMismatch(safeParse(text))
        if (mismatch) {
          this.crash(mismatch, "worker.protocol_mismatch")
          return
        }
        continue
      }
      this.settle(reply)
    }
  }

  private settle(reply: WorkerReply): void {
    const id = typeof reply.id === "number" ? reply.id : null
    if (id === null) return
    const pending = this.pending.get(id)
    if (!pending) return
    this.pending.delete(id)
    clearTimeout(pending.timer)
    if (pending.signal && pending.onAbort) {
      pending.signal.removeEventListener("abort", pending.onAbort)
    }

    if (reply.ok) {
      pending.resolve(reply)
      return
    }
    pending.reject(
      new WorkerError(
        String(reply.error ?? `worker action ${pending.action} failed`),
        typeof reply.exit_code === "number" ? reply.exit_code : EXIT_INTERNAL,
        reply,
        this.stderr.trim(),
      ),
    )
  }

  private onStderr(chunk: string, options: WorkerOptions): void {
    this.stderr += chunk
    if (this.stderr.length > 8_000) this.stderr = this.stderr.slice(-8_000)
    const handler = options.onLog ?? this.logger
    if (!handler) return
    for (const line of chunk.split(/\r?\n/)) {
      const record = safeParse(line.trim())
      if (record && typeof record.event === "string") handler(record)
    }
  }

  private crash(message: string, event: string, extra: Record<string, unknown> = {}): void {
    const error = new WorkerError(message, EXIT_INTERNAL, null, this.stderr.trim())
    this.killChild()
    this.rejectAll(error)
    this.logger?.({ event, message, ...extra })
  }

  private rejectAll(error: WorkerError): void {
    const pending = [...this.pending.values()]
    this.pending.clear()
    for (const entry of pending) {
      clearTimeout(entry.timer)
      if (entry.signal && entry.onAbort) {
        entry.signal.removeEventListener("abort", entry.onAbort)
      }
      entry.reject(error)
    }
  }

  private killChild(): void {
    const child = this.child
    this.child = null
    this.buffer = ""
    if (!child) return
    try {
      child.stdin.end()
    } catch {
      // Already closed.
    }
    child.kill("SIGTERM")
    const force = setTimeout(() => child.kill("SIGKILL"), 2_000)
    force.unref?.()
    child.once("close", () => clearTimeout(force))
  }

  private stderrTail(): string {
    const lines = this.stderr.trim().split(/\r?\n/).filter(Boolean)
    const last = lines[lines.length - 1] ?? ""
    return last ? `: ${last}` : ""
  }
}

function defaultSpawn(
  file: string,
  args: readonly string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; stdio: ["pipe", "pipe", "pipe"] },
): ChildProcessWithoutNullStreams {
  return spawn(file, [...args], options)
}

function safeParse(text: string): Record<string, unknown> | null {
  if (!text.startsWith("{")) return null
  try {
    const parsed = JSON.parse(text) as unknown
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null
    return parsed as Record<string, unknown>
  } catch {
    return null
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

// ---- shared sessions -------------------------------------------------------

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
    return import("./oneshot").then((module) => module.runWorkerOnce(invocation, options))
  }
  return getWorkerSession(options).request(invocation, options)
}

/** Close every shared session; safe to call when none were opened. */
export async function closeWorkerSessions(): Promise<void> {
  const open = [...sessions.values()]
  sessions.clear()
  await Promise.allSettled(open.map((session) => session.close()))
}

export { PROTOCOL_VERSION }
