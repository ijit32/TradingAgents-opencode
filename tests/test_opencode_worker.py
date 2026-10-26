"""The OpenCode worker's contract, run directory, pack, and stage actions."""

import json

import pytest

from tradingagents.opencode import cli
from tradingagents.opencode.contract import (
    EXIT_BAD_INPUT,
    EXIT_VALIDATION,
    AnalysisRequest,
)


def _write_request(root, **overrides):
    payload = {"ticker": "NVDA", "trade_date": "2026-10-23", **overrides}
    (root / "request.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(out[-1]) if out else {}
    return code, payload


@pytest.mark.unit
def test_init_normalizes_request_and_writes_settings(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, depth=2)

    code, out = _run(capsys, "init", "--run-dir", str(root))

    assert code == 0
    assert out["ok"] is True
    saved = json.loads((root / "request.json").read_text())
    assert saved["analysts"] == ["market", "social", "news", "fundamentals"]
    assert saved["max_debate_rounds"] == 2
    assert saved["max_risk_discuss_rounds"] == 1

    settings = json.loads((root / "run.json").read_text())
    assert settings["analysts"] == ["market", "social", "news", "fundamentals"]
    assert settings["max_debate_rounds"] == 2
    assert settings["llm_provider"] == "opencode"
    assert "instrument_context" in json.loads((root / "meta.json").read_text())


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["../etc/passwd", "..", "NVDA/../../x", ""])
def test_init_rejects_unsafe_tickers(tmp_path, capsys, ticker):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, ticker=ticker)

    code, out = _run(capsys, "init", "--run-dir", str(root))

    assert code == EXIT_BAD_INPUT
    assert out["ok"] is False


@pytest.mark.unit
@pytest.mark.parametrize("trade_date", ["2026-13-01", "23-10-2026", "2999-01-01"])
def test_init_rejects_bad_or_future_dates(tmp_path, capsys, trade_date):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, trade_date=trade_date)

    code, out = _run(capsys, "init", "--run-dir", str(root))

    assert code == EXIT_BAD_INPUT
    assert out["ok"] is False


@pytest.mark.unit
def test_init_rejects_unknown_analyst(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, analysts=["market", "astrology"])

    code, out = _run(capsys, "init", "--run-dir", str(root))

    assert code == EXIT_BAD_INPUT
    assert "astrology" in out["error"]


@pytest.fixture
def patched_tools(monkeypatch):
    """Deterministic vendor output: no network, predictable sizes."""
    from tradingagents.agents import tools as tool_module

    calls = []

    def fake_route(name, *args):
        calls.append((name, args))
        return f"{name} output for {args[1] if len(args) > 1 else args}\n" + ("row,1,2,3\n" * 40)

    def fake_snapshot(symbol, curr_date, look_back_days):
        calls.append(("get_verified_market_snapshot", (symbol, curr_date, look_back_days)))
        return f"verified snapshot for {symbol} on {curr_date}\n" + ("close,100\n" * 30)

    monkeypatch.setattr(tool_module, "route_to_vendor", fake_route)
    monkeypatch.setattr(tool_module, "build_verified_market_snapshot", fake_snapshot)
    return calls


@pytest.mark.unit
def test_collect_writes_a_bounded_pack(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    assert _run(capsys, "init", "--run-dir", str(root))[0] == 0

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "market")

    assert code == 0
    assert out["ok"] is True
    pack = (root / "data" / "market.md").read_text()
    assert len(pack) <= 20_000
    assert "Market data pack — NVDA as of 2026-10-23" in pack
    assert "get_stock_data" in pack
    for indicator in ("close_50_sma", "rsi", "atr"):
        assert indicator in pack
    assert "verified snapshot" in pack

    index = json.loads((root / "data" / "index.json").read_text())
    assert index["market"]["chars"] == len(pack)
    assert index["market"]["sources"]
    # Every tool call is dated: the injected state is passed explicitly.
    assert all(call for call in patched_tools)
    stock_calls = [call for call in patched_tools if call[0] == "get_stock_data"]
    assert stock_calls and stock_calls[0][1][1] == "2026-04-26"


