import { describe, expect, test } from "bun:test"
import { CommandReporter, type ReporterOptions, type SyntheticNotice } from "../notify"
import { GenerateError } from "../generate"
import { PipelineError, type ProgressEvent } from "../runtime"

const SESSION_ID = "ses_test"
const CLOCK = new Date(2026, 0, 2, 9, 5)

interface FakeTimer {
  readonly run: () => void
  readonly ms: number
  cleared: boolean
}

interface Harness {
  readonly reporter: CommandReporter
  readonly sent: SyntheticNotice[]
  readonly timers: FakeTimer[]
  fire(): Promise<void>
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

function harness(overrides: Partial<ReporterOptions> = {}): Harness {
  const sent: SyntheticNotice[] = []
  const timers: FakeTimer[] = []
  const reporter = new CommandReporter({
    sessionID: SESSION_ID,
    header: "TradingAgents run",
    send: (notice) => {
      sent.push(notice)
    },
    now: () => CLOCK,
    setTimer: (run, ms) => {
      const timer: FakeTimer = { run, ms, cleared: false }
      timers.push(timer)
      return timer
    },
    clearTimer: (handle) => {
      ;(handle as FakeTimer).cleared = true
    },
    ...overrides,
  })
  return {
    reporter,
    sent,
    timers,
    async fire() {
      timers.at(-1)?.run()
      await tick()
    },
  }
}

function progress(message: string): ProgressEvent {
  return { phase: "generate", message }
}

describe("CommandReporter", () => {
  test("finishing before the delay sends only the final text", async () => {
    const { reporter, sent, timers } = harness()
    await reporter.finish("### NVDA · 2026-10-23 · Buy")

    expect(sent).toHaveLength(1)
    expect(sent[0].text).toBe("### NVDA · 2026-10-23 · Buy")
    expect(sent[0].resume).toBe(false)
    expect(sent[0].sessionID).toBe(SESSION_ID)
    expect(timers[0].cleared).toBe(true)
  })

  test("the start line is delayed, evaluated at send time, then the final text follows", async () => {
    let header = "TradingAgents run"
    const { reporter, sent, timers, fire } = harness({ header: () => header })
    header = "TradingAgents · NVDA · 2026-10-23"
    expect(sent).toHaveLength(0)

    await fire()
    expect(timers[0].ms).toBe(4_000)
    expect(sent).toHaveLength(1)
    expect(sent[0].text).toBe("TradingAgents · NVDA · 2026-10-23 · started 09:05")
    expect(sent[0].text).toContain("started ")
    expect(sent[0].resume).toBe(false)

    await reporter.finish("final")
    expect(sent).toHaveLength(2)
    expect(sent[0].text).toContain("started ")
    expect(sent[1].text).toBe("final")
    expect(sent[1].resume).toBe(false)
    expect(sent[0].sessionID).toBe(sent[1].sessionID)
  })

  test("a timer that fires after finish adds nothing", async () => {
    const { reporter, sent, fire } = harness()
    await reporter.finish("final")
    await fire()

    expect(sent).toHaveLength(1)
    expect(sent[0].text).toBe("final")
  })

  test("finishing twice sends once", async () => {
    const { reporter, sent } = harness()
    await reporter.finish("first")
    await reporter.finish("second")

    expect(sent).toHaveLength(1)
    expect(sent[0].text).toBe("first")
  })

  test("events never send and the ring keeps only the last tailSize lines", async () => {
    const { reporter, sent, fire } = harness({ tailSize: 3 })
    for (const message of ["one", "two", "three", "four", "five"]) {
      reporter.event(progress(message))
    }

    expect(sent).toHaveLength(0)
    expect(reporter.tail()).toEqual(["three", "four", "five"])
    expect(reporter.tail(2)).toEqual(["four", "five"])
    expect(reporter.tail(10)).toEqual(["three", "four", "five"])

    await fire()
    expect(sent).toHaveLength(1)
    expect(sent[0].text).toContain("started ")
  })

  test("fail sends one classified error card with at most three tail lines", async () => {
    const { reporter, sent } = harness()
    for (const message of ["one", "two", "three", "four", "five"]) {
      reporter.event(progress(message))
    }

    await reporter.fail(
      "TradingAgents run",
      new GenerateError("gateway refused", "freetier", "opencode/x", 2),
    )

    expect(sent).toHaveLength(1)
    const text = sent[0].text
    expect(text).toContain("### TradingAgents run — failed")
    expect(text).toContain("**freetier**")
    expect(text).toContain("**Last progress**")
    expect(text).toContain("- three")
    expect(text).toContain("- four")
    expect(text).toContain("- five")
    expect(text).not.toContain("- one")
    expect(text).not.toContain("- two")
    const bullets = text.split("\n").filter((line) => line.startsWith("- "))
    expect(bullets).toHaveLength(3)
    expect(sent[0].resume).toBe(false)
  })

  test("fail also carries a stage when the error has one", async () => {
    const { reporter, sent } = harness()
    await reporter.fail("TradingAgents run", new PipelineError("boom", "portfolio_manager"))

    expect(sent).toHaveLength(1)
    expect(sent[0].text).toContain("stage `portfolio_manager`")
  })

  test("a failing send never rejects finish, fail, or the start timer", async () => {
    const { reporter, fire } = harness({
      send: () => Promise.reject(new Error("ui down")),
    })

    await fire()
    await expect(reporter.finish("final")).resolves.toBeUndefined()

    const second = harness({ send: () => Promise.reject(new Error("ui down")) })
    await expect(
      second.reporter.fail("TradingAgents run", new Error("boom")),
    ).resolves.toBeUndefined()

    const sync = harness({
      send: () => {
        throw new Error("ui down")
      },
    })
    await expect(sync.reporter.finish("final")).resolves.toBeUndefined()
  })
})
