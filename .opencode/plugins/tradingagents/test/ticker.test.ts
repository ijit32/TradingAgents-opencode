import { describe, expect, test } from "bun:test"
import { isSafeTicker } from "../runtime"

describe("isSafeTicker", () => {
  test("accepts NSE ampersand symbols", () => {
    for (const ticker of ["M&M.NS", "M&MFIN.NS"]) {
      expect(isSafeTicker(ticker)).toBe(true)
    }
  })

  test("accepts the existing symbol formats", () => {
    for (const ticker of ["RELIANCE.NS", "^NSEI", "GC=F", "XAUUSD+", "BTC-USD"]) {
      expect(isSafeTicker(ticker)).toBe(true)
    }
  })

  test("rejects traversal and separators", () => {
    for (const ticker of ["../x", "a/b", "a b", ""]) {
      expect(isSafeTicker(ticker)).toBe(false)
    }
  })

  test("rejects dot-only values", () => {
    for (const ticker of [".", "..", "..."]) {
      expect(isSafeTicker(ticker)).toBe(false)
    }
  })
})
