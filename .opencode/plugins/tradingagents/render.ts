/** Terminal rendering shared by tools and commands. */

import { GenerateError } from "./generate"
import { PipelineError } from "./runtime"

export interface ErrorMarkdownOptions {
  readonly stage?: string
  readonly hint?: string
  readonly tail?: readonly string[]
  readonly runDir?: string
  readonly maxTail?: number
}

const MESSAGE_LIMIT = 600
const DEFAULT_TAIL = 3
const DEFAULT_HINT = "Rerun the same command; completed stages are checkpointed."
const DEFAULT_CANCEL_HINT = "The run stopped; resume it with the same command."
const CANCEL_PATTERN = /cancelled|canceled|aborted/i

function isCancelled(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === "AbortError") return true
  if (error instanceof PipelineError && error.message.trim() === "run was cancelled") return true
  return CANCEL_PATTERN.test(error.message)
}

function messageOf(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error)
  const message = raw.trim()
  if (message.length <= MESSAGE_LIMIT) return message
  return `${message.slice(0, MESSAGE_LIMIT - 1).trimEnd()}…`
}

function tailOf(tail: readonly string[] | undefined, maxTail: number): string[] {
  if (!tail || maxTail <= 0) return []
  return tail
    .map((line) => line.replace(/\s*\r?\n\s*/g, " ").trim())
    .filter((line) => line.length > 0)
    .slice(-maxTail)
}

function badgeOf(error: unknown, cancelled: boolean): string {
  const parts: string[] = []

  if (cancelled) parts.push("**cancelled**")
  else if (error instanceof GenerateError) parts.push(`**${error.kind}**`)
  else parts.push("**failed**")

  if (error instanceof GenerateError) {
    parts.push(`\`${error.model}\``)
    if (error.attempts > 0) parts.push(`${error.attempts} attempt(s)`)
  }

  return parts.join(" · ")
}

export function errorMarkdown(
  title: string,
  error: unknown,
  options: ErrorMarkdownOptions = {},
): string {
  const cancelled = isCancelled(error)
  const stage =
    options.stage ?? (error instanceof PipelineError ? error.stage : undefined)
  const message = messageOf(error)
  const tail = tailOf(options.tail, options.maxTail ?? DEFAULT_TAIL)
  const hint =
    options.hint ?? (cancelled ? DEFAULT_CANCEL_HINT : DEFAULT_HINT)
  const runDir = options.runDir?.trim()

  const blocks: string[] = [
    `### ${title} — ${cancelled ? "cancelled" : "failed"}`,
    badgeOf(error, cancelled),
  ]

  if (stage) blocks.push(`stage \`${stage}\``)
  if (message) blocks.push(message)
  if (tail.length > 0) blocks.push(["**Last progress**", ...tail.map((line) => `- ${line}`)].join("\n"))
  if (hint.trim()) blocks.push(["**Next**", hint.trim()].join("\n"))
  if (runDir) blocks.push(`**Run** \`${runDir}\``)

  return blocks.join("\n\n")
}
