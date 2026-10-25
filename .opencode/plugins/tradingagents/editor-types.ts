/**
 * The editor types the registration callbacks receive.
 *
 * `@opencode/plugin` exposes `ToolEditor`/`CommandEditor` only from its internal
 * dist paths, which are not part of the package exports. Deriving them from the
 * context keeps the plugin on the public API and pinned to the installed
 * version.
 */

import type { Plugin } from "@opencode/plugin"

type TransformEditor<T> = T extends (callback: (input: infer Input) => void) => Promise<unknown>
  ? Input
  : never

export type ToolEditor = TransformEditor<Plugin.Context["tool"]["transform"]>
export type CommandEditor = TransformEditor<Plugin.Context["command"]["transform"]>
