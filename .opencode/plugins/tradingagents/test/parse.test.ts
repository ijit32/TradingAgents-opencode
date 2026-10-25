import { describe, expect, test } from "bun:test"
import { parseAnalysisText, parseBacktestText, parseProbeText } from "../parse"

describe("parseAnalysisText", () => {
  test("reads the positional ticker and date", () => {
    expect(parseAnalysisText("NVDA 2026-10-23")).toEqual({
      ticker: "NVDA",
      trade_date: "2026-10-23",
    })
  })

  test("reads flags before or after the positionals", () => {
    const input = parseAnalysisText(
      "--depth 2 NVDA --model opencode/free --analysts market,news --no-memory",
    )
    expect(input.ticker).toBe("NVDA")
    expect(input.depth).toBe(2)
    expect(input.model).toBe("opencode/free")
    expect(input.analysts).toEqual(["market", "news"])
    expect(input.memory).toBe(false)
  })

  test("an explicit date wins over the positional one", () => {
    const input = parseAnalysisText("NVDA 2026-01-01 --date 2026-02-02")
    expect(input.trade_date).toBe("2026-02-02")
  })

  test("a value-less flag does not become a ticker", () => {
    const input = parseAnalysisText("--depth NVDA")
    expect(input.trade_date).toBeUndefined()
    expect(Number.isNaN(input.depth)).toBe(true)
  })
})

describe("parseBacktestText", () => {
  test("reads tickers, dates, and flags", () => {
    const input = parseBacktestText(
      "NVDA,AAPL 2026-01-02 2026-03-01 --every 7 --run-id sweep1 --no-html",
    )
    expect(input.tickers).toEqual(["NVDA", "AAPL"])
    expect(input.start).toBe("2026-01-02")
    expect(input.end).toBe("2026-03-01")
    expect(input.every_n_days).toBe(7)
    expect(input.run_id).toBe("sweep1")
    expect(input.html).toBe(false)
  })

  test("keeps a single ticker as a one-item list", () => {
    expect(parseBacktestText("NVDA --start 2026-01-02").tickers).toEqual(["NVDA"])
  })
})

describe("parseProbeText", () => {
  test("splits flags from the free-text prompt", () => {
    const { input, prompt } = parseProbeText("--model opencode/test --parallel 4 hello world")
    expect(input.model).toBe("opencode/test")
    expect(input.parallel).toBe(4)
    expect(prompt).toBe("hello world")
  })

  test("only text mode is opt-in", () => {
    expect(parseProbeText("--mode text ping").input.mode).toBe("text")
    expect(parseProbeText("--mode session ping").input.mode).toBe("session")
  })

  test("a bare prompt has no input flags", () => {
    const { input, prompt } = parseProbeText("just check reachability")
    expect(input).toEqual({})
    expect(prompt).toBe("just check reachability")
  })
})