@pytest.mark.unit
def test_collect_truncates_oversized_packs(tmp_path, capsys, monkeypatch):
    from tradingagents.agents import tools as tool_module

    monkeypatch.setattr(
        tool_module,
        "route_to_vendor",
        lambda name, *args: "x" * 30_000,
    )
    monkeypatch.setattr(
        tool_module,
        "build_verified_market_snapshot",
        lambda symbol, curr_date, look_back_days: "snapshot",
    )
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "market")

    assert code == 0
    assert out["truncated"] is True
    pack = (root / "data" / "market.md").read_text()
    assert len(pack) <= 20_000
    assert "[truncated:" in pack
    assert "snapshot" in pack  # never trimmed


@pytest.mark.unit
def test_prompt_is_deterministic_and_tool_free(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))
    _run(capsys, "collect", "--run-dir", str(root), "--analyst", "market")

    first_code, first = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "market_analyst")
    first_text = (root / "stages" / "market_analyst.prompt.md").read_text()
    second_code, second = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "market_analyst")
    second_text = (root / "stages" / "market_analyst.prompt.md").read_text()

    assert first_code == second_code == 0
    assert first_text == second_text
    assert first["hash"] == second["hash"]
    assert first["model_tier"] == "quick"
    assert "Pre-fetched market data pack" in first_text
    assert "call get_stock_data first" not in first_text
    assert "call get_verified_market_snapshot" not in first_text


@pytest.mark.unit
def test_validate_rejects_a_short_report_and_accepts_it_lenient(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))
    _run(capsys, "collect", "--run-dir", str(root), "--analyst", "market")
    _run(capsys, "prompt", "--run-dir", str(root), "--stage", "market_analyst")
    (root / "stages" / "market_analyst.out.txt").write_text("too short", encoding="utf-8")

    code, out = _run(capsys, "validate", "--run-dir", str(root), "--stage", "market_analyst")
    assert code == EXIT_VALIDATION
    assert out["errors"]

    code, out = _run(
        capsys, "validate", "--run-dir", str(root), "--stage", "market_analyst", "--lenient"
    )
    assert code == 0
    assert out["lenient"] is True
    assert (root / "stages" / "market_analyst.md").read_text().startswith("> [review]")


@pytest.mark.unit
def test_validate_promotes_a_full_report(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))
    _run(capsys, "collect", "--run-dir", str(root), "--analyst", "market")
    (root / "stages" / "market_analyst.out.txt").write_text("t" * 400, encoding="utf-8")

    code, out = _run(capsys, "validate", "--run-dir", str(root), "--stage", "market_analyst")

    assert code == 0
    assert out["chars"] == 400
    assert (root / "stages" / "market_analyst.md").read_text().strip() == "t" * 400


@pytest.mark.unit
def test_unknown_stage_is_bad_input(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "nope")

    assert code == EXIT_BAD_INPUT
    assert "unknown stage" in out["error"]


@pytest.mark.unit
def test_request_normalizes_models_and_language():
    request = AnalysisRequest.from_mapping(
        {
            "ticker": "BTC-USD",
            "trade_date": "2026-01-05",
            "asset_type": "CRYPTO",
            "analysts": ["news", "market"],
            "models": {"quick": "opencode/deepseek-v4.1-flash-free", "ignored": 3},
        }
    )

    payload = request.to_dict()

    assert payload["asset_type"] == "crypto"
    assert payload["analysts"] == ["market", "news"]
    assert payload["models"] == {"quick": "opencode/deepseek-v4.1-flash-free"}


@pytest.fixture
def patched_social(monkeypatch):
    """No network: the two social fetchers and the optional Jev screen are stubbed."""
    from tradingagents.agents import post_screen
    from tradingagents.dataflows.vendors import reddit, stocktwits

    calls = []

    def fake_stocktwits(ticker, **kwargs):
        calls.append(("stocktwits", ticker, kwargs))
        return "ST: NVDA bullish, buying the dip"

    def fake_reddit(ticker, subreddits=None, **kwargs):
        calls.append(("reddit", ticker, tuple(subreddits or ()), kwargs))
        return "RD: NVDA discussion thread"

    monkeypatch.setattr(post_screen, "jev_screen", lambda ticker: None)
    monkeypatch.setattr(stocktwits, "fetch_stocktwits_messages", fake_stocktwits)
    monkeypatch.setattr(reddit, "fetch_reddit_posts", fake_reddit)
    return calls


