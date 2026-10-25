"""Worker entry points: one action per subprocess, JSON result on stdout.

The plugin owns orchestration and every model call; each action here is a plain
file-in, file-out step. Nothing in this module builds an LLM client.

    python -m tradingagents.opencode_worker <action> --run-dir <path> [options]

Exit codes (branched on by the plugin): 0 ok, 2 validation failed (retryable),
3 bad input, 4 vendor missing, 1 internal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tradingagents
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.opencode.backtest import action_backtest_plan, action_backtest_summary
from tradingagents.opencode.collect import COLLECTORS
from tradingagents.opencode.contract import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_VALIDATION,
    AnalysisRequest,
    BadInput,
    ValidationFailed,
    WorkerError,
)
from tradingagents.opencode.memory import (
    action_memory_apply,
    action_memory_begin,
    action_settle,
)
from tradingagents.opencode.run_dir import RunDir
from tradingagents.opencode.stages import StageDef, plan_payload, stage_defs

# Below this the report or argument is treated as a failed generation, not a
# terse one.
FREE_TEXT_MIN_CHARS = 200

# Prompt builders, keyed by the plan's ``builder`` field. Analyst builders take
# ``(state, data_pack=...)``; every other builder takes ``(state)`` and returns a
# string or a message list.
_BUILDERS = {
    "market_analyst": (
        "tradingagents.agents.analysts.market_analyst",
        "build_market_analyst_messages",
    ),
    "sentiment_analyst": (
        "tradingagents.agents.analysts.sentiment_analyst",
        "build_sentiment_analyst_messages",
    ),
    "news_analyst": (
        "tradingagents.agents.analysts.news_analyst",
        "build_news_analyst_messages",
    ),
    "fundamentals_analyst": (
        "tradingagents.agents.analysts.fundamentals_analyst",
        "build_fundamentals_analyst_messages",
    ),
    "bull_researcher": (
        "tradingagents.agents.researchers.bull_researcher",
        "build_bull_researcher_prompt",
    ),
    "bear_researcher": (
        "tradingagents.agents.researchers.bear_researcher",
        "build_bear_researcher_prompt",
    ),
    "research_manager": (
        "tradingagents.agents.managers.research_manager",
        "build_research_manager_prompt",
    ),
    "trader": (
        "tradingagents.agents.trader.trader",
        "build_trader_messages",
    ),
    "aggressive_analyst": (
        "tradingagents.agents.risk_mgmt.aggressive_debator",
        "build_aggressive_prompt",
    ),
    "conservative_analyst": (
        "tradingagents.agents.risk_mgmt.conservative_debator",
        "build_conservative_prompt",
    ),
    "neutral_analyst": (
        "tradingagents.agents.risk_mgmt.neutral_debator",
        "build_neutral_prompt",
    ),
    "portfolio_manager": (
        "tradingagents.agents.managers.portfolio_manager",
        "build_portfolio_manager_prompt",
    ),
}


def _result(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_json_args(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BadInput(f"--json-args is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BadInput("--json-args must contain a JSON object")
    return raw


# ---- actions ---------------------------------------------------------------


def action_init(run: RunDir, args) -> dict[str, Any]:
    """Normalize the request and write the run's immutable metadata."""
    raw = run.read_json("request.json")
    if raw is None:
        incoming = args.json_args.get("request")
        if incoming is None:
            raise BadInput("request.json is missing and --json-args carries no request")
        raw = incoming
    request = AnalysisRequest.from_mapping(raw)
    run.ensure()
    run.save_request(request)

    from tradingagents.agents.context import build_instrument_context

    instrument_context = build_instrument_context(
        request.ticker, request.asset_type, trade_date=request.trade_date
    )
    run.save_meta(
        {
            "run_id": run.run_id,
            "ticker": request.ticker,
            "trade_date": request.trade_date,
            "asset_type": request.asset_type,
            "instrument_context": instrument_context,
            "versions": {"tradingagents": tradingagents.__version__},
            "warnings": [],
        }
    )
    run.save_settings(_settings(request))
    return {
        "ok": True,
        "action": "init",
        "run_dir": str(run.root),
        "run_id": run.run_id,
        "request": request.to_dict(),
        "instrument_context": instrument_context,
    }


