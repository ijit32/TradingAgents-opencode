"""``python -m tradingagents.opencode_worker`` — the plugin's worker entry point."""

from tradingagents.opencode.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
