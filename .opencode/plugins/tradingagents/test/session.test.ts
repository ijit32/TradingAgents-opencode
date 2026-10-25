import { describe, expect, test } from "bun:test"
import { EventEmitter } from "node:events"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PassThrough } from "node:stream"
import { WorkerError } from "../oneshot"
import type { WorkerRequestFrame } from "../protocol"
import { PROTOCOL_VERSION } from "../protocol"
import { WorkerSession, type SpawnFn } from "../session"

type Reply = Record<string, unknown>

class FakeChild extends EventEmitter {
  readonly stdin = new PassThrough()
  readonly stdout = new PassThrough()
  readonly stderr = new PassThrough()
  killed: NodeJS.Signals | null = null
  readonly frames: WorkerRequestFrame[] = []
  onFrame: (frame: WorkerRequestFrame) => void = () => {}
  private buffer = ""

  constructor(
    readonly file: string,
    readonly args: readonly string[],
  ) {
    super()
    this.stdin.on("data", (chunk: Buffer | string) => this.read(chunk.toString()))
  }

  reply(frame: WorkerRequestFrame, payload: Reply): void {
    this.write({ protocol_version: PROTOCOL_VERSION, id: frame.id, action: frame.action, ...payload })
  }

  write(raw: Record<string, unknown>): void {
    this.stdout.write(`${JSON.stringify(raw)}\n`)
  }

  kill(signal?: NodeJS.Signals): boolean {
    this.killed = signal ?? "SIGTERM"
    return true
  }

  private read(chunk: string): void {
    this.buffer += chunk
    const lines = this.buffer.split(/\r?\n/)
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      const frame = JSON.parse(line) as WorkerRequestFrame
      this.frames.push(frame)
      this.onFrame(frame)
    }
  }
}

interface Harness {
  readonly session: WorkerSession
  readonly children: FakeChild[]
  readonly logs: Record<string, unknown>[]
  readonly dir: string
  readonly options: { directory: string; python: string; timeoutMs?: number }
  close(): Promise<void>
}

async function harness(onRequest?: (frame: WorkerRequestFrame, child: FakeChild) => void): Promise<Harness> {
  const dir = await mkdtemp(join(tmpdir(), "ta-session-"))
  await writeFile(join(dir, "pyproject.toml"), "")
  const python = join(dir, "python3")
  await writeFile(python, "")

  const children: FakeChild[] = []
  const logs: Record<string, unknown>[] = []
  const spawnFn: SpawnFn = (file, args) => {
    const child = new FakeChild(file, args)
    children.push(child)
    child.onFrame = (frame) => {
      if (frame.action === "ping") {
        child.reply(frame, { ok: true, ready: true })
        return
      }
      if (frame.action === "shutdown") {
        child.reply(frame, { ok: true, ready: false })
        return
      }
      onRequest?.(frame, child)
    }
    return child as never
  }

  const options = { directory: dir, python, timeoutMs: 500 }
  const session = new WorkerSession({ ...options, onLog: (record) => logs.push(record) }, spawnFn)
  return {
    session,
    children,
    logs,
    dir,
    options,
    async close() {
      await session.close()
      await rm(dir, { recursive: true, force: true })
    },
  }
}