@pytest.mark.unit
def test_collect_sentiment_writes_a_windowed_pack(tmp_path, capsys, patched_tools, patched_social):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "social")

    assert code == 0
    assert out["analyst"] == "social"
    pack = (root / "data" / "social.md").read_text()
    assert "Sentiment data pack — NVDA as of 2026-10-23" in pack
    assert "ST: NVDA bullish, buying the dip" in pack
    assert "RD: NVDA discussion thread" in pack
    assert "news output" in pack
    assert "r/wallstreetbets" in pack
    # The window is passed to both social fetchers and never ends past the run date.
    stocktwits_call = next(call for call in patched_social if call[0] == "stocktwits")
    reddit_call = next(call for call in patched_social if call[0] == "reddit")
    assert stocktwits_call[2]["start_date"] == "2026-10-16"
    assert stocktwits_call[2]["end_date"] == "2026-10-23"
    assert reddit_call[3]["end_date"] == "2026-10-23"
    # A historical run says so: these feeds are not archived.
    assert "not point-in-time" in pack
    assert any("not point-in-time" in note for note in out["notes"])
    # US: the news section is still Yahoo's, and StockTwits was fetched.
    assert "News headlines — Yahoo Finance" in pack
    assert "StockTwits messages — user-labelled" in pack


@pytest.mark.unit
def test_collect_sentiment_for_an_india_listing_skips_stocktwits(
    tmp_path, capsys, patched_tools, patched_social
):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, ticker="TCS.NS")
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "social")

    assert code == 0
    pack = (root / "data" / "social.md").read_text()
    assert "Sentiment data pack — TCS.NS as of 2026-10-23" in pack
    # No StockTwits request: the section says why instead of reporting a 404.
    assert not [call for call in patched_social if call[0] == "stocktwits"]
    assert "StockTwits — not applicable to NSE/BSE listings" in pack
    assert "<stocktwits not applicable to NSE/BSE listings>" in pack
    assert "not evidence of indifference" in pack
    assert "no cashtag stream exists for this run" in pack
    # Reddit searched the Indian communities, not wallstreetbets.
    reddit_call = next(call for call in patched_social if call[0] == "reddit")
    assert reddit_call[2] == (
        "IndiaInvestments",
        "IndianStockMarket",
        "DalalStreetBets",
        "IndianStreetBets",
    )
    assert "r/IndiaInvestments" in pack
    assert "News headlines — Google News, GDELT and Indian publishers" in pack
    # The pack's index records the skipped source as a note, not a silent gap.
    assert any("StockTwits does not list NSE/BSE instruments" in note for note in out["notes"])


@pytest.mark.unit
def test_collect_news_covers_macro_and_odds_without_future_data(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "news")

    assert code == 0
    pack = (root / "data" / "news.md").read_text()
    for section in ("Global and macro news", "FRED macro series", "Prediction markets"):
        assert section in pack
    for indicator in ("cpi", "core_pce", "unemployment", "fed_funds_rate", "10y_treasury", "yield_curve"):
        assert f"### {indicator}" in pack
    for topic in ("Fed rate cut", "recession 2026", "NVDA"):
        assert f"### {topic}" in pack
    assert "searched by issuer name" not in pack

    # Every dated call is clamped to the analysis date, never later.
    dated = {
        "get_news": 2,
        "get_global_news": 0,
        "get_macro_indicators": 1,
        "get_prediction_markets": 2,
    }
    seen = set()
    for name, args in patched_tools:
        if name not in dated:
            continue
        seen.add(name)
        assert str(args[dated[name]]) <= "2026-10-23"
    assert seen == set(dated)


@pytest.mark.unit
def test_collect_news_names_the_india_sources(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, ticker="RELIANCE.NS")
    assert _run(capsys, "init", "--run-dir", str(root))[0] == 0

    code, _ = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "news")

    assert code == 0
    pack = (root / "data" / "news.md").read_text()
    assert "News data pack — RELIANCE.NS as of 2026-10-23" in pack
    assert "searched by issuer name" in pack
    assert "exchange's own filing record" in pack


@pytest.mark.unit
def test_collect_fundamentals_writes_statements_and_profile(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "fundamentals")

    assert code == 0
    pack = (root / "data" / "fundamentals.md").read_text()
    for section in (
        "Company profile and fundamentals",
        "Income statement (quarterly)",
        "Balance sheet (quarterly)",
        "Cash flow statement (quarterly)",
        "Insider transactions",
    ):
        assert section in pack
    assert "get_income_statement output" in pack
    # Quarterly statements are asked for, and dated no later than the run date.
    names = [name for name, _ in patched_tools]
    assert "get_income_statement" in names and "get_cashflow" in names
    for name, args in patched_tools:
        if name in {"get_balance_sheet", "get_cashflow", "get_income_statement"}:
            assert args[1] == "quarterly"
            assert args[2] <= "2026-10-23"


