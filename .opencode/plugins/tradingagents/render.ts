/** Terminal rendering shared by tools and commands. */
function errorMarkdown(title: string, error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return `### ${title}\n\n**Failed** — ${message}`
}

export { errorMarkdown }
