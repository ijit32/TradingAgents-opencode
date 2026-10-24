"""The OpenCode plugin's Python side.

The plugin orchestrates the pipeline in TypeScript and calls OpenCode models; this
package is the worker it shells out to for everything that already exists in the
repo — point-in-time data collection, the agent prompts, report rendering, and
the memory log. It never calls an LLM.

One-shot subprocess per action, with the run directory as the interface: large
payloads are written there, and stdout carries a small JSON result.
"""
