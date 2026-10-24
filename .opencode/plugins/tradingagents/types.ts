/**
 * Shared types for the pipeline, the checkpoint, and the worker's JSON replies.
 */

export type StageStatus = "ok" | "failed" | "skipped"

export interface StageRecord {
  readonly status: StageStatus
  readonly hash: string
  readonly attempts: number
  readonly model: string
  readonly out: string
  readonly ms: number
  readonly warnings?: readonly string[]
  readonly error?: string
}

export interface Checkpoint {
  readonly version: 1
  readonly run_id: string
  readonly request_hash: string
  readonly stages: Readonly<Record<string, StageRecord>>
  readonly warnings: readonly string[]
}

export interface AnalyzeInput {
  readonly ticker?: string
  readonly trade_date?: string
  readonly asset_type?: "stock" | "crypto"
  readonly depth?: number
  readonly analysts?: readonly string[]
  /** Model override for every stage of this run. */
  readonly model?: string
  readonly quick_model?: string
  readonly deep_model?: string
  readonly output_language?: string
  /** Free-text portfolio context, or an object rendered into one. */
  readonly portfolio?: string | Record<string, unknown>
  /** Settle past decisions and record this decision. Defaults to plugin config. */
  readonly memory?: boolean
  /** Write the HTML report. Defaults to plugin config. */
  readonly html?: boolean
  /** Results directory for this run; a backtest sweep isolates it. */
  readonly results_dir?: string
  /** Memory log this run settles and appends to; a sweep isolates it. */
  readonly memory_log_path?: string
  /** Rerun a stage even when its prompt hash is unchanged. */
  readonly rerun?: string
  /** Rerun this stage and everything downstream of it. */
  readonly rerun_from?: string
  /** Resume an existing run directory instead of starting one. */
  readonly run_dir?: string
  /** Parent directory for new run directories; a sweep isolates its cells. */
  readonly run_root?: string
  /** Resume a previous run by id (exact name or unique prefix). */
  readonly resume_run?: string
}

/** What `python -m tradingagents.opencode_worker` prints on stdout. */
export interface WorkerReply {
  readonly ok: boolean
  readonly action?: string
  readonly error?: string
  readonly errors?: readonly string[]
  readonly [key: string]: unknown
}

export interface CollectReply extends WorkerReply {
  readonly chars: number
  readonly truncated: boolean
  readonly sources: readonly string[]
  readonly notes: readonly string[]
  readonly hash: string
}

export interface PromptReply extends WorkerReply {
  readonly stage: string
  readonly prompt_path: string
  readonly hash: string
  readonly model_tier: "quick" | "deep"
  readonly kind?: StageKind
  /** Present when the prompt asks for JSON; the pipeline then allows two repairs. */
  readonly schema?: unknown
  readonly analyst?: string
}

/** What a stage produces: analyst prose, one debate turn, or a structured decision. */
export type StageKind = "report" | "argument" | "structured"

/** One stage from the worker's `plan` action. */
export interface PlanStage {
  readonly stage: string
  readonly tier: "quick" | "deep"
  readonly kind: StageKind
  readonly group: string
  readonly builder: string
  readonly report_key?: string
  readonly analyst?: string
  readonly debate?: string
  readonly speaker?: string
  readonly index?: number
}

export interface PlanGroup {
  readonly name: string
  readonly parallel: boolean
  readonly stages: readonly PlanStage[]
}

export interface PlanReply extends WorkerReply {
  readonly groups: readonly PlanGroup[]
  readonly order: readonly string[]
}

/** One due memory entry with its rendered reflection prompt. */
export interface MemoryEntry {
  readonly ticker: string
  readonly date: string
  readonly rating: string
  readonly raw_return: number
  readonly alpha_return: number
  readonly holding_days: number
  readonly system: string
  readonly user: string
}

export interface MemoryFailure {
  readonly ticker: string
  readonly date: string
  readonly reason: string
}

export interface MemoryBeginReply extends WorkerReply {
  readonly pending?: number
  readonly due?: number
  readonly entries?: readonly MemoryEntry[]
  readonly not_ready?: readonly { ticker: string; date: string }[]
  readonly failed?: readonly MemoryFailure[]
  readonly log_path?: string
}

