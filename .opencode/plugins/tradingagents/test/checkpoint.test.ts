import { describe, expect, test } from "bun:test"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  createCheckpoint,
  hashJson,
  invalidateFrom,
  markStage,
  newRunId,
  readCheckpoint,
  shouldSkip,
  uniqueRunDir,
  writeCheckpoint,
} from "../checkpoint"
import type { StageRecord } from "../types"

function record(hash: string, status: StageRecord["status"] = "ok"): StageRecord {
  return { status, hash, attempts: 1, model: "opencode/test", out: "stages/x.md", ms: 10 }
}

describe("hashJson", () => {
  test("ignores key order", () => {
    expect(hashJson({ a: 1, b: [2, 3] })).toBe(hashJson({ b: [2, 3], a: 1 }))
  })

  test("changes with a value", () => {
    expect(hashJson({ a: 1 })).not.toBe(hashJson({ a: 2 }))
  })
})

describe("shouldSkip", () => {
  test("skips a successful stage with the same hash", () => {
    const checkpoint = markStage(createCheckpoint("run", "req"), "market_analyst", record("h1"))
    expect(shouldSkip(checkpoint, "market_analyst", "h1")).toBe(true)
  })

  test("skips a stage the last resume skipped", () => {
    const checkpoint = markStage(
      createCheckpoint("run", "req"),
      "market_analyst",
      record("h1", "skipped"),
    )
    expect(shouldSkip(checkpoint, "market_analyst", "h1")).toBe(true)
  })

  test("reruns when the prompt changed or the stage failed", () => {
    const checkpoint = markStage(createCheckpoint("run", "req"), "trader", record("h2"))
    expect(shouldSkip(checkpoint, "trader", "h3")).toBe(false)
    const failed = markStage(createCheckpoint("run", "req"), "trader", record("h2", "failed"))
    expect(shouldSkip(failed, "trader", "h2")).toBe(false)
  })

  test("treats a missing checkpoint as empty", () => {
    expect(shouldSkip(null, "trader", "h1")).toBe(false)
  })
})

describe("invalidateFrom", () => {
  test("drops the tail and keeps earlier stages", () => {
    let checkpoint = createCheckpoint("run", "req")
    checkpoint = markStage(checkpoint, "a", record("h"))
    checkpoint = markStage(checkpoint, "b", record("h"))
    checkpoint = markStage(checkpoint, "c", record("h"))
    const next = invalidateFrom(checkpoint, ["a", "b", "c"], "b")
    expect(Object.keys(next.stages)).toEqual(["a"])
  })

  test("ignores an unknown stage", () => {
    const checkpoint = createCheckpoint("run", "req")
    expect(invalidateFrom(checkpoint, ["a"], "nope")).toBe(checkpoint)
  })
})

describe("run ids and directories", () => {
  test("uses the worker's UTC stamp", () => {
    expect(newRunId("NVDA", "2026-10-23", new Date("2026-10-24T11:30:00Z"))).toBe(
      "NVDA_2026-10-23_20261024_1130",
    )
  })

  test("never overwrites an existing run", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-runs-"))
    try {
      const first = uniqueRunDir(base, "NVDA_2026-10-23_20261024_1130")
      await writeFile(first, "")
      const second = uniqueRunDir(base, "NVDA_2026-10-23_20261024_1130")
      expect(second).not.toBe(first)
      expect(second.endsWith("_2")).toBe(true)
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })
})

describe("checkpoint file", () => {
  test("round-trips through an atomic write", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-checkpoint-"))
    try {
      const checkpoint = markStage(createCheckpoint("run", "req"), "trader", record("h"))
      await writeCheckpoint(base, checkpoint)
      expect(await readCheckpoint(base)).toEqual(checkpoint)
      expect(await readFile(join(base, "checkpoint.json"), "utf8")).toContain('"version": 1')
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })

  test("treats a truncated file as missing", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-checkpoint-"))
    try {
      await writeFile(join(base, "checkpoint.json"), "{")
      expect(await readCheckpoint(base)).toBeNull()
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })
})
