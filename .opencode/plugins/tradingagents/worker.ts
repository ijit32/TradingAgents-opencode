/**
 * The bridge to the Python worker.
 *
 * One action per subprocess: `uv run python -m tradingagents.opencode_worker
 * <action> --run-dir <path>`. The run directory carries the payload; stdout
 * carries a single JSON line. Exit codes are the protocol:
 *
 *   0 ok · 2 validation failed (retryable) · 3 bad input · 4 vendor missing · 1 internal
 *
 * The worker never calls a model — every generation happens in `generate.ts`.
 */

import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { mkdir, unlink, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import type { WorkerReply } from "./types"

export const EXIT_OK = 0
export const EXIT_INTERNAL = 1
export const EXIT_VALIDATION = 2
export const EXIT_BAD_INPUT = 3
export const EXIT_VENDOR = 4

/** A worker run that ended non-zero, with whatever the worker managed to say. */
export class WorkerError extends Error {
  readonly code: number
  readonly reply: WorkerReply | null
  readonly stderr: string

  constructor(message: string, code: number, reply: WorkerReply | null, stderr: string) {
    super(message)
    this.name = "WorkerError"
    this.code = code
    this.reply = reply
    this.stderr = stderr
  }

  get validationErrors(): readonly string[] {
    const listed = this.reply?.errors
    if (Array.isArray(listed)) return listed.map(String)
    const single = this.reply?.error
    return typeof single === "string" && single ? [single] : [this.message]
  }
}

export interface WorkerInvocation {
  readonly action:
    | "init"
    | "plan"
    | "collect"
    | "prompt"
    | "validate"
    | "debates"
    | "memory-begin"
    | "memory-apply"
    | "settle"
    | "backtest-plan"
    | "backtest-summary"
    | "status"
  readonly runDir: string
  readonly stage?: string
  readonly analyst?: string
  readonly lenient?: boolean
  readonly jsonArgs?: Record<string, unknown>
}

export interface WorkerOptions {
  /** Project directory the worker runs in; `uv run` needs the project root. */
  readonly directory: string
  /** `uv`, or a path to a Python interpreter to call directly. */
  readonly python?: string
  readonly timeoutMs?: number
  readonly signal?: AbortSignal
}

interface Command {
  readonly file: string
  readonly prefix: readonly string[]
}

/** Windows resolves `uv`/`python` through PATHEXT, but spawn needs the suffix. */
export function executable(name: string): string {
  if (process.platform !== "win32") return name
  if (/\.(exe|cmd|bat)$/i.test(name)) return name
  return `${name}.exe`
}

export function pythonCommand(python: string): Command {
  const base = python.split(/[\\/]/).pop() ?? python
  if (base.startsWith("python")) return { file: executable(python), prefix: [] }
  return { file: executable(python), prefix: ["run", "python"] }
}

/**
 * Fail before spawning when the project or `uv` is missing.
 *
 * A bare ENOENT from `spawn` does not say which of the two is absent, and the
 * fix is different: open the repo root, or install uv.
 */
export function assertWorkerEnvironment(options: WorkerOptions): void {
  const directory = options.directory
  if (!existsSync(directory)) {
    throw new Error(`project directory missing: ${directory}`)
  }
  if (!existsSync(join(directory, "pyproject.toml"))) {
    throw new Error(
      `no pyproject.toml in ${directory}; run OpenCode from the repository root`,
    )
  }
  const runner = options.python ?? "uv"
  const file = pythonCommand(runner).file
  if (file !== "uv.exe" && runner !== "uv" && !existsSync(file)) {
    throw new Error(`python runner not found: ${file}`)
  }
}

function timeoutFor(action: WorkerInvocation["action"]): number {
  // Collection and price fetches are the actions that reach a vendor.
  if (action === "collect" || action === "memory-begin") return 300_000
  if (action === "init" || action === "backtest-plan" || action === "backtest-summary") {
    return 60_000
  }
  return 120_000
}

function lastJsonLine(stdout: string): WorkerReply | null {
  const lines = stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index]
    if (!line.startsWith("{")) continue
    try {
      const parsed = JSON.parse(line) as WorkerReply
      if (parsed && typeof parsed === "object") return parsed
    } catch {
      // A stray log line is not the reply; keep looking upward.
    }
  }
  return null
}

function childEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    // Keep vendor warnings out of stdout's way; stderr is captured separately.
    PYTHONIOENCODING: "utf-8",
  }
}

/** Run one worker action and return its reply. Throws `WorkerError` on non-zero exit. */
export async function runWorker(
  invocation: WorkerInvocation,
  options: WorkerOptions,
): Promise<WorkerReply> {
  const argsPath = invocation.jsonArgs
    ? join(invocation.runDir, `.${invocation.action}-args.json`)
    : undefined
  if (argsPath && invocation.jsonArgs) {
    await mkdir(dirname(argsPath), { recursive: true })
    await writeFile(argsPath, JSON.stringify(invocation.jsonArgs), "utf8")
  }

  const { file, prefix } = pythonCommand(options.python ?? "uv")
  const argv = [
    ...prefix,
    "-m",
    "tradingagents.opencode_worker",
    invocation.action,
    "--run-dir",
    invocation.runDir,
  ]
  if (invocation.stage) argv.push("--stage", invocation.stage)
  if (invocation.analyst) argv.push("--analyst", invocation.analyst)
  if (invocation.lenient) argv.push("--lenient")
  if (argsPath) argv.push("--json-args", argsPath)

  const timeoutMs = options.timeoutMs ?? timeoutFor(invocation.action)
  try {
    return await spawnWorker(file, argv, options, timeoutMs)
  } finally {
    if (argsPath) await unlink(argsPath).catch(() => undefined)
  }
}

function spawnWorker(
  file: string,
  argv: readonly string[],
  options: WorkerOptions,
  timeoutMs: number,
): Promise<WorkerReply> {
  return new Promise<WorkerReply>((resolve, reject) => {
    try {
      assertWorkerEnvironment(options)
    } catch (error) {
      reject(error instanceof Error ? error : new Error(String(error)))
      return
    }
    const child = spawn(file, [...argv], {
      cwd: options.directory,
      env: childEnv(),
      stdio: ["ignore", "pipe", "pipe"],
    })

    let stdout = ""
    let stderr = ""
    let settled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const finish = (work: () => void): void => {
      if (settled) return
      settled = true
      if (timer !== undefined) clearTimeout(timer)
      options.signal?.removeEventListener("abort", onAbort)
      work()
    }

    const fail = (message: string, code: number): void => {
      finish(() => {
        const reply = lastJsonLine(stdout)
        const detail = reply?.error ?? message
        reject(new WorkerError(String(detail), code, reply, stderr.trim()))
      })
    }

    const onAbort = (): void => {
      child.kill("SIGTERM")
      fail("worker run was cancelled", EXIT_INTERNAL)
    }

    if (options.signal?.aborted) {
      child.kill("SIGTERM")
      reject(new WorkerError("worker run was cancelled", EXIT_INTERNAL, null, ""))
      return
    }
    options.signal?.addEventListener("abort", onAbort, { once: true })

    timer = setTimeout(() => {
      child.kill("SIGTERM")
      // Give the process a moment to flush before SIGKILL.
      setTimeout(() => child.kill("SIGKILL"), 2_000)
      fail(`worker action did not finish within ${timeoutMs}ms`, EXIT_INTERNAL)
    }, timeoutMs)

    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk
    })
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk
      // Bound the error text we can carry back to the user.
      if (stderr.length > 8_000) stderr = stderr.slice(-8_000)
    })

    child.on("error", (error) => {
      // Spawn failures (uv missing, cwd gone) never reach an exit code.
      const hint = existsSync(options.directory)
        ? ""
        : ` (project directory missing: ${options.directory})`
      fail(`${file} could not be started: ${error.message}${hint}`, EXIT_INTERNAL)
    })

    child.on("close", (code) => {
      const reply = lastJsonLine(stdout)
      if (code === EXIT_OK) {
        finish(() => {
          if (!reply) {
            reject(
              new WorkerError(
                "worker exited 0 without a JSON reply",
                EXIT_INTERNAL,
                null,
                stderr.trim(),
              ),
            )
            return
          }
          resolve(reply)
        })
        return
      }
      const detail =
        reply?.error ?? (stderr.trim().split(/\r?\n/).pop() || `worker exited ${code ?? "?"}`)
      fail(String(detail), code ?? EXIT_INTERNAL)
    })
  })
}

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