export interface MemoryApplyReply extends WorkerReply {
  readonly settled?: readonly { ticker: string; date: string }[]
  readonly missing?: readonly { ticker: string; date: string }[]
  readonly past_context_chars?: number
  readonly note?: string
  readonly log_path?: string
}

/** What `settle` wrote: the report tree, the state log, and the memory entry. */
export interface SettleReply extends WorkerReply {
  readonly rating?: string
  readonly report_md?: string
  readonly report_html?: string
  readonly state_log?: string
  readonly notes?: string
  readonly memory_log?: string
  readonly decision_recorded?: boolean
}

export interface ValidateReply extends WorkerReply {
  readonly path: string
  readonly chars: number
  readonly lenient: boolean
  readonly warnings: readonly string[]
  readonly kind?: StageKind
  /** The typed Research Manager / Portfolio Manager rating, when parsed. */
  readonly rating?: string
  /** The Trader's action, when parsed. */
  readonly action?: string
}

export interface InitReply extends WorkerReply {
  readonly run_dir: string
  readonly run_id: string
  readonly instrument_context: string
}

export interface RunSummary {
  readonly runId: string
  readonly runDir: string
  readonly ticker: string
  readonly tradeDate: string
  readonly depth: number
  readonly stages: readonly { stage: string; status: string; model: string; ms: number; chars: number }[]
  readonly warnings: readonly string[]
  readonly calls: number
  readonly elapsedMs: number
  /** The Portfolio Manager's rating, when the decision parsed or was readable. */
  readonly rating?: string
  /** Validated reports, keyed by `final_state.json` name (`market_report`, …). */
  readonly reports: Readonly<Record<string, string>>
  /** `complete_report.md`, when the settle step wrote it. */
  readonly reportMd?: string
  /** `complete_report.html`, when the settle step wrote it. */
  readonly reportHtml?: string
  /** The graph-shaped state log, under the results directory. */
  readonly stateLog?: string
  /** The memory log this run appended to. */
  readonly memoryLog?: string
  /** Memory settlement note; also written into the report header. */
  readonly memoryNote?: string
}

/** One sweep: tickers over a date grid, with its own memory log as the results table. */
export interface BacktestInput {
  readonly ticker?: string
  readonly tickers?: readonly string[]
  readonly start?: string
  readonly end?: string
  readonly every_n_days?: number
  /** Sweep id, also its directory name. Generated when omitted. */
  readonly run_id?: string
  readonly depth?: number
  readonly analysts?: readonly string[]
  readonly model?: string
  readonly quick_model?: string
  readonly deep_model?: string
  readonly output_language?: string
  readonly html?: boolean
}

export interface BacktestCell {
  readonly ticker: string
  readonly date: string
}

export interface BacktestPlanReply extends WorkerReply {
  readonly run_id?: string
  readonly sweep_dir?: string
  readonly log_path?: string
  readonly dates?: readonly string[]
  readonly cells?: readonly BacktestCell[]
  readonly todo?: readonly BacktestCell[]
  readonly skipped?: number
  readonly total?: number
}

export interface BacktestRatingRow {
  readonly count: number
  readonly hit_rate: number
  readonly mean_alpha: number
}

export interface BacktestSummaryReply extends WorkerReply {
  readonly resolved?: number
  readonly pending?: number
  readonly unscored?: number
  readonly holding?: number
  readonly by_rating?: Readonly<Record<string, BacktestRatingRow>>
  readonly markdown?: string
  readonly sweep_dir?: string
  readonly log_path?: string
}

/** What one cell did. A failed cell does not stop the sweep. */
export interface BacktestCellResult extends BacktestCell {
  readonly status: "ok" | "failed"
  readonly rating?: string
  readonly calls?: number
  readonly runDir?: string
  readonly error?: string
}

export interface BacktestResult {
  readonly runId: string
  readonly sweepDir: string
  readonly logPath: string
  readonly total: number
  readonly skipped: number
  readonly cells: readonly BacktestCellResult[]
  readonly calls: number
  readonly elapsedMs: number
  readonly markdown: string
  readonly warnings: readonly string[]
}