def _settings(request: AnalysisRequest) -> dict[str, Any]:
    """The report header's allowlist, in the shape ``run_settings()`` returns.

    Model tiers come from the plugin, which is the only place that knows them;
    endpoints and keys are never recorded.
    """
    quick = request.models.get("quick") or request.models.get("quick_think_llm") or "?"
    deep = request.models.get("deep") or request.models.get("deep_think_llm") or quick
    config = DEFAULT_CONFIG
    return {
        "version": tradingagents.__version__,
        "llm_provider": "opencode",
        "deep_think_provider": "opencode",
        "deep_think_llm": deep,
        "quick_think_provider": "opencode",
        "quick_think_llm": quick,
        "analysts": list(request.analysts),
        "max_debate_rounds": request.max_debate_rounds,
        "max_risk_discuss_rounds": request.max_risk_discuss_rounds,
        "output_language": request.output_language,
        "data_vendors": dict(config.get("data_vendors") or {}),
        "tool_vendors": dict(config.get("tool_vendors") or {}),
    }


def action_plan(run: RunDir, args) -> dict[str, Any]:
    """The ordered stages and their groups, so the plugin does not mirror the order."""
    return plan_payload(run.load_request())


def action_collect(run: RunDir, args) -> dict[str, Any]:
    """Fetch one analyst's data pack."""
    request = run.load_request()
    analyst = (args.analyst or "market").strip().lower()
    collector = COLLECTORS.get(analyst)
    if collector is None:
        raise BadInput(
            f"no collector for analyst {analyst!r}; known: {', '.join(sorted(COLLECTORS))}"
        )
    if analyst not in request.analysts:
        raise BadInput(f"the {analyst} analyst is not selected for this run")

    pack = collector(request)
    relative = f"data/{analyst}.md"
    target = run.write_text(pack.text, "data", f"{analyst}.md")
    index = run.read_json("data", "index.json", default={}) or {}
    if not isinstance(index, dict):
        index = {}
    index[analyst] = pack.index_entry(relative)
    index["updated_at"] = pack.as_of
    run.write_json(index, "data", "index.json")

    return {
        "ok": True,
        "action": "collect",
        "analyst": analyst,
        "path": str(target.relative_to(run.root)),
        "chars": pack.chars,
        "truncated": pack.truncated,
        "sources": pack.sources,
        "notes": pack.notes,
        "hash": pack.digest,
    }


def _render_prompt(messages) -> str:
    """One string, because the plugin's generation path takes a single prompt.

    Builders return a string (most agents), a message list, or a list of role
    dicts (the trader). All three become labelled blocks.
    """
    if isinstance(messages, str):
        return f"### user\n\n{messages.strip()}\n"
    blocks = []
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
        else:
            role = getattr(message, "type", "user")
            raw = getattr(message, "content", "")
            content = raw if isinstance(raw, str) else str(raw)
        blocks.append(f"### {role}\n\n{content.strip()}")
    return "\n\n".join(blocks) + "\n"


def _builder(builder_key: str):
    import importlib

    module_name, function_name = _BUILDERS[builder_key]
    return getattr(importlib.import_module(module_name), function_name)


def _base_state(run: RunDir, request: AnalysisRequest) -> dict[str, Any]:
    """The state the analysts' builders expect, with the run's identity context."""
    meta = run.read_json("meta.json", default={}) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "company_of_interest": request.ticker,
        "asset_type": request.asset_type,
        "trade_date": request.trade_date,
        "instrument_context": str(meta.get("instrument_context") or ""),
        "messages": [],
    }


def _stage_spec(request: AnalysisRequest, stage: str) -> StageDef:
    specs = stage_defs(request)
    spec = specs.get(stage)
    if spec is None:
        raise BadInput(f"unknown stage {stage!r}; known: {', '.join(specs)}")
    return spec


def _structured_model(stage: str):
    """The pydantic model and its markdown renderer for a structured stage."""
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        ResearchPlan,
        TraderProposal,
        render_pm_decision,
        render_research_plan,
        render_trader_proposal,
    )

    models = {
        "research_manager": (ResearchPlan, render_research_plan, "recommendation"),
        "trader": (TraderProposal, render_trader_proposal, "action"),
        "portfolio_manager": (PortfolioDecision, render_pm_decision, "rating"),
    }
    return models[stage]


def _schema_block(schema: dict[str, Any]) -> str:
    return (
        "\n\n## Structured output\n\n"
        "Return one JSON object matching this schema. Output only the JSON object: "
        "no prose, no explanation, no code fences.\n\n"
        "```json\n"
        + json.dumps(schema, indent=2, ensure_ascii=False)
        + "\n```\n"
    )