@pytest.mark.unit
def test_a_failed_vendor_degrades_one_section_not_the_pack(tmp_path, capsys, monkeypatch):
    from tradingagents.agents import tools as tool_module

    def selective_route(name, *args):
        if name == "get_cashflow":
            raise ConnectionError("vendor timeout")
        return f"{name} output\n" + ("row,1,2,3\n" * 20)

    monkeypatch.setattr(tool_module, "route_to_vendor", selective_route)
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "fundamentals")

    assert code == 0
    assert out["truncated"] is False
    pack = (root / "data" / "fundamentals.md").read_text()
    assert "get_cashflow unavailable (ConnectionError: vendor timeout)" in pack
    assert "get_income_statement output" in pack
    assert any("get_cashflow unavailable" in note for note in out["notes"])


@pytest.mark.unit
@pytest.mark.parametrize("analyst", ["market", "social", "news", "fundamentals"])
def test_every_pack_respects_the_hard_cap(tmp_path, capsys, monkeypatch, analyst):
    from tradingagents.agents import post_screen, tools as tool_module
    from tradingagents.dataflows.vendors import reddit, stocktwits

    monkeypatch.setattr(tool_module, "route_to_vendor", lambda name, *args: "x" * 30_000)
    monkeypatch.setattr(
        tool_module, "build_verified_market_snapshot", lambda symbol, curr_date, look_back_days: "snapshot"
    )
    monkeypatch.setattr(post_screen, "jev_screen", lambda ticker: None)
    monkeypatch.setattr(stocktwits, "fetch_stocktwits_messages", lambda *a, **k: "s" * 30_000)
    monkeypatch.setattr(reddit, "fetch_reddit_posts", lambda *a, **k: "r" * 30_000)

    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", analyst)

    assert code == 0
    assert out["truncated"] is True
    pack = (root / f"data/{analyst}.md").read_text()
    assert len(pack) <= 20_000
    assert "[truncated:" in pack


@pytest.mark.unit
def test_collect_rejects_an_analyst_outside_the_request(tmp_path, capsys, patched_tools):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, analysts=["market"])
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "collect", "--run-dir", str(root), "--analyst", "news")

    assert code == EXIT_BAD_INPUT
    assert "not selected" in out["error"]


# --- Phase 3: debates and structured decisions ------------------------------


def _stage_out(root, stage, text):
    target = root / "stages" / f"{stage}.out.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _decision(**fields):
    return json.dumps(fields)


@pytest.mark.unit
def test_plan_follows_the_graph_turn_order(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, depth=2)
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "plan", "--run-dir", str(root))

    assert code == 0
    assert [group["name"] for group in out["groups"]] == [
        "analysts",
        "research_debate",
        "research_manager",
        "trader",
        "risk_debate",
        "portfolio_manager",
    ]
    names = out["order"]
    assert names[:4] == [
        "market_analyst",
        "sentiment_analyst",
        "news_analyst",
        "fundamentals_analyst",
    ]
    # depth 2 is two bull/bear rounds and one three-way risk round.
    assert names[4:8] == [
        "bull_researcher_1",
        "bear_researcher_1",
        "bull_researcher_2",
        "bear_researcher_2",
    ]
    assert names[8:10] == ["research_manager", "trader"]
    assert names[10:13] == [
        "aggressive_analyst_1",
        "conservative_analyst_1",
        "neutral_analyst_1",
    ]
    assert names[-1] == "portfolio_manager"
    # Only the analysts group may run concurrently.
    assert [group["parallel"] for group in out["groups"]] == [
        True, False, False, False, False, False,
    ]


@pytest.mark.unit
def test_plan_only_includes_selected_analysts(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, analysts=["news"])
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "plan", "--run-dir", str(root))

    assert code == 0
    assert [stage for stage in out["order"] if stage.endswith("_analyst") and "risk" not in stage] == [
        "news_analyst"
    ]


