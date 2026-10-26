"""Reflection: a settled decision's outcome turned into a short lesson for later runs."""

from typing import Any


def reflection_system_prompt(holding_days: int) -> str:
    """The reflection system prompt, for any caller that owns its own LLM call.

    Concise by design: 2-4 sentences of plain prose, compact enough to be
    re-injected into future agent prompts without bloating the context window.
    The window is named because it bounds what the outcome can show: a thesis
    written for months is not disproved by a week, and a lesson that ignores
    the difference is read by later runs as an established failure.
    """
    return (
        "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
        f"The outcome covers {holding_days} trading days from the first close after the analysis date, "
        "which may be shorter than the horizon the decision was written for.\n"
        "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
        "Cover in order:\n"
        f"1. What the {holding_days}-day alpha shows about the directional call (cite the figure), "
        "and say so plainly if the window is too short to judge the thesis.\n"
        "2. Which part of the investment thesis this window supports or undercuts.\n"
        "3. One concrete lesson to apply to the next similar analysis.\n\n"
        "Be specific and terse. Your output will be stored verbatim in a memory log "
        "and re-read by future analysts, so every word must earn its place."
    )


def _currency_disclosure(
    benchmark_name: str, instrument_currency: str, benchmark_currency: str
) -> str:
    """The line that keeps a local-currency alpha from being read as a USD return.

    Named only when the instrument's currency is known and non-USD, so every US
    prompt stays byte-identical to the one the tests snapshot. An empty benchmark
    currency is reported as unverified rather than assumed to be USD.
    """
    if not benchmark_currency:
        return (
            f"Benchmark {benchmark_name}'s currency was not verified; the raw figure is in "
            f"{instrument_currency} and both figures are local-currency percentage returns, "
            "not converted across currencies."
        )
    if benchmark_currency != instrument_currency:
        return (
            f"The instrument is quoted in {instrument_currency} and benchmark "
            f"{benchmark_name} is quoted in {benchmark_currency}; the two figures are in "
            "different currencies and are local-currency percentage returns, not converted "
            "across currencies."
        )
    return (
        f"Benchmark {benchmark_name} is quoted in {benchmark_currency}; both figures are "
        "local-currency percentage returns and are not converted across currencies."
    )


def build_reflection_prompt(
    final_decision: str,
    raw_return: float,
    alpha_return: float,
    benchmark_name: str = "SPY",
    holding_days: int = 5,
    instrument_currency: str = "",
    benchmark_currency: str = "",
) -> list[tuple[str, str]]:
    """The reflection messages, exactly as the graph sends them.

    Extracted so an orchestrator that owns the model call (the OpenCode worker)
    renders the same prompt the graph would: if the two drift, a lesson written
    through one path stops being comparable with the other.

    ``instrument_currency`` and ``benchmark_currency`` are ISO codes, empty when
    unverified. The disclosure is added only for a known, non-USD instrument, so
    a US prompt is unchanged; the default arguments reproduce it exactly.
    """
    raw_line = f"Raw return over {holding_days} trading days: {raw_return:+.1%}"
    alpha_line = f"Alpha vs {benchmark_name}: {alpha_return:+.1%}"
    lines = [raw_line, alpha_line]
    if instrument_currency and instrument_currency != "USD":
        lines[0] = f"{raw_line} ({instrument_currency})"
        lines.append(
            _currency_disclosure(benchmark_name, instrument_currency, benchmark_currency)
        )
    return [
        ("system", reflection_system_prompt(holding_days)),
        (
            "human",
            "\n".join(lines) + f"\n\nFinal Decision:\n{final_decision}",
        ),
    ]


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm

    def _system_prompt(self, holding_days: int) -> str:
        """Concise prompt for reflect_on_final_decision (settled memory log entries)."""
        return reflection_system_prompt(holding_days)

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
        holding_days: int = 5,
        instrument_currency: str = "",
        benchmark_currency: str = "",
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used when a pending decision is settled. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        ``benchmark_name`` is the label used for the alpha line (e.g. ``"SPY"``
        for US tickers, ``"^N225"`` for ``.T`` listings); defaults to SPY for
        callers that haven't been updated to thread the benchmark through.
        ``instrument_currency`` and ``benchmark_currency`` are the currencies the
        two figures are quoted in, empty when unverified; a non-USD instrument
        gets a disclosure so its alpha is not read as a USD return.
        """
        messages = build_reflection_prompt(
            final_decision,
            raw_return,
            alpha_return,
            benchmark_name,
            holding_days,
            instrument_currency,
            benchmark_currency,
        )
        return self.quick_thinking_llm.invoke(messages).content