def _stage_prompt(
    run: RunDir, request: AnalysisRequest, spec: StageDef
) -> tuple[str, dict[str, Any]]:
    """Render a stage's prompt from the run's data, without network or a model."""
    from tradingagents.dataflows.config import run_config

    builder = _builder(spec.builder)
    detail: dict[str, Any] = {"kind": spec.kind, "data_chars": 0}

    with run_config({"output_language": request.output_language}):
        if spec.kind == "report":
            from tradingagents.opencode.stages import ANALYST_STAGES

            analyst = next(
                key for key, (stage, _, _) in ANALYST_STAGES.items() if stage == spec.stage
            )
            pack = run.read_text("data", f"{analyst}.md")
            if pack is None or not pack.strip():
                raise BadInput(f"data/{analyst}.md is missing; run collect first")
            messages = builder(_base_state(run, request), data_pack=pack)
            text = _render_prompt(messages)
            detail.update(
                {"messages": len(messages), "data_chars": len(pack), "analyst": analyst}
            )
        else:
            from tradingagents.opencode.state import load_state

            state = load_state(run, request, spec)
            built = builder(state)
            text = _render_prompt(built)
            detail["messages"] = len(built) if isinstance(built, list) else 1

    schema = None
    if spec.kind == "structured":
        model, _, _ = _structured_model(spec.stage)
        schema = model.model_json_schema()
        text += _schema_block(schema)
    detail["schema"] = schema
    return text, detail


def action_prompt(run: RunDir, args) -> dict[str, Any]:
    """Render a stage's prompt from the run's data; no network, no model."""
    request = run.load_request()
    spec = _stage_spec(request, args.stage)

    text, detail = _stage_prompt(run, request, spec)

    target = run.write_text(text, "stages", f"{spec.stage}.prompt.md")
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "action": "prompt",
        "stage": spec.stage,
        "prompt_path": str(target.relative_to(run.root)),
        "hash": digest,
        "chars": len(text),
        "model_tier": spec.tier,
        **detail,
    }


