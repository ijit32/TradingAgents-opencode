import { describe, expect, test } from "bun:test"
import { formatModelRef, parseModelRef, readConfig } from "../config"

describe("parseModelRef", () => {
  test("splits on the first slash only", () => {
    expect(parseModelRef("opencode/deepseek-v4.1-flash-free")).toEqual({
      providerID: "opencode",
      id: "deepseek-v4.1-flash-free",
    })
  })

  test("keeps a variant", () => {
    expect(parseModelRef("opencode/qwen3.8-flash#thinking")).toEqual({
      providerID: "opencode",
      id: "qwen3.8-flash",
      variant: "thinking",
    })
  })

  test("rejects a reference without a provider", () => {
    expect(() => parseModelRef("deepseek-v4.1-flash")).toThrow(/provider\/model/)
    expect(() => parseModelRef("")).toThrow(/empty/)
  })

  test("round-trips", () => {
    const spec = "opencode/kimi-k2.6#fast"
    expect(formatModelRef(parseModelRef(spec))).toBe(spec)
  })
})

describe("readConfig", () => {
  test("defaults to the free model on both tiers", () => {
    const config = readConfig({})
    expect(formatModelRef(config.quickModel)).toBe("opencode/deepseek-v4.1-flash-free")
    expect(formatModelRef(config.deepModel)).toBe("opencode/deepseek-v4.1-flash-free")
    expect(config.memory).toBe(true)
    expect(config.html).toBe(true)
    expect(config.python).toBe("uv")
  })

  test("clamps numbers instead of failing", () => {
    const config = readConfig({ concurrency: 99, callTimeoutMs: 1, maxRetries: -3 })
    expect(config.concurrency).toBe(8)
    expect(config.callTimeoutMs).toBe(5_000)
    expect(config.maxRetries).toBe(0)
  })

  test("reads stage and fallback models", () => {
    const config = readConfig({
      stageModels: { research_manager: "opencode/glm-5.3-flash-free" },
      fallbackModels: ["opencode/qwen3.8-flash"],
    })
    expect(formatModelRef(config.stageModels.research_manager)).toBe(
      "opencode/glm-5.3-flash-free",
    )
    expect(config.fallbackModels.map(formatModelRef)).toEqual(["opencode/qwen3.8-flash"])
  })

  test("ignores malformed option values", () => {
    const config = readConfig({ memory: "nope", concurrency: "many" })
    expect(config.memory).toBe(true)
    expect(config.concurrency).toBe(4)
  })
})