@pytest.mark.unit
def test_debate_turns_accumulate_and_a_rerun_truncates_the_tail(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, depth=2)
    _run(capsys, "init", "--run-dir", str(root))

    bull = "Bull case: growth and margins. " * 20
    bear = "Bear case: valuation and competition. " * 20
    _stage_out(root, "bull_researcher_1", bull)
    code, first = _run(capsys, "validate", "--run-dir", str(root), "--stage", "bull_researcher_1")
    assert code == 0
    assert (root / "stages" / "bull_researcher_1.md").read_text().startswith("Bull Analyst: Bull case")

    _stage_out(root, "bear_researcher_1", bear)
    assert _run(capsys, "validate", "--run-dir", str(root), "--stage", "bear_researcher_1")[0] == 0

    code, prompt = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "bull_researcher_2")
    text = (root / "stages" / "bull_researcher_2.prompt.md").read_text()
    assert code == 0
    assert prompt["kind"] == "argument"
    assert "Bull Analyst: Bull case" in text
    assert "Bear Analyst: Bear case" in text
    assert "Last bear argument: Bear Analyst" in text

    # Rerunning turn 1 with new text drops the turn 2 transcript.
    _stage_out(root, "bull_researcher_1", "Bull case, revised: demand is stronger. " * 20)
    assert _run(capsys, "validate", "--run-dir", str(root), "--stage", "bull_researcher_1")[0] == 0
    _run(capsys, "prompt", "--run-dir", str(root), "--stage", "bull_researcher_2")
    revised = (root / "stages" / "bull_researcher_2.prompt.md").read_text()
    assert "revised" in revised
    assert "Bear Analyst: Bear case" not in revised


@pytest.mark.unit
def test_long_debate_compacts_older_turns_verbatim_last_two(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, depth=2)
    _run(capsys, "init", "--run-dir", str(root))

    turns = {
        "bull_researcher_1": "BULL-ONE " + "x" * 3000,
        "bear_researcher_1": "BEAR-ONE " + "y" * 3000,
        "bull_researcher_2": "BULL-TWO " + "z" * 3000,
    }
    for stage, text in turns.items():
        _stage_out(root, stage, text)
        assert _run(capsys, "validate", "--run-dir", str(root), "--stage", stage)[0] == 0

    code, _ = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "bear_researcher_2")
    text = (root / "stages" / "bear_researcher_2.prompt.md").read_text()
    assert code == 0
    assert "chars of this earlier turn omitted for length" in text
    # The last two turns are complete; the first two lost their tails.
    assert "BULL-TWO " + "z" * 3000 in text
    assert "BULL-ONE " + "x" * 3000 not in text


@pytest.mark.unit
def test_research_manager_prompt_carries_the_schema_and_parses_json(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))
    for stage, text in {
        "bull_researcher_1": "Bull case. " * 30,
        "bear_researcher_1": "Bear case. " * 30,
    }.items():
        _stage_out(root, stage, text)
        assert _run(capsys, "validate", "--run-dir", str(root), "--stage", stage)[0] == 0

    code, out = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "research_manager")
    text = (root / "stages" / "research_manager.prompt.md").read_text()
    assert code == 0
    assert out["schema"] and out["schema"]["properties"]["recommendation"]
    assert "## Structured output" in text
    assert '"recommendation"' in text
    assert "**Debate History:**" in text
    assert "Bull case." in text and "Bear case." in text

    _stage_out(
        root,
        "research_manager",
        _decision(
            recommendation="Buy",
            rationale="The bull case carried the growth argument.",
            strategic_actions="Add on strength, size to a standard allocation.",
        ),
    )
    code, validated = _run(capsys, "validate", "--run-dir", str(root), "--stage", "research_manager")
    report = (root / "stages" / "research_manager.md").read_text()
    assert code == 0
    assert validated["rating"] == "Buy"
    assert report.startswith("**Recommendation**: Buy")
    saved = json.loads((root / "stages" / "research_manager.json").read_text())
    assert saved["recommendation"] == "Buy"


