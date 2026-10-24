"""The stage manifest: order, tiers, and output targets for one run.

The plugin asks the worker for this plan instead of duplicating the order in
TypeScript, so the graph's turn order, round counts, and tier choices live in
one place. Every stage is one generation call; groups decide what may run
concurrently.

Order mirrors ``tradingagents.graph.setup``:

    analysts (parallel) -> bull/bear debate -> research manager -> trader
    -> aggressive/conservative/neutral risk debate -> portfolio manager

The debate speakers alternate exactly as ``ConditionalLogic`` routes them, and
the round counts come from ``AnalysisRequest.max_*_rounds``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from tradingagents.opencode.contract import AnalysisRequest

# Analyst key -> (stage, report key, prompt builder key). Kept in report order.
ANALYST_STAGES: dict[str, tuple[str, str, str]] = {
    "market": ("market_analyst", "market_report", "market_analyst"),
    "social": ("sentiment_analyst", "sentiment_report", "sentiment_analyst"),
    "news": ("news_analyst", "news_report", "news_analyst"),
    "fundamentals": ("fundamentals_analyst", "fundamentals_report", "fundamentals_analyst"),
}

# Report key -> the stage that produces it, for assembling downstream state.
ANALYST_REPORTS = {
    report: stage for stage, report, _ in ANALYST_STAGES.values()
}

INVESTMENT_SPEAKERS = (
    ("bull_researcher", "Bull Analyst", "Bull"),
    ("bear_researcher", "Bear Analyst", "Bear"),
)

RISK_SPEAKERS = (
    ("aggressive_analyst", "Aggressive Analyst", "Aggressive"),
    ("conservative_analyst", "Conservative Analyst", "Conservative"),
    ("neutral_analyst", "Neutral Analyst", "Neutral"),
)

STRUCTURED_STAGES = {
    "research_manager": ("investment_plan", "deep"),
    "trader": ("trader_investment_plan", "quick"),
    "portfolio_manager": ("final_trade_decision", "deep"),
}


@dataclass(frozen=True)
class StageDef:
    """One generation call, and where its output goes."""

    stage: str
    tier: str
    # "report": analyst prose from a data pack; "argument": one debate turn;
    # "structured": JSON validated against a pydantic schema.
    kind: str
    group: str
    builder: str
    report_key: str = ""
    # The request analyst key a report stage reads its pack for.
    analyst: str = ""
    debate: str = ""
    speaker: str = ""
    # Position in the debate's turn list; -1 outside a debate.
    index: int = -1

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", -1)}


@dataclass(frozen=True)
class StageGroup:
    name: str
    parallel: bool
    stages: tuple[StageDef, ...]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "parallel": self.parallel,
            "stages": [stage.to_dict() for stage in self.stages],
        }


def _analyst_group(request: AnalysisRequest) -> StageGroup:
    stages = tuple(
        StageDef(
            stage=stage,
            tier="quick",
            kind="report",
            group="analysts",
            builder=builder,
            report_key=report,
            analyst=analyst,
        )
        for analyst, (stage, report, builder) in ANALYST_STAGES.items()
        if analyst in request.analysts
    )
    return StageGroup("analysts", True, stages)


def _debate_group(
    name: str,
    debate: str,
    speakers: tuple[tuple[str, str, str], ...],
    rounds: int,
) -> StageGroup:
    stages = [
        StageDef(
            stage=f"{key}_{round_number}",
            tier="quick",
            kind="argument",
            group=name,
            builder=key,
            debate=debate,
            speaker=label,
            index=(round_number - 1) * len(speakers) + position,
        )
        for round_number in range(1, rounds + 1)
        for position, (key, label, _) in enumerate(speakers)
    ]
    return StageGroup(name, False, tuple(stages))


def _structured(stage: str) -> StageGroup:
    report_key, tier = STRUCTURED_STAGES[stage]
    return StageGroup(
        stage,
        False,
        (
            StageDef(
                stage=stage,
                tier=tier,
                kind="structured",
                group=stage,
                builder=stage,
                report_key=report_key,
            ),
        ),
    )


def build_plan(request: AnalysisRequest) -> list[StageGroup]:
    """Every stage this run will run, in order, grouped by what may run together."""
    return [
        _analyst_group(request),
        _debate_group(
            "research_debate",
            "investment",
            INVESTMENT_SPEAKERS,
            request.max_debate_rounds,
        ),
        _structured("research_manager"),
        _structured("trader"),
        _debate_group(
            "risk_debate",
            "risk",
            RISK_SPEAKERS,
            request.max_risk_discuss_rounds,
        ),
        _structured("portfolio_manager"),
    ]


def stage_defs(request: AnalysisRequest) -> dict[str, StageDef]:
    """Stage key -> definition, in plan order."""
    return {
        stage.stage: stage
        for group in build_plan(request)
        for stage in group.stages
    }


def stage_order(request: AnalysisRequest) -> list[str]:
    return list(stage_defs(request))


def plan_payload(request: AnalysisRequest) -> dict:
    groups = build_plan(request)
    return {
        "ok": True,
        "action": "plan",
        "groups": [group.to_dict() for group in groups],
        "order": [stage.stage for group in groups for stage in group.stages],
    }
