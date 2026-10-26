"""Portfolio context: what the caller holds, threaded into the decision agents.

Decisions were made with no knowledge of the current book, so "add to a full
position" and "open a new one" read alike. The context is optional and carries
three distinct states: a position, a flat book, and no context at all. Nothing
may present the third as the second. The research team stays blind so the bull
and bear cases are not anchored by the caller's position.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.agents.context import get_portfolio_context_from_state
from tradingagents.portfolio import PortfolioContext, load_portfolio

HOLDING = {
    "cash": 25000.0,
    "currency": "USD",
    "positions": [
        {"ticker": "AAPL", "quantity": 120, "average_price": 150.0},
        {"ticker": "MSFT", "quantity": 10},
    ],
}


@pytest.mark.unit
def test_position_in_the_analyzed_instrument_leads_the_render():
    text = PortfolioContext.model_validate(HOLDING).render("AAPL")
    assert "120" in text and "150" in text
    assert "MSFT" in text and "25,000" in text and "USD" in text


@pytest.mark.unit
def test_flat_book_says_no_position_rather_than_omitting_it():
    text = PortfolioContext.model_validate({"cash": 1000.0, "positions": []}).render("AAPL")
    assert "No current position in AAPL" in text


@pytest.mark.unit
def test_a_ticker_held_under_another_spelling_is_matched():
    text = PortfolioContext.model_validate({"positions": [{"ticker": "aapl", "quantity": 5}]}).render("AAPL")
    assert "No current position" not in text and "5" in text


@pytest.mark.unit
def test_absent_context_is_reported_as_not_provided():
    notice = get_portfolio_context_from_state({"company_of_interest": "AAPL"})
    assert "not provided" in notice.lower()
    assert "no position" not in notice.lower()  # missing must not read as flat


@pytest.mark.unit
def test_rendered_context_reaches_the_agents_from_state():
    block = get_portfolio_context_from_state({"portfolio_context": "Portfolio: flat", "company_of_interest": "AAPL"})
    assert block == "Portfolio: flat"


@pytest.mark.unit
def test_load_rejects_a_malformed_file_with_a_clear_error(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"positions": [{"quantity": 5}]}))
    with pytest.raises(ValueError, match="portfolio"):
        load_portfolio(bad)


@pytest.mark.unit
def test_load_reads_a_valid_file(tmp_path):
    good = tmp_path / "p.json"
    good.write_text(json.dumps(HOLDING))
    assert load_portfolio(good).positions[0].ticker == "AAPL"


# --- currencies -------------------------------------------------------------

RELIANCE_INR = {
    "cash": 50000.0,
    "currency": "INR",
    "positions": [{"ticker": "RELIANCE.NS", "quantity": 3, "average_price": 1234.0}],
}


@pytest.mark.unit
def test_an_inr_book_validates_and_renders_inr():
    book = PortfolioContext.model_validate(RELIANCE_INR)
    text = book.render("RELIANCE.NS")
    assert book.currency == "INR"
    assert "Cash available: 50,000.00 INR" in text
    assert "average price 1,234.00 INR" in text


@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [("inr", "INR"), ("usd ", "USD")])
def test_currency_labels_are_stripped_and_upper_cased(raw, expected):
    assert PortfolioContext.model_validate({"currency": raw}).currency == expected


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["dollars", "XYZ"])
def test_an_unknown_currency_is_rejected_and_named(bad):
    with pytest.raises(ValueError, match=bad):
        PortfolioContext.model_validate({"currency": bad})


@pytest.mark.unit
def test_load_rejects_an_unknown_currency_naming_the_file_and_value(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"currency": "dollars"}))
    with pytest.raises(ValueError) as exc:
        load_portfolio(bad)
    message = str(exc.value)
    assert "p.json" in message and "dollars" in message


@pytest.mark.unit
def test_a_usd_book_on_an_indian_listing_names_both_currencies_without_converting():
    """A USD cash figure must never be read as INR, nor size an INR position."""
    book = PortfolioContext.model_validate(
        {"cash": 10000.0, "currency": "USD",
         "positions": [{"ticker": "RELIANCE.NS", "quantity": 2, "average_price": 1200.0}]}
    )
    text = book.render("RELIANCE.NS")
    assert "Cash available: 10,000.00 USD" in text
    assert "average price 1,200.00 INR" in text
    assert "Portfolio currency is USD" in text
    assert "RELIANCE.NS is quoted in INR" in text
    assert "not converted" in text


@pytest.mark.unit
def test_an_inr_book_on_an_indian_listing_needs_no_two_currency_warning():
    text = PortfolioContext.model_validate(RELIANCE_INR).render("RELIANCE.NS")
    assert "Portfolio currency is" not in text
    assert "not converted" not in text


@pytest.mark.unit
def test_a_book_without_a_currency_does_not_claim_its_cash_is_in_inr():
    book = PortfolioContext.model_validate(
        {"cash": 10000.0, "positions": [{"ticker": "RELIANCE.NS", "quantity": 2}]}
    )
    text = book.render("RELIANCE.NS")
    assert "Cash available: 10,000.00\n" in text
    assert "10,000.00 INR" not in text
    assert "quoted in INR" in text and "cash currency was not provided" in text


@pytest.mark.unit
def test_an_unverified_quote_currency_makes_no_currency_claim():
    book = PortfolioContext.model_validate(
        {"cash": 1000.0, "currency": "GBP",
         "positions": [{"ticker": "VOD.L", "quantity": 10, "average_price": 1.5}]}
    )
    text = book.render("VOD.L")
    assert "Cash available: 1,000.00 GBP" in text
    assert "average price 1.50\n" in text  # no ISO code appended to the price
    assert "quoted in" not in text and "not converted" not in text


# --- threading through the graph --------------------------------------------

def _bare_graph(tmp_path):
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.memory import TradingMemoryLog

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"memory_log_path": str(tmp_path / "m.md"), "max_debate_rounds": 1,
                    "max_risk_discuss_rounds": 1}
    graph.memory_log = TradingMemoryLog(graph.config)
    graph.propagator = Propagator()
    graph.selected_analysts = ["market"]
    graph.settle_pending = lambda t: None
    graph.resolve_instrument_context = lambda t, a="stock", d=None: ""
    graph._memory_as_of = lambda d: None
    return graph


@pytest.mark.unit
def test_create_run_state_renders_the_portfolio_once(tmp_path):
    graph = _bare_graph(tmp_path)
    state = graph.create_run_state("AAPL", "2026-08-14", portfolio=PortfolioContext.model_validate(HOLDING))
    assert "120" in state["portfolio_context"]
    assert graph.create_run_state("AAPL", "2026-08-14")["portfolio_context"] == ""


@pytest.mark.unit
def test_checkpoint_signature_changes_with_the_portfolio(tmp_path):
    graph = _bare_graph(tmp_path)
    none = graph._run_signature("stock")
    flat = graph._run_signature("stock", PortfolioContext())
    held = graph._run_signature("stock", PortfolioContext.model_validate(HOLDING))
    assert len({none, flat, held}) == 3


@pytest.mark.unit
@pytest.mark.parametrize("module, factory", [
    ("tradingagents.agents.trader.trader", "create_trader"),
    ("tradingagents.agents.managers.portfolio_manager", "create_portfolio_manager"),
    ("tradingagents.agents.risk_mgmt.aggressive_debator", "create_aggressive_debator"),
    ("tradingagents.agents.risk_mgmt.conservative_debator", "create_conservative_debator"),
    ("tradingagents.agents.risk_mgmt.neutral_debator", "create_neutral_debator"),
])
def test_decision_agents_see_the_portfolio(module, factory, monkeypatch):
    """The prompt each decision agent sends carries the portfolio block."""
    import importlib

    mod = importlib.import_module(module)
    seen = []

    class _LLM:
        def invoke(self, prompt, *a, **k):
            seen.append(prompt if isinstance(prompt, str) else json.dumps(str(prompt)))
            from langchain_core.messages import AIMessage
            return AIMessage("Rating: Hold\n\nnothing to do")

        def with_structured_output(self, *a, **k):
            raise NotImplementedError  # force the free-text path

    state = {
        "company_of_interest": "AAPL", "trade_date": "2026-08-14", "asset_type": "stock",
        "instrument_context": "", "market_report": "M", "sentiment_report": "S",
        "news_report": "N", "fundamentals_report": "F", "investment_plan": "P",
        "trader_investment_plan": "T", "past_context": "",
        "portfolio_context": "PORTFOLIO_BLOCK_MARKER",
        "investment_debate_state": {"history": "", "count": 0},
        "risk_debate_state": {"history": "", "latest_speaker": "", "count": 0,
                              "aggressive_history": "", "conservative_history": "", "neutral_history": "",
                              "current_aggressive_response": "", "current_conservative_response": "",
                              "current_neutral_response": ""},
    }
    node = getattr(mod, factory)(_LLM())
    node(state)
    assert any("PORTFOLIO_BLOCK_MARKER" in p for p in seen), f"{factory} prompt lacks the portfolio block"


@pytest.mark.unit
def test_research_team_stays_blind_to_the_portfolio():
    import inspect

    from tradingagents.agents.researchers import bear_researcher, bull_researcher
    for mod in (bull_researcher, bear_researcher):
        assert "portfolio_context" not in inspect.getsource(mod)


@pytest.mark.unit
def test_completed_run_clears_the_checkpoint_it_wrote(tmp_path, monkeypatch):
    """The clear must key on the same portfolio the run was checkpointed under.

    Keyed on a different one it deletes nothing, and the next identical call
    resumes the finished thread and returns the old decision without running.
    """
    import tradingagents.graph.trading_graph as tg

    graph = _bare_graph(tmp_path)
    graph.config.update({"checkpoint_enabled": True, "data_cache_dir": str(tmp_path),
                         "results_dir": str(tmp_path)})
    graph.debug = False
    graph._resuming = False
    graph.propagator.get_graph_args = lambda callbacks=None: {}
    graph._log_state = lambda *a, **k: None
    graph.graph = type("G", (), {"invoke": lambda self, i, **k: {"final_trade_decision": "Rating: Hold\n\nx", "final_rating": "Hold"}})()
    book = PortfolioContext.model_validate(HOLDING)

    written = graph._run_signature("stock", book)  # what begin_checkpoint keys on
    cleared = []
    monkeypatch.setattr(tg, "clear_checkpoint", lambda d, t, dt, signature: cleared.append(signature))

    graph._run_graph("AAPL", "2026-08-14", "stock", checkpoint_thread_id=None, portfolio=book)

    assert cleared == [written]


@pytest.mark.unit
def test_research_layer_sizes_against_a_standard_allocation():
    """The research team is blind to the book, so its plan cannot promise
    position-relative sizing: it sizes against a standard allocation instead."""
    from tradingagents.agents.schemas import ResearchPlan

    description = ResearchPlan.model_fields["strategic_actions"].description
    assert "standard allocation" in description
    assert "does not see the caller's holdings" in description


@pytest.mark.unit
def test_partial_portfolio_states_only_what_it_was_given():
    """Cash omitted is not cash zero; the line is absent rather than invented."""
    text = PortfolioContext.model_validate({"positions": [{"ticker": "AAPL", "quantity": 5}]}).render("AAPL")
    assert "Cash" not in text
    assert "5" in text


@pytest.mark.unit
def test_cli_rejects_an_unusable_portfolio_file_before_running(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as m

    bad = tmp_path / "bad.json"
    bad.write_text('{"positions": [{"quantity": 5}]}')
    ran = []
    monkeypatch.setattr(m, "run_analysis", lambda **k: ran.append(k))

    result = CliRunner().invoke(m.app, ["--portfolio", str(bad)])

    assert result.exit_code == 1 and ran == []


@pytest.mark.unit
def test_cli_passes_a_valid_portfolio_into_the_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as m

    good = tmp_path / "good.json"
    good.write_text(json.dumps(HOLDING))
    ran = []
    monkeypatch.setattr(m, "run_analysis", lambda **k: ran.append(k))

    result = CliRunner().invoke(m.app, ["--portfolio", str(good)])

    assert result.exit_code == 0
    assert ran[0]["portfolio"].position_in("AAPL").quantity == 120