describe("WorkerSession", () => {
  test("spawns serve once and answers by request id", async () => {
    const h = await harness((frame, child) => {
      child.reply(frame, { ok: true, action: frame.action, chars: 3 })
    })
    try {
      const first = await h.session.request({ action: "status", runDir: h.dir })
      const second = await h.session.request({ action: "plan", runDir: h.dir })

      expect(h.children).toHaveLength(1)
      expect(h.children[0].args).toEqual(["-m", "tradingagents.opencode_worker", "serve"])
      expect(first.id).toBe(2)
      expect(second.id).toBe(3)
      expect(first.action).toBe("status")
      expect(second.action).toBe("plan")
    } finally {
      await h.close()
    }
  })

  test("maps concurrent replies even when they arrive out of order", async () => {
    let resolveBoth: (() => void) | undefined
    const both = new Promise<void>((resolve) => {
      resolveBoth = resolve
    })
    let plan: WorkerRequestFrame | undefined
    let status: WorkerRequestFrame | undefined
    const h = await harness((frame, child) => {
      if (frame.action === "plan") plan = frame
      if (frame.action === "status") status = frame
      if (!plan || !status) return
      resolveBoth?.()
      // Answer the later request first, then the earlier one.
      child.reply(status, { ok: true, action: "status", order: ["status"] })
      child.reply(plan, { ok: true, action: "plan", order: ["plan"] })
    })
    try {
      const planReply = h.session.request({ action: "plan", runDir: h.dir })
      const statusReply = h.session.request({ action: "status", runDir: h.dir })
      await both

      expect((await planReply).order).toEqual(["plan"])
      expect((await statusReply).order).toEqual(["status"])
    } finally {
      await h.close()
    }
  })

  test("turns a structured failure into WorkerError with its exit code", async () => {
    const h = await harness((frame, child) => {
      child.reply(frame, { ok: false, error: "too short", errors: ["short"], exit_code: 2 })
    })
    try {
      const error = await h.session
        .request({ action: "validate", runDir: h.dir, stage: "bull_researcher_1" })
        .catch((caught: unknown) => caught)
      expect(error).toBeInstanceOf(WorkerError)
      expect((error as WorkerError).code).toBe(2)
      expect((error as WorkerError).validationErrors).toEqual(["short"])
      expect(h.session.alive).toBe(true)
    } finally {
      await h.close()
    }
  })

  test("kills the session on a protocol mismatch", async () => {
    const h = await harness((frame, child) => {
      child.write({ protocol_version: 99, id: frame.id, ok: true })
    })
    try {
      await expect(h.session.request({ action: "plan", runDir: h.dir })).rejects.toThrow(
        /update both/,
      )
      expect(h.session.alive).toBe(false)
      expect(h.children[0].killed).not.toBeNull()
    } finally {
      await h.close()
    }
  })

  test("times out, kills the worker, and restarts on the next request", async () => {
    let respond = false
    const h = await harness((frame, child) => {
      if (respond) child.reply(frame, { ok: true, action: frame.action })
    })
    try {
      await expect(
        h.session.request({ action: "plan", runDir: h.dir }, { ...h.options, timeoutMs: 20 }),
      ).rejects.toThrow(/within 20ms/)
      expect(h.children[0].killed).not.toBeNull()

      respond = true
      const reply = await h.session.request({ action: "status", runDir: h.dir })
      expect(reply.ok).toBe(true)
      expect(h.children).toHaveLength(2)
    } finally {
      await h.close()
    }
  })

  test("restarts after an unexpected exit", async () => {
    let respond = false
    const h = await harness((frame, child) => {
      if (respond) child.reply(frame, { ok: true, action: frame.action })
    })
    try {
      const pending = h.session.request({ action: "collect", runDir: h.dir })
      await new Promise((resolve) => setTimeout(resolve, 10))
      h.children[0].emit("close", 1)
      await expect(pending).rejects.toThrow(/worker exited 1/)

      respond = true
      expect((await h.session.request({ action: "status", runDir: h.dir })).ok).toBe(true)
      expect(h.children).toHaveLength(2)
    } finally {
      await h.close()
    }
  })

  test("abort cancels the request and kills the worker", async () => {
    const h = await harness(() => {})
    try {
      const controller = new AbortController()
      const pending = h.session.request(
        { action: "plan", runDir: h.dir },
        { ...h.options, signal: controller.signal },
      )
      setTimeout(() => controller.abort(), 10)
      await expect(pending).rejects.toThrow(/cancelled/)
      expect(h.children[0].killed).not.toBeNull()
    } finally {
      await h.close()
    }
  })

  test("forwards structured stderr logs", async () => {
    const h = await harness((frame, child) => {
      child.stderr.write('{"event":"worker.request","action":"plan","duration_ms":4}\n')
      child.reply(frame, { ok: true, action: frame.action })
    })
    try {
      await h.session.request({ action: "plan", runDir: h.dir })
      expect(h.logs).toContainEqual({
        event: "worker.request",
        action: "plan",
        duration_ms: 4,
      })
    } finally {
      await h.close()
    }
  })

  test("close sends shutdown and kills the child", async () => {
    const h = await harness((frame, child) => {
      child.reply(frame, { ok: true, action: frame.action })
    })
    await h.session.request({ action: "status", runDir: h.dir })
    await h.session.close()

    const frames = h.children[0].frames.map((frame) => frame.action)
    expect(frames).toContain("shutdown")
    expect(h.children[0].killed).not.toBeNull()
    expect(h.session.alive).toBe(false)
    await rm(h.dir, { recursive: true, force: true })
  })

  test("refuses new requests after close", async () => {
    const h = await harness()
    await h.session.close()
    await expect(h.session.request({ action: "plan", runDir: h.dir })).rejects.toThrow(/closed/)
    await rm(h.dir, { recursive: true, force: true })
  })
})
