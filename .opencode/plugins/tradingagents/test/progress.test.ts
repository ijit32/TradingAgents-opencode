import { describe, expect, test } from "bun:test"
import type { PipelineHooks, ProgressEvent } from "../runtime"
import { formatProgress, progress } from "../runtime"

const event: ProgressEvent = {
  phase: "generate",
  stage: "market_analyst",
  title: "Market Analyst",
  group: "analysts",
  index: 1,
  total: 4,
  model: "opencode/deepseek-v4.1-flash-free",
  message: "generating market analyst with opencode/deepseek-v4.1-flash-free",
}

describe("formatProgress", () => {
  test("returns the message and leaves the event alone", () => {
    const before = { ...event }
    expect(formatProgress(event)).toBe(event.message)
    expect(event).toEqual(before)
  })
})

describe("progress", () => {
  test("passes the whole event to the hook", async () => {
    const seen: ProgressEvent[] = []
    const hooks: PipelineHooks = {
      progress: (item) => {
        seen.push(item)
      },
    }
    await progress(hooks, event)
    expect(seen).toEqual([event])
  })

  test("swallows a hook that throws", async () => {
    const hooks: PipelineHooks = {
      progress: () => {
        throw new Error("progress is broken")
      },
    }
    await expect(progress(hooks, event)).resolves.toBeUndefined()
  })

  test("swallows a hook that rejects", async () => {
    const hooks: PipelineHooks = {
      progress: async () => {
        throw new Error("progress is broken")
      },
    }
    await expect(progress(hooks, event)).resolves.toBeUndefined()
  })

  test("resolves when the hooks define no progress callback", async () => {
    await expect(progress({}, event)).resolves.toBeUndefined()
  })
})
