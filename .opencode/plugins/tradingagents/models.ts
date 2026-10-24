/**
 * Model discovery for the plugin.
 *
 * `ctx.generate.text` accepts any model the active registry knows. Checking
 * first turns a mid-run provider error into a message that names the models the
 * user can actually pick.
 */

import type { Plugin } from "@opencode/plugin"
import type { ModelRef } from "./config"
import { formatModelRef } from "./config"

export type ModelContext = Pick<Plugin.Context, "model">

export interface AvailableModel {
  readonly providerID: string
  readonly id: string
  readonly name: string
  readonly enabled: boolean
  readonly contextLimit?: number
  readonly free: boolean
}

/** Normalize the client's `{ location, data }` page into a flat list. */
function rows(output: unknown): unknown[] {
  if (Array.isArray(output)) return output
  const data = (output as { data?: unknown } | undefined)?.data
  return Array.isArray(data) ? data : []
}

function isFree(row: Record<string, unknown>): boolean {
  const id = String(row.modelID ?? row.id ?? "")
  if (id.includes("free")) return true
  const costs = row.cost
  if (!Array.isArray(costs) || costs.length === 0) return false
  return costs.every((tier) => {
    if (!tier || typeof tier !== "object") return true
    const entry = tier as { input?: number; output?: number }
    return (entry.input ?? 0) === 0 && (entry.output ?? 0) === 0
  })
}

export async function listModels(ctx: ModelContext): Promise<AvailableModel[]> {
  const output = await ctx.model.list()
  return rows(output).flatMap((row): AvailableModel[] => {
    if (!row || typeof row !== "object") return []
    const record = row as Record<string, unknown>
    const providerID = record.providerID
    const modelID = record.modelID ?? record.id
    if (typeof providerID !== "string" || typeof modelID !== "string") return []
    const limit = record.limit as { context?: number } | undefined
    return [
      {
        providerID,
        id: modelID,
        name: typeof record.name === "string" ? record.name : modelID,
        enabled: record.enabled !== false,
        contextLimit: typeof limit?.context === "number" ? limit.context : undefined,
        free: isFree(record),
      },
    ]
  })
}

/**
 * Confirm the model exists and is enabled; return it unchanged.
 *
 * Throws with the provider's free models listed, because the free tier is the
 * default here and catalogs rotate.
 */
export async function resolveModel(ctx: ModelContext, ref: ModelRef): Promise<ModelRef> {
  const models = await listModels(ctx)
  const match = models.find(
    (model) => model.providerID === ref.providerID && model.id === ref.id,
  )
  if (match && match.enabled) return ref

  const reason = match ? "is disabled" : "is not in the active model list"
  const freeHere = models
    .filter((model) => model.providerID === ref.providerID && model.free && model.enabled)
    .map((model) => model.id)
    .slice(0, 10)
  const hint = freeHere.length
    ? ` free models on '${ref.providerID}': ${freeHere.join(", ")}`
    : ` no free models on '${ref.providerID}'`
  const known = models
    .filter((model) => model.providerID === ref.providerID && model.enabled)
    .map((model) => model.id)
    .slice(0, 20)
  const suffix = known.length ? `; enabled there: ${known.join(", ")}` : ""
  throw new Error(`model '${formatModelRef(ref)}' ${reason}.${hint}${suffix}`)
}

/** Resolve every candidate, dropping unknown ones instead of failing the run. */
export async function resolveCandidates(
  ctx: ModelContext,
  refs: readonly ModelRef[],
): Promise<ModelRef[]> {
  const models = await listModels(ctx)
  const enabled = new Set(
    models.filter((model) => model.enabled).map((model) => `${model.providerID}/${model.id}`),
  )
  return refs.filter((ref) => enabled.has(`${ref.providerID}/${ref.id}`))
}
