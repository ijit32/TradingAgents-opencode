/**
 * Public surface of the analysis pipeline.
 *
 * The implementation is split by concern:
 *
 *   runtime.ts      input normalization, run context, worker helpers
 *   stage.ts        prepare / generate / validate / repair one stage
 *   run.ts          the ordered run and its checkpoint
 *   run-dir.ts      where a run lives and how an id resolves
 *   payload.ts      request.json, merged with a resumed run's settings
 *   memory-pass.ts  settle past decisions and inject the lessons
 *   report.ts       final_state.json, summaries, report/status readers
 *
 * Import from this module when the caller wants "a run"; import a specific
 * module when it needs only that concern.
 */

export {
  ANALYST_KEYS,
  type PipelineHooks,
  PipelineError,
  type StageSpec,
  modelForRun,
} from "./runtime"
export { confineRunDir, resolveRunId, runRootOf } from "./run-dir"
export {
  type MemoryOutcome,
  type MemoryPassOptions,
  settleMemory,
} from "./memory-pass"
export { runAnalysis } from "./run"
export { type ReportView, describeRun, readReport, renderSummary } from "./report"
