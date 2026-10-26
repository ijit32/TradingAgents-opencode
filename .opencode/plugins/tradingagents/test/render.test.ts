import { describe, expect, test } from "bun:test"
import { GenerateError } from "../generate"
import { errorMarkdown } from "../render"
import { PipelineError } from "../runtime"

const DEFAULT_HINT = "Rerun the same command; completed stages are checkpointed."
const CANCEL_HINT = "The run stopped; resume it with the same command."

describe("errorMarkdown", () => {
  test("renders a plain error with default hint", () => {
    const markdown = errorMarkdown("TradingAgents run", new Error("boom"))
    expect(markdown).toContain("### TradingAgents run — failed")
    expect(markdown).toContain("**failed**")
    expect(markdown).toContain("boom")
    expect(markdown).toContain(DEFAULT_HINT)
  })

  test("renders a GenerateError badge with model and attempts", () => {
    const markdown = errorMarkdown(
      "TradingAgents run",
      new GenerateError("gateway refused", "freetier", "opencode/x", 2),
    )
    expect(markdown).toContain("**freetier**")
    expect(markdown).toContain("`opencode/x`")
    expect(markdown).toContain("2 attempt(s)")
    expect(markdown).toContain("gateway refused")
  })

  test("keeps two-argument calls sane", () => {
    const markdown = errorMarkdown("TradingAgents status", new Error("nope"))
    expect(markdown).toContain("### TradingAgents status — failed")
    expect(markdown).toContain("**failed**")
    expect(markdown).toContain("nope")
    expect(markdown).not.toContain("**Last progress**")
    expect(markdown).not.toContain("**Run**")
  })

  test("classifies cancellation", () => {
    const abort = new Error("The operation was aborted")
    abort.name = "AbortError"
    const markdown = errorMarkdown("TradingAgents run", abort)
    expect(markdown).toContain("### TradingAgents run — cancelled")
    expect(markdown).toContain("**cancelled**")
    expect(markdown).toContain(CANCEL_HINT)
    expect(markdown).not.toContain(DEFAULT_HINT)
  })

  test("caps tail at three lines and collapses newlines", () => {
    const markdown = errorMarkdown("TradingAgents run", new Error("boom"), {
      tail: ["one", "two", "generating\nportfolio manager", "validating report"],
    })
    expect(markdown).not.toContain("- one")
    expect(markdown).toContain("- two")
    expect(markdown).toContain("- generating portfolio manager")
    expect(markdown).toContain("- validating report")
    const bullets = markdown.split("\n").filter((line) => line.startsWith("- "))
    expect(bullets).toHaveLength(3)
  })

  test("renders the stage from options", () => {
    const markdown = errorMarkdown("TradingAgents run", new Error("boom"), {
      stage: "portfolio_manager",
    })
    expect(markdown).toContain("stage `portfolio_manager`")
  })

  test("renders the run dir last and honors a hint override", () => {
    const markdown = errorMarkdown("TradingAgents run", new PipelineError("bad report", "risk"), {
      hint: "Check the report tree.",
      runDir: "~/.tradingagents/opencode/NVDA_2026",
    })
    expect(markdown).toContain("stage `risk`")
    expect(markdown).toContain("Check the report tree.")
    expect(markdown).not.toContain(DEFAULT_HINT)
    expect(markdown.endsWith("**Run** `~/.tradingagents/opencode/NVDA_2026`")).toBe(true)
  })

  test("caps a long message with an ellipsis", () => {
    const markdown = errorMarkdown("TradingAgents run", new Error("x".repeat(900)))
    expect(markdown).toContain("…")
    expect(markdown).not.toContain("x".repeat(700))
  })
})