@pytest.mark.unit
def test_an_unparseable_decision_is_validation_failed(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    _stage_out(root, "research_manager", "I recommend buying, but here is no JSON.")
    code, out = _run(capsys, "validate", "--run-dir", str(root), "--stage", "research_manager")

    assert code == EXIT_VALIDATION
    assert out["errors"]


@pytest.mark.unit
def test_lenient_decision_marks_review_and_reads_the_rating(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    _stage_out(
        root,
        "portfolio_manager",
        "**Rating**: Hold\n\n**Executive Summary**: keep the position.\n\n**Investment Thesis**: balanced.",
    )
    code, out = _run(
        capsys, "validate", "--run-dir", str(root), "--stage", "portfolio_manager", "--lenient"
    )

    assert code == 0
    assert out["rating"] == "Hold"
    report = (root / "stages" / "portfolio_manager.md").read_text()
    assert report.startswith("> [review]")
    assert "**Rating**: Hold" in report


@pytest.mark.unit
def test_trader_and_portfolio_manager_read_the_prior_stages(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    (root / "stages" / "research_manager.md").write_text(
        "**Recommendation**: Overweight\n\n**Rationale**: demand.\n\n"
        "**Strategic Actions**: add gradually.\n",
        encoding="utf-8",
    )
    code, out = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "trader")
    text = (root / "stages" / "trader.prompt.md").read_text()
    assert code == 0
    assert "**Recommendation**: Overweight" in text
    assert out["model_tier"] == "quick"

    _stage_out(
        root,
        "trader",
        _decision(action="Buy", reasoning="Plan and structure agree.", entry_price=190.5),
    )
    code, validated = _run(capsys, "validate", "--run-dir", str(root), "--stage", "trader")
    assert code == 0
    assert validated["action"] == "Buy"
    assert "FINAL TRANSACTION PROPOSAL: **BUY**" in (root / "stages" / "trader.md").read_text()

    for stage, text in {
        "aggressive_analyst_1": "Aggressive case. " * 20,
        "conservative_analyst_1": "Conservative case. " * 20,
        "neutral_analyst_1": "Neutral case. " * 20,
    }.items():
        _stage_out(root, stage, text)
        assert _run(capsys, "validate", "--run-dir", str(root), "--stage", stage)[0] == 0

    code, out = _run(capsys, "prompt", "--run-dir", str(root), "--stage", "portfolio_manager")
    text = (root / "stages" / "portfolio_manager.prompt.md").read_text()
    assert code == 0
    assert out["model_tier"] == "deep"
    assert "Trader's transaction proposal" in text
    assert "Aggressive case." in text and "Conservative case." in text

    _stage_out(
        root,
        "portfolio_manager",
        _decision(
            rating="Overweight",
            executive_summary="Add gradually.",
            investment_thesis="The bull case won on demand.",
        ),
    )
    code, validated = _run(capsys, "validate", "--run-dir", str(root), "--stage", "portfolio_manager")
    assert code == 0
    assert validated["rating"] == "Overweight"
    assert "**Rating**: Overweight" in (root / "stages" / "portfolio_manager.md").read_text()


@pytest.mark.unit
def test_unreadable_lenient_decision_becomes_review_never_hold(tmp_path, capsys):
    """A decision with no readable rating is REVIEW, not a made-up Hold."""
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root)
    _run(capsys, "init", "--run-dir", str(root))

    _stage_out(
        root,
        "portfolio_manager",
        "The risk debate was inconclusive; I would rather not commit to a stance "
        "until the missing news source is available again.",
    )
    code, out = _run(
        capsys, "validate", "--run-dir", str(root), "--stage", "portfolio_manager", "--lenient"
    )

    assert code == 0
    assert out["rating"] == "REVIEW"
    report = (root / "stages" / "portfolio_manager.md").read_text()
    assert report.startswith("> [review]")


# --- Phase 4: memory, reports, and the state log -----------------------------


def _memory_log(path):
    from tradingagents.memory.log import TradingMemoryLog

    return TradingMemoryLog({"memory_log_path": str(path)})


def _pending_decision(log, ticker="NVDA", date="2026-10-01"):
    log.store_decision(
        ticker=ticker,
        trade_date=date,
        final_trade_decision="**Rating**: Buy\n\nThe thesis was demand.",
        rating="Buy",
    )


@pytest.mark.unit
def test_memory_begin_renders_the_graph_reflection_prompt(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "trading_memory.md"
    _pending_decision(_memory_log(log_path))
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, memory_log_path=str(log_path), results_dir=str(tmp_path / "logs"))
    _run(capsys, "init", "--run-dir", str(root))

    from tradingagents.opencode import memory as memory_module

    monkeypatch.setattr(
        memory_module, "fetch_returns", lambda *a, **k: (0.042, 0.021, 5, "2026-10-08")
    )
    code, out = _run(capsys, "memory-begin", "--run-dir", str(root))

    assert code == 0
    assert out["due"] == 1
    assert out["log_path"] == str(log_path)
    entry = out["entries"][0]
    assert entry["ticker"] == "NVDA" and entry["date"] == "2026-10-01"
    assert entry["raw_return"] == pytest.approx(0.042)
    assert "Alpha vs SPY: +2.1%" in entry["user"]
    assert "Raw return over 5 trading days: +4.2%" in entry["user"]
    assert "The thesis was demand." in entry["user"]
    assert entry["system"].startswith("You are a trading analyst reviewing your own past decision")
    assert (root / "memory" / "pending.json").exists()


@pytest.mark.unit
def test_memory_begin_leaves_an_unfinished_window_pending(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "trading_memory.md"
    _pending_decision(_memory_log(log_path))
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, memory_log_path=str(log_path), results_dir=str(tmp_path / "logs"))
    _run(capsys, "init", "--run-dir", str(root))

    from tradingagents.opencode import memory as memory_module

    monkeypatch.setattr(memory_module, "fetch_returns", lambda *a, **k: (None, None, None, None))
    code, out = _run(capsys, "memory-begin", "--run-dir", str(root))

    assert code == 0
    assert out["due"] == 0
    assert out["not_ready"] == [{"ticker": "NVDA", "date": "2026-10-01"}]
    assert out["entries"] == []


@pytest.mark.unit
def test_memory_apply_writes_the_reflection_and_the_lesson(tmp_path, capsys, monkeypatch):
    log_path = tmp_path / "trading_memory.md"
    log = _memory_log(log_path)
    _pending_decision(log)
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, memory_log_path=str(log_path), results_dir=str(tmp_path / "logs"))
    _run(capsys, "init", "--run-dir", str(root))

    from tradingagents.opencode import memory as memory_module

    monkeypatch.setattr(
        memory_module, "fetch_returns", lambda *a, **k: (0.042, 0.021, 5, "2026-10-08")
    )
    _run(capsys, "memory-begin", "--run-dir", str(root))
    (root / "memory" / "reflections.json").write_text(
        json.dumps(
            {
                "reflections": [
                    {
                        "ticker": "NVDA",
                        "date": "2026-10-01",
                        "text": "The call read demand correctly; the alpha was +2.1%.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    code, out = _run(capsys, "memory-apply", "--run-dir", str(root))

    assert code == 0
    assert out["settled"] == [{"ticker": "NVDA", "date": "2026-10-01"}]
    assert out["note"] == ""
    entry = log.load_entries()[0]
    assert entry["pending"] is False
    assert entry["raw"] == "+4.2%"
    assert "alpha was +2.1%" in entry["reflection"]
    past_context = (root / "memory" / "past_context.md").read_text()
    assert "NVDA" in past_context and "alpha was +2.1%" in past_context
    assert (root / "memory" / "note.txt").read_text() == ""


@pytest.mark.unit
def test_memory_apply_keeps_an_entry_pending_when_the_reflection_is_missing(
    tmp_path, capsys, monkeypatch
):
    log_path = tmp_path / "trading_memory.md"
    log = _memory_log(log_path)
    _pending_decision(log)
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, memory_log_path=str(log_path), results_dir=str(tmp_path / "logs"))
    _run(capsys, "init", "--run-dir", str(root))

    from tradingagents.opencode import memory as memory_module

    monkeypatch.setattr(
        memory_module, "fetch_returns", lambda *a, **k: (0.042, 0.021, 5, "2026-10-08")
    )
    _run(capsys, "memory-begin", "--run-dir", str(root))

    code, out = _run(capsys, "memory-apply", "--run-dir", str(root))

    assert code == 0
    assert out["settled"] == []
    assert out["missing"] == [{"ticker": "NVDA", "date": "2026-10-01"}]
    assert "could not be settled" in out["note"]
    assert log.load_entries()[0]["pending"] is True
    assert (root / "memory" / "note.txt").read_text() == out["note"]


def _final_state():
    return {
        "ticker": "NVDA",
        "trade_date": "2026-10-23",
        "market_report": "Market: 210.96, below the 50 SMA.",
        "sentiment_report": "Sentiment: mixed.",
        "news_report": "News: no company items.",
        "fundamentals_report": "Fundamentals: 96,221M revenue.",
        "investment_debate_state": {
            "bull_history": "\nBull Analyst: margins.",
            "bear_history": "\nBear Analyst: multiple.",
            "history": "\nBull Analyst: margins.\nBear Analyst: multiple.",
            "current_response": "Bear Analyst: multiple.",
        },
        "investment_plan": "**Recommendation**: Overweight",
        "trader_investment_plan": "**Action**: Buy",
        "risk_debate_state": {
            "aggressive_history": "\nAggressive Analyst: press.",
            "conservative_history": "\nConservative Analyst: halve.",
            "neutral_history": "\nNeutral Analyst: ladder.",
            "history": "\nAggressive Analyst: press.",
        },
        "final_trade_decision": "**Rating**: Overweight\n\n**Executive Summary**: add.",
        "final_rating": "Overweight",
        "memory_note": "",
    }


@pytest.mark.unit
def test_settle_writes_the_report_tree_the_state_log_and_the_memory_entry(
    tmp_path, capsys
):
    log_path = tmp_path / "trading_memory.md"
    results = tmp_path / "logs"
    root = tmp_path / "run"
    root.mkdir()
    _write_request(root, memory_log_path=str(log_path), results_dir=str(results))
    _run(capsys, "init", "--run-dir", str(root))
    (root / "final_state.json").write_text(json.dumps(_final_state()), encoding="utf-8")

    code, out = _run(capsys, "settle", "--run-dir", str(root))

    assert code == 0
    assert out["rating"] == "Overweight"
    assert out["decision_recorded"] is True
    report_dir = root / "reports"
    assert (report_dir / "1_analysts" / "market.md").read_text().startswith("Market:")
    assert (report_dir / "2_research" / "manager.md").exists()
    assert (report_dir / "3_trading" / "trader.md").exists()
    assert (report_dir / "4_risk" / "neutral.md").exists()
    complete = (report_dir / "complete_report.md").read_text()
    assert complete.startswith("# Trading Analysis Report: NVDA")
    assert "## V. Portfolio Manager Decision" in complete
    page = (report_dir / "complete_report.html").read_text()
    assert "<html" in page and "Portfolio Manager" in page
    assert (report_dir / "run_notes.md").read_text().startswith("# Run notes")
    state_log = results / "NVDA" / "TradingAgentsStrategy_logs" / "full_states_log_2026-10-23.json"
    saved = json.loads(state_log.read_text())
    assert saved["final_rating"] == "Overweight"
    assert saved["investment_plan"] == "**Recommendation**: Overweight"
    assert saved["run_settings"]["llm_provider"] == "opencode"
    entries = _memory_log(log_path).load_entries()
    assert len(entries) == 1
    assert entries[0]["pending"] is True and entries[0]["rating"] == "Overweight"

    # A resume settles again without duplicating the decision or the report.
    code, out = _run(capsys, "settle", "--run-dir", str(root))
    assert code == 0
    assert len(_memory_log(log_path).load_entries()) == 1


@pytest.mark.unit
def test_settle_can_skip_the_html_page(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(
        root,
        memory_log_path=str(tmp_path / "memory.md"),
        results_dir=str(tmp_path / "logs"),
    )
    _run(capsys, "init", "--run-dir", str(root))
    (root / "final_state.json").write_text(json.dumps(_final_state()), encoding="utf-8")

    args_path = tmp_path / "args.json"
    args_path.write_text(json.dumps({"html": False}), encoding="utf-8")
    code, out = _run(capsys, "settle", "--run-dir", str(root), "--json-args", str(args_path))

    assert code == 0
    assert out["report_html"] == ""
    assert (root / "reports" / "complete_report.md").exists()
    assert not (root / "reports" / "complete_report.html").exists()


@pytest.mark.unit
def test_settle_rejects_a_run_without_a_final_state(tmp_path, capsys):
    root = tmp_path / "run"
    root.mkdir()
    _write_request(
        root,
        memory_log_path=str(tmp_path / "memory.md"),
        results_dir=str(tmp_path / "logs"),
    )
    _run(capsys, "init", "--run-dir", str(root))

    code, out = _run(capsys, "settle", "--run-dir", str(root))

    assert code == EXIT_BAD_INPUT
    assert out["ok"] is False
