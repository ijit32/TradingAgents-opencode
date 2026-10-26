import { describe, expect, test } from "bun:test"
import { formatDuration, renderSummary } from "../report"
import type { RunSummary } from "../types"

/** A stage fixture; `group` is the timeline bucket the card rolls up. */
interface StageFixture {
  readonly stage: string
  readonly status: string
  readonly model: string
  readonly ms: number
  readonly chars: number
  readonly group: string
}

function stage(stage: string, group: string, status = "ok", ms = 1_000): StageFixture {
  return { stage, status, model: "model/test", ms, chars: 100, group }
}

const STAGES: readonly StageFixture[] = [
  stage("market_analyst", "analysts", "ok", 120_000),
  stage("sentiment_analyst", "analysts"),
  stage("news_analyst", "analysts"),
  stage("fundamentals_analyst", "analysts"),
  stage("bull_researcher", "research_debate"),
  stage("bear_researcher", "research_debate"),
  stage("bull_researcher_round2", "research_debate"),
  stage("bear_researcher_round2", "research_debate"),
  stage("research_manager", "research_manager"),
  stage("trader", "trader"),
  stage("risky_analyst", "risk_debate"),
  stage("safe_analyst", "risk_debate"),
  stage("neutral_analyst", "risk_debate"),
  stage("portfolio_manager", "portfolio_manager"),
]

const RUN_DIR = "/runs/3f9c2a1"

const BASE: Omit<RunSummary, "stages"> & { stages: readonly StageFixture[] } = {
  runId: "3f9c2a1",
  runDir: RUN_DIR,
  ticker: "NVDA",
  tradeDate: "2026-10-23",
  depth: 2,
  stages: STAGES,
  warnings: ["first", "second"],
  calls: 62,
  elapsedMs: 374_000,
  rating: "Hold",
  reports: { market_report: "MARKET_BODY_MARKER" },
  reportMd: `${RUN_DIR}/reports/complete_report.md`,
  reportHtml: `${RUN_DIR}/reports/complete_report.html`,
  memoryLog: `${RUN_DIR}/memory/log.md`,
  stateLog: `${RUN_DIR}/results/state.json`,
}

function card(overrides: Partial<typeof BASE> = {}): string {
  return renderSummary({ ...BASE, ...overrides } as unknown as RunSummary)
}

describe("renderSummary", () => {
  test("renders the full run card", () => {
    const text = card()
    const lines = text.split("\n")

    expect(lines[0]).toBe("### NVDA · 2026-10-23 · Hold")
    expect(lines[2]).toBe(
      "Depth 2 · 14 stages · 62 calls · 6m 14s · run `3f9c2a1`",
    )
    expect(text).toContain("| Analysts | ✓✓✓✓ | market · sentiment · news · fundamentals |")
    expect(text).toContain("| Debate | ✓ | 2 rounds |")
    expect(text).toContain("| Manager | ✓ | Research manager |")
    expect(text).toContain("| Trader | ✓ | Trader |")
    expect(text).toContain("| Risk | ✓ | 1 round |")
    expect(text).toContain("| PM | ✓ | Hold |")
    expect(text).toContain("**Warnings (2)**")
    expect(text).toContain("- first")
    expect(text).toContain("- second")
    expect(text).toContain("**Artifacts**")
    expect(text).toContain(`- Directory \`${RUN_DIR}\``)
    expect(text).toContain(`- Report \`${RUN_DIR}/reports/complete_report.md\``)
    expect(text).toContain(`- HTML \`${RUN_DIR}/reports/complete_report.html\``)
    expect(text).toContain(`- Memory \`${RUN_DIR}/memory/log.md\``)
    expect(text).toContain(`- State \`${RUN_DIR}/results/state.json\``)
  })

  test("no rating, and a failed analyst turns its glyph and the roll-up red", () => {
    const stages = STAGES.map((entry) =>
      entry.stage === "market_analyst" ? { ...entry, status: "failed" } : entry,
    )
    const text = card({ rating: undefined, stages })
    const lines = text.split("\n")

    expect(lines[0]).toBe("### NVDA · 2026-10-23 · no rating")
    expect(text).toContain("| Analysts | ✗✓✓✓ | market · sentiment · news · fundamentals |")
    expect(text).toContain("| PM | ✓ | no rating |")
  })

  test("skipped stages render as ~", () => {
    const stages = STAGES.map((entry) => {
      if (entry.stage === "news_analyst") return { ...entry, status: "skipped" }
      if (entry.group === "research_debate") return { ...entry, status: "skipped" }
      return entry
    })
    const text = card({ stages })

    expect(text).toContain("| Analysts | ✓✓~✓ | market · sentiment · news · fundamentals |")
    expect(text).toContain("| Debate | ~ | 2 rounds |")
  })

  test("more than six warnings are summarized", () => {
    const warnings = ["w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9"]
    const text = card({ warnings })
    const section = text.slice(text.indexOf("**Warnings (9)**"), text.indexOf("**Artifacts**"))

    expect(text).toContain("**Warnings (9)**")
    expect(text).toContain(`_+3 more in \`${RUN_DIR}/checkpoint.json\`_`)
    expect(section).toContain("- w6")
    expect(section).not.toContain("- w7")
    expect(section.split("\n").filter((line) => line.startsWith("- ")).length).toBe(6)
  })

  test("formatDuration", () => {
    expect(formatDuration(14_200)).toBe("14.2s")
    expect(formatDuration(180_000)).toBe("3m")
    expect(formatDuration(374_000)).toBe("6m 14s")
    expect(formatDuration(3_720_000)).toBe("1h 2m")
  })

  test("no stage table, no char counts, no report body", () => {
    const text = card({ reports: { market_report: "REPORT_BODY_UNIQUE_MARKER" } })

    expect(text).not.toContain("| Stage |")
    expect(text).not.toContain("Chars")
    expect(text).not.toContain("REPORT_BODY_UNIQUE_MARKER")
  })

  test("artifacts block is omitted when no artifact paths are set", () => {
    const text = card({
      reportMd: undefined,
      reportHtml: undefined,
      memoryLog: undefined,
      stateLog: undefined,
    })

    expect(text).not.toContain("**Artifacts**")
    expect(text).not.toContain("- Directory")
  })

  test("slowest line appears only when the rule holds", () => {
    const stages = STAGES.map((entry) =>
      entry.stage === "market_analyst" ? { ...entry, ms: 1_000 } : { ...entry, ms: 100 },
    )

    expect(card({ stages, elapsedMs: 4_000 })).toContain(
      "_Slowest: Market Analyst 1.0s_",
    )
    expect(card({ stages, elapsedMs: 3_999 })).not.toContain("_Slowest:")
    expect(card({ stages, elapsedMs: 10_000 })).not.toContain("_Slowest:")
  })
})
