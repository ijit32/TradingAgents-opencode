import { describe, expect, test } from "bun:test"
import {
  PROTOCOL_VERSION,
  asReplyFrame,
  decodeFrame,
  encodeFrame,
  requestFrame,
  versionMismatch,
} from "../protocol"

describe("encodeFrame", () => {
  test("is one newline-terminated line", () => {
    const line = encodeFrame(requestFrame(1, "plan", { run_dir: "/tmp/run" }))
    expect(line.endsWith("\n")).toBe(true)
    expect(line.trimEnd().includes("\n")).toBe(false)
    expect(JSON.parse(line)).toEqual({
      protocol_version: PROTOCOL_VERSION,
      id: 1,
      action: "plan",
      run_dir: "/tmp/run",
    })
  })

  test("omits optional fields that are absent", () => {
    const frame = requestFrame(3, "ping")
    expect(Object.keys(frame).sort()).toEqual(["action", "id", "protocol_version"])
  })
})

describe("decodeFrame", () => {
  test("accepts a v1 reply", () => {
    const reply = decodeFrame('{"protocol_version":1,"id":2,"ok":true,"action":"plan"}')
    expect(reply?.id).toBe(2)
    expect(reply?.ok).toBe(true)
  })

  test("keeps a null id for unparsable requests", () => {
    expect(decodeFrame('{"protocol_version":1,"id":null,"ok":false}')?.id).toBeNull()
  })

  test("ignores log lines, broken JSON, and other versions", () => {
    expect(decodeFrame("worker started")).toBeNull()
    expect(decodeFrame("{broken")).toBeNull()
    expect(decodeFrame('{"protocol_version":2,"id":1,"ok":true}')).toBeNull()
    expect(decodeFrame('{"protocol_version":1,"id":1}')).toBeNull()
    expect(decodeFrame('{"protocol_version":1,"id":"1","ok":true}')).toBeNull()
  })
})

describe("asReplyFrame", () => {
  test("rejects arrays and scalars", () => {
    expect(asReplyFrame([])).toBeNull()
    expect(asReplyFrame(null)).toBeNull()
    expect(asReplyFrame("ok")).toBeNull()
  })
})

describe("versionMismatch", () => {
  test("is silent for the current version", () => {
    expect(versionMismatch({ protocol_version: 1 })).toBeNull()
    expect(versionMismatch({})).toBeNull()
    expect(versionMismatch(null)).toBeNull()
  })

  test("names both versions", () => {
    const message = versionMismatch({ protocol_version: 2 })
    expect(message).toContain("2")
    expect(message).toContain(String(PROTOCOL_VERSION))
    expect(message).toContain("update both")
  })
})
