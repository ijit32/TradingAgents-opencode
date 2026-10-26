/**
 * Chat surface for the `/ta-*` commands.
 *
 * A run can produce dozens of progress events, but chat should not: the user
 * sees one delayed start line and one final card. Everything in between stays
 * in memory as a short diagnostic tail for the error card.
 */

import type { ProgressEvent } from "./runtime"
import { errorMarkdown } from "./render"

export interface SyntheticNotice {
  readonly sessionID: string
  readonly text: string
  /** Never wake the agent for a plugin notice. */
  readonly resume: false
}

export type TimerHandle = unknown

export interface ReporterOptions {
  readonly sessionID: string
  readonly header: string | (() => string)
  readonly send: (notice: SyntheticNotice) => Promise<unknown> | unknown
  /** Delay before the start line; default 4_000. */
  readonly delayMs?: number
  /** Progress lines kept for diagnostics; default 20. */
  readonly tailSize?: number
  readonly now?: () => Date
  readonly setTimer?: (run: () => void, ms: number) => TimerHandle
  readonly clearTimer?: (handle: TimerHandle) => void
}

const DEFAULT_DELAY_MS = 4_000
const DEFAULT_TAIL_SIZE = 20
const DEFAULT_TAIL = 3

const defaultSetTimer = (run: () => void, ms: number): TimerHandle => setTimeout(run, ms)

/** `TimerHandle` is opaque to callers, so unwrap it only here. */
const defaultClearTimer = (handle: TimerHandle): void =>
  clearTimeout(handle as ReturnType<typeof setTimeout>)

function clockTime(when: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0")
  return `${pad(when.getHours())}:${pad(when.getMinutes())}`
}

/**
 * One reporter per command run: delayed start line, then one final message.
 *
 * Both `finish` and `fail` are terminal. Whichever runs first sends exactly one
 * message; later calls and a late-firing timer are no-ops.
 */
export class CommandReporter {
  private readonly sessionID: string
  private readonly header: string | (() => string)
  private readonly send: (notice: SyntheticNotice) => Promise<unknown> | unknown
  private readonly tailSize: number
  private readonly now: () => Date
  private readonly clearTimer: (handle: TimerHandle) => void
  private readonly lines: string[] = []
  private timer: TimerHandle | undefined
  private started = false
  private finished = false

  constructor(options: ReporterOptions) {
    this.sessionID = options.sessionID
    this.header = options.header
    this.send = options.send
    this.tailSize = Math.max(0, options.tailSize ?? DEFAULT_TAIL_SIZE)
    this.now = options.now ?? (() => new Date())
    this.clearTimer = options.clearTimer ?? defaultClearTimer
    const setTimer = options.setTimer ?? defaultSetTimer
    this.timer = setTimer(() => this.start(), options.delayMs ?? DEFAULT_DELAY_MS)
  }

  /** Record one progress line; never sends. */
  event(event: ProgressEvent): void {
    this.lines.push(event.message)
    if (this.lines.length > this.tailSize) {
      this.lines.splice(0, this.lines.length - this.tailSize)
    }
  }

  /** Send the final text once, unless already finished. */
  async finish(text: string): Promise<void> {
    if (this.finished) return
    this.finished = true
    this.stopTimer()
    await this.emit({ sessionID: this.sessionID, text, resume: false })
  }

  /** Send one error card with the tail, unless already finished. */
  async fail(title: string, error: unknown, hint?: string): Promise<void> {
    await this.finish(errorMarkdown(title, error, { hint, tail: this.tail(DEFAULT_TAIL) }))
  }

  /** Last `count` progress lines (default 3). */
  tail(count = DEFAULT_TAIL): readonly string[] {
    if (count <= 0) return []
    return this.lines.slice(Math.max(0, this.lines.length - count))
  }

  private start(): void {
    this.timer = undefined
    if (this.finished || this.started) return
    this.started = true
    const header = typeof this.header === "function" ? this.header() : this.header
    void this.emit({
      sessionID: this.sessionID,
      text: `${header} · started ${clockTime(this.now())}`,
      resume: false,
    })
  }

  private stopTimer(): void {
    if (this.timer === undefined) return
    const handle = this.timer
    this.timer = undefined
    this.clearTimer(handle)
  }

  /** A UI failure must not fail a run. */
  private async emit(notice: SyntheticNotice): Promise<void> {
    try {
      await this.send(notice)
    } catch {
      // Swallowed on purpose: chat is a side channel.
    }
  }
}
