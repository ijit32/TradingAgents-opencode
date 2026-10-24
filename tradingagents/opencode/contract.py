"""The request contract between the plugin and the worker.

The plugin writes ``request.json`` once, when a run starts; every later action
reads the normalized form. Validation is strict and happens before any network
call, so a typo in a ticker or date fails in milliseconds instead of after a
vendor round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import safe_ticker_component

# Exit codes the plugin branches on. Keep in sync with
# .opencode/plugins/tradingagents/worker.ts.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_VALIDATION = 2
EXIT_BAD_INPUT = 3
EXIT_VENDOR = 4

# The analysts in report order; "social" is the key the graph uses for sentiment.
ANALYST_KEYS = ("market", "social", "news", "fundamentals")
ASSET_TYPES = ("stock", "crypto")

# depth -> (research debate rounds, risk debate rounds), matching what the CLI
# asks for at each research-depth setting.
DEPTH_PLANS = {
    1: (1, 1),
    2: (2, 1),
    3: (2, 2),
}


class WorkerError(Exception):
    """A failure the plugin should surface with a specific exit code."""

    exit_code = EXIT_INTERNAL

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": str(self)}


class BadInput(WorkerError):
    """The request is malformed or names something the worker cannot use."""

    exit_code = EXIT_BAD_INPUT


class VendorUnavailable(WorkerError):
    """A data vendor could not answer; the pack is incomplete."""

    exit_code = EXIT_VENDOR


class ValidationFailed(WorkerError):
    """A stage's output did not meet its contract; the plugin may retry."""

    exit_code = EXIT_VALIDATION

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "validation failed")

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": str(self), "errors": self.errors}


def _canonical_date(value: Any) -> str:
    """``YYYY-MM-DD``, no later than today, or ``BadInput``.

    Same rule as ``TradingAgentsGraph._validate_trade_date``; duplicated here so
    the worker can validate without importing the graph (and its LLM clients).
    """
    text = str(value).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise BadInput(f"trade_date must be YYYY-MM-DD, got {value!r}")
    try:
        from datetime import datetime

        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise BadInput(f"trade_date must be YYYY-MM-DD, got {value!r}") from exc
    if text > get_current_date():
        raise BadInput(f"trade_date cannot be in the future: {text}")
    return text


def _analysts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ANALYST_KEYS
    if not isinstance(value, (list, tuple)):
        raise BadInput("analysts must be a list")
    wanted = {str(item).strip().lower() for item in value}
    unknown = wanted - set(ANALYST_KEYS)
    if unknown:
        raise BadInput(f"unknown analyst(s): {', '.join(sorted(unknown))}")
    if not wanted:
        raise BadInput("analysts cannot be empty")
    return tuple(key for key in ANALYST_KEYS if key in wanted)


def _absolute_path(value: Any, name: str) -> str:
    """An absolute path, or ``""`` for "use the configured default".

    Paths reach the report writer, the memory log, and the state log, so they
    are normalized once here: a relative path would silently depend on the
    worker's working directory.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "\x00" in text:
        raise BadInput(f"{name} contains a null byte")
    expanded = Path(text).expanduser()
    if not expanded.is_absolute():
        raise BadInput(f"{name} must be an absolute path, got {text!r}")
    return str(expanded)


def _depth(value: Any) -> int:
    try:
        depth = int(value)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"depth must be 1, 2, or 3, got {value!r}") from exc
    if depth not in DEPTH_PLANS:
        raise BadInput(f"depth must be 1, 2, or 3, got {value!r}")
    return depth


@dataclass(frozen=True)
class AnalysisRequest:
    """One analysis run, normalized."""

    ticker: str
    trade_date: str
    asset_type: str = "stock"
    depth: int = 1
    analysts: tuple[str, ...] = ANALYST_KEYS
    output_language: str = "English"
    portfolio: str = ""
    models: dict[str, str] = field(default_factory=dict)
    # Where this run writes its reports and state log, and the memory log it
    # settles and appends to. Empty means the repo defaults; a backtest sweep
    # points both at its own isolated directory.
    results_dir: str = ""
    memory_log_path: str = ""

    @property
    def max_debate_rounds(self) -> int:
        return DEPTH_PLANS[self.depth][0]

    @property
    def max_risk_discuss_rounds(self) -> int:
        return DEPTH_PLANS[self.depth][1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "trade_date": self.trade_date,
            "asset_type": self.asset_type,
            "depth": self.depth,
            "analysts": list(self.analysts),
            "output_language": self.output_language,
            "portfolio": self.portfolio,
            "models": dict(self.models),
            "results_dir": self.results_dir,
            "memory_log_path": self.memory_log_path,
            "max_debate_rounds": self.max_debate_rounds,
            "max_risk_discuss_rounds": self.max_risk_discuss_rounds,
        }

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AnalysisRequest:
        if not isinstance(raw, dict):
            raise BadInput("request must be a JSON object")
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            raise BadInput("ticker is required")
        # Validates that the ticker is safe to use as a path component too.
        try:
            ticker = safe_ticker_component(ticker.strip())
        except ValueError as exc:
            raise BadInput(str(exc)) from exc

        trade_date = _canonical_date(raw.get("trade_date"))

        asset_type = str(raw.get("asset_type") or "stock").strip().lower()
        if asset_type not in ASSET_TYPES:
            raise BadInput(f"asset_type must be one of {', '.join(ASSET_TYPES)}, got {asset_type!r}")

        language = raw.get("output_language")
        language = "English" if language is None else str(language).strip()
        if not language or len(language) > 40:
            raise BadInput("output_language must be a short non-empty string")

        portfolio = raw.get("portfolio") or ""
        if not isinstance(portfolio, str):
            raise BadInput("portfolio must be a string")
        if len(portfolio) > 8000:
            raise BadInput("portfolio is too long (8000 chars max)")

        models = raw.get("models") or {}
        if not isinstance(models, dict):
            raise BadInput("models must be an object of stage -> model reference")
        clean_models = {
            str(key): str(value) for key, value in models.items() if isinstance(value, str) and value.strip()
        }

        return cls(
            ticker=ticker,
            trade_date=trade_date,
            asset_type=asset_type,
            depth=_depth(raw.get("depth", 1)),
            analysts=_analysts(raw.get("analysts")),
            output_language=language,
            portfolio=portfolio.strip(),
            models=clean_models,
            results_dir=_absolute_path(raw.get("results_dir"), "results_dir"),
            memory_log_path=_absolute_path(raw.get("memory_log_path"), "memory_log_path"),
        )
