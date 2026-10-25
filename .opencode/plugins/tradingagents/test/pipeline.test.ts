import { describe, expect, test } from "bun:test"
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { confineRunDir, resolveRunId } from "../pipeline"
import { assertWorkerEnvironment, pythonCommand } from "../worker"

describe("pythonCommand", () => {
  test("runs uv with `run python`", () => {
    const command = pythonCommand("uv")
    expect(command.prefix).toEqual(["run", "python"])
  })

  test("calls an interpreter path directly", () => {
    const command = pythonCommand("/usr/bin/python3.13")
    expect(command.prefix).toEqual([])
  })
})

describe("assertWorkerEnvironment", () => {
  test("explains a missing project root", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-env-"))
    try {
      expect(() => assertWorkerEnvironment({ directory: join(base, "nope") })).toThrow(
        /project directory missing/,
      )
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })

  test("explains a directory that is not the project root", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-env-"))
    try {
      expect(() => assertWorkerEnvironment({ directory: base })).toThrow(/pyproject\.toml/)
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })

  test("accepts a project root", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-env-"))
    try {
      await writeFile(join(base, "pyproject.toml"), "")
      expect(() => assertWorkerEnvironment({ directory: base })).not.toThrow()
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })
})

describe("confineRunDir", () => {
  test("accepts a run under the root", () => {
    expect(confineRunDir("/runs", "/runs/NVDA_2026-10-23_20261024_1130")).toBe(
      "/runs/NVDA_2026-10-23_20261024_1130",
    )
  })

  test("accepts the root itself after resolution", () => {
    expect(confineRunDir("/runs", "/runs/../runs")).toBe("/runs")
  })

  test("rejects a sibling, an escape, and an absolute path outside", () => {
    expect(() => confineRunDir("/runs", "/runs-other/x")).toThrow(/must be inside/)
    expect(() => confineRunDir("/runs", "/runs/../etc")).toThrow(/must be inside/)
    expect(() => confineRunDir("/runs", "/etc")).toThrow(/must be inside/)
  })
})

describe("resolveRunId", () => {
  test("resolves an exact name and a unique prefix", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-ids-"))
    try {
      await mkdir(join(base, "NVDA_2026-10-23_20261024_1130"))
      await mkdir(join(base, "AAPL_2026-10-23_20261024_1130"))
      expect(await resolveRunId(base, "NVDA_2026-10-23_20261024_1130")).toBe(
        join(base, "NVDA_2026-10-23_20261024_1130"),
      )
      expect(await resolveRunId(base, "AAPL_2026")).toBe(
        join(base, "AAPL_2026-10-23_20261024_1130"),
      )
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })

  test("reports an ambiguous prefix", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-ids-"))
    try {
      await mkdir(join(base, "NVDA_2026-10-22_a"))
      await mkdir(join(base, "NVDA_2026-10-23_b"))
      await expect(resolveRunId(base, "NVDA")).rejects.toThrow(/matches 2 runs/)
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })

  test("reports a missing base and a missing run", async () => {
    const base = await mkdtemp(join(tmpdir(), "ta-ids-"))
    try {
      await expect(resolveRunId(base, "NOPE")).rejects.toThrow(/no run matches/)
      await expect(resolveRunId(join(base, "gone"), "NOPE")).rejects.toThrow(/no runs directory/)
    } finally {
      await rm(base, { recursive: true, force: true })
    }
  })
})