def _extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in the output, fenced or bare."""
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    decoder = json.JSONDecoder()
    for index, char in enumerate(body):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(body[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found in the output")


def _format_errors(error: Exception) -> list[str]:
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(part) for part in item['loc']) or 'output'}: {item['msg']}"
            for item in error.errors()[:10]
        ]
    return [str(error)]


def _review_marker() -> str:
    return (
        "> [review] This stage produced less than a full report; "
        "treat it as incomplete.\n\n"
    )


def _check_length(text: str, minimum: int) -> list[str]:
    if not text:
        return ["output is empty"]
    if len(text) < minimum:
        return [f"output is {len(text)} chars; at least {minimum} expected for a report"]
    return []


def _validate_structured(run: RunDir, spec: StageDef, text: str, lenient: bool) -> tuple:
    """Parse and validate JSON, or accept the prose with a review marker."""
    from tradingagents.agents.rating import parse_rating

    model, render, field = _structured_model(spec.stage)

    if not lenient:
        try:
            parsed = model.model_validate(_extract_json(text))
        except Exception as exc:  # noqa: BLE001 — every parse failure is retryable
            raise ValidationFailed(_format_errors(exc)) from None
        markdown = render(parsed)
        payload = parsed.model_dump(mode="json")
        run.write_json(payload, "stages", f"{spec.stage}.json")
        value = getattr(parsed, field)
        verdict = str(getattr(value, "value", value))
        detail = {"rating": verdict} if spec.stage != "trader" else {"action": verdict}
        return markdown, detail, []

    errors = _check_length(text, FREE_TEXT_MIN_CHARS)
    markdown = _review_marker() + (text or "(no output)")
    # A free-text decision has no typed field, so its rating is read from the
    # text; a decision nobody can read becomes REVIEW, never a Hold.
    detail: dict[str, Any] = {}
    if spec.stage in {"research_manager", "portfolio_manager"}:
        detail["rating"] = parse_rating(text)
    elif spec.stage == "trader":
        detail["action"] = ""
    return markdown, detail, errors


def action_validate(run: RunDir, args) -> dict[str, Any]:
    """Check a stage's raw output and promote it to the run's report file.

    Reports get a length floor; debate turns are prefixed with the speaker and
    recorded at their place in the transcript; structured decisions are parsed
    against their pydantic schema and rendered to markdown.
    """
    request = run.load_request()
    spec = _stage_spec(request, args.stage)
    raw = run.read_text("stages", f"{spec.stage}.out.txt")
    if raw is None:
        raise BadInput(f"stages/{spec.stage}.out.txt is missing; generate it first")
    text = raw.strip()

    detail: dict[str, Any] = {}
    if spec.kind == "structured":
        markdown, detail, errors = _validate_structured(run, spec, text, args.lenient)
    else:
        minimum = FREE_TEXT_MIN_CHARS
        errors = _check_length(text, minimum)
        if errors and not args.lenient:
            raise ValidationFailed(errors)
        body = text or "(no output)"
        if errors:
            body = _review_marker() + body
        if spec.kind == "argument":
            markdown = f"{spec.speaker}: {body}"
            from tradingagents.opencode.state import save_argument

            save_argument(run, spec, markdown)
        else:
            markdown = body

    target = run.write_text(markdown + "\n", "stages", f"{spec.stage}.md")
    warnings = errors if args.lenient else []
    return {
        "ok": True,
        "action": "validate",
        "stage": spec.stage,
        "kind": spec.kind,
        "path": str(target.relative_to(run.root)),
        "chars": len(markdown),
        "lenient": bool(errors and args.lenient),
        "warnings": warnings,
        **detail,
    }


def action_debates(run: RunDir, args) -> dict[str, Any]:
    """Both debate transcripts in ``final_state.json`` shape, for the settle step."""
    from tradingagents.opencode.state import debate_histories

    return {"ok": True, "action": "debates", **debate_histories(run)}


def action_status(run: RunDir, args) -> dict[str, Any]:
    """What the run has produced so far; safe to call at any point."""
    checkpoint = run.checkpoint()
    stages = {}
    for path in sorted((run.root / "stages").glob("*.md")):
        if path.name.endswith(".prompt.md"):
            continue
        stages[path.stem] = {
            "chars": path.stat().st_size,
            "path": str(path.relative_to(run.root)),
        }
    return {
        "ok": True,
        "action": "status",
        "run_id": run.run_id,
        "run_dir": str(run.root),
        "stages": stages,
        "checkpoint": checkpoint,
        "data": run.read_json("data", "index.json", default={}),
    }


ACTIONS = {
    "init": action_init,
    "plan": action_plan,
    "collect": action_collect,
    "prompt": action_prompt,
    "validate": action_validate,
    "debates": action_debates,
    "memory-begin": action_memory_begin,
    "memory-apply": action_memory_apply,
    "settle": action_settle,
    "backtest-plan": action_backtest_plan,
    "backtest-summary": action_backtest_summary,
    "status": action_status,
}


# ---- CLI -------------------------------------------------------------------


@dataclass
class WorkerArgs:
    """The action options after parsing, shared by both invocation modes."""

    run_dir: str | None
    stage: str | None = None
    analyst: str | None = None
    lenient: bool = False
    json_args: dict[str, Any] = field(default_factory=dict)


def dispatch(action: str, args: WorkerArgs) -> tuple[dict[str, Any], int]:
    """Run one action and return ``(payload, exit_code)``.

    Both the one-shot CLI and ``serve`` go through here, so the two transports
    cannot drift into different validation or error behavior.
    """
    try:
        if not args.run_dir:
            raise BadInput(f"run_dir is required for action {action!r}")
        run = RunDir(args.run_dir).ensure() if action == "init" else RunDir.open(args.run_dir)
        payload = ACTIONS[action](run, args)
    except ValidationFailed as exc:
        return {**exc.payload(), "action": action, "errors": exc.errors}, EXIT_VALIDATION
    except WorkerError as exc:
        return {**exc.payload(), "action": action}, exc.exit_code
    except Exception as exc:  # noqa: BLE001 — the plugin needs a JSON error, not a traceback
        return {"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"}, EXIT_INTERNAL
    return payload, EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingagents.opencode_worker", add_help=True)
    parser.add_argument("action", choices=sorted([*ACTIONS, "serve"]))
    parser.add_argument("--run-dir", default=None, help="the run directory (not used by serve)")
    parser.add_argument("--stage", default=None, help="stage key, for prompt/validate")
    parser.add_argument("--analyst", default=None, help="analyst key, for collect")
    parser.add_argument("--lenient", action="store_true", help="accept a degraded output with a warning")
    parser.add_argument("--json-args", default=None, help="path to a JSON object of extra arguments")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "serve":
        from tradingagents.opencode.server import serve

        return serve()

    if not args.run_dir:
        parser.error("--run-dir is required")

    try:
        json_args = _read_json_args(args.json_args)
    except WorkerError as exc:
        _result(exc.payload())
        return exc.exit_code

    payload, code = dispatch(
        args.action,
        WorkerArgs(
            run_dir=args.run_dir,
            stage=args.stage,
            analyst=args.analyst,
            lenient=args.lenient,
            json_args=json_args,
        ),
    )
    _result(payload)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
