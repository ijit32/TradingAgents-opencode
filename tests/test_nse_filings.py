"""The NSE filings feeds: market gating, the IST day boundary, and row selection.

Every test is offline. The transport is mocked at ``client.get_json`` — the one
seam every NSE adapter shares — and the fixtures are hand-written with NSE's real
field names and obviously synthetic values, including the timestamps the
point-in-time rules turn on (a 20:05 IST results filing, a 00:15 IST filing the
next day, a revision beside an original).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.ist import parse_ist, to_ist, today_ist
from tradingagents.dataflows.vendors.nse import filings

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "india"
_GET_JSON = "tradingagents.dataflows.vendors.nse.client.get_json"
_RESOLVE_BSE = "tradingagents.dataflows.vendors.nse.issuer.nse_symbol_for_bse"


def _fixture(name: str):
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _stamps(rows: list[filings.Filing]) -> list[str]:
    return [to_ist(row.published).strftime("%Y-%m-%d %H:%M") for row in rows]


def _financial(seq: str, qe: str, sub: str, basis: str | None, broadcast: str) -> dict:
    return {
        "seq_Id": seq,
        "symbol": "RELIANCE",
        "type": "Integrated Filing- Financials",
        "type_Sub": sub,
        "consolidated": basis,
        "qe_Date": qe,
        "broadcast_Date": broadcast,
        "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/SYNT_{seq}.xml",
    }


@pytest.mark.unit
def test_a_bse_listing_resolves_to_its_nse_symbol():
    with (
        mock.patch(_GET_JSON, return_value=_fixture("announcements.json")) as get_json,
        mock.patch(_RESOLVE_BSE, return_value="RELIANCE") as resolve,
    ):
        rows = filings.announcements("RELIANCE.BO", "2025-04-20", "2025-04-30")

    resolve.assert_called_once_with("RELIANCE")
    params = get_json.call_args.kwargs["params"]
    assert params == {
        "index": "equities",
        "from_date": "20-04-2025",
        "to_date": "30-04-2025",
        "symbol": "RELIANCE",
    }
    assert rows and all(row.symbol == "RELIANCE" for row in rows)


@pytest.mark.unit
def test_a_bse_scrip_with_no_nse_symbol_is_named_bse_only_without_a_request():
    with (
        mock.patch(_GET_JSON) as get_json,
        mock.patch(_RESOLVE_BSE, return_value=None),
        pytest.raises(NoMarketDataError, match="BSE-only"),
    ):
        filings.announcements("NAPEROL.BO", "2025-04-20", "2025-04-30")

    get_json.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        lambda: filings.announcements("AAPL", "2025-04-20", "2025-04-30"),
        lambda: filings.recent_filings("AAPL", "2025-04-25"),
        lambda: filings.integrated_filings("AAPL", "2025-04-20", "2025-04-30"),
        lambda: filings.latest_financials("AAPL", "2025-04-25"),
    ],
    ids=["announcements", "recent", "integrated", "latest"],
)
def test_a_non_india_ticker_is_refused_before_any_request(call):
    with (
        mock.patch(_GET_JSON) as get_json,
        pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
    ):
        call()

    get_json.assert_not_called()


@pytest.mark.unit
def test_recent_filings_uses_the_ist_day_and_sorts_newest_first():
    with mock.patch(_GET_JSON, return_value=_fixture("announcements.json")):
        rows = filings.recent_filings("RELIANCE.NS", "2025-04-25", lookback_days=10, limit=10)

    # The 00:15 IST filing of 26 April is 18:45 UTC on the 25th, so a UTC-day
    # comparison would admit it; the IST day comparison does not.
    assert _stamps(rows) == [
        "2025-04-25 20:10",
        "2025-04-25 20:05",
        "2025-04-25 20:05",
        "2025-04-22 22:10",
        "2025-04-21 18:12",
    ]
    subjects = [
        row.subject
        for row in rows
        if to_ist(row.published).date() == date(2025, 4, 25)
    ]
    assert subjects == ["Press Release", "Outcome of Board Meeting", "Dividend"]


@pytest.mark.unit
def test_recent_filings_excludes_the_evening_results_filing_one_day_earlier():
    with mock.patch(_GET_JSON, return_value=_fixture("announcements.json")):
        rows = filings.recent_filings("RELIANCE.NS", "2025-04-24", lookback_days=10)

    assert _stamps(rows) == ["2025-04-22 22:10", "2025-04-21 18:12"]
    assert all("2025-04-25" not in stamp for stamp in _stamps(rows))


@pytest.mark.unit
def test_recent_filings_honours_the_limit_and_reads_the_configured_lookback():
    set_config({"india": {"announcements_lookback_days": 30}})
    with mock.patch(_GET_JSON, return_value=_fixture("announcements.json")) as get_json:
        rows = filings.recent_filings("RELIANCE.NS", "2025-04-25", limit=2)

    assert len(rows) == 2
    assert get_json.call_args.kwargs["params"]["from_date"] == "26-03-2025"


@pytest.mark.unit
def test_recent_filings_keeps_the_structured_filing_flag():
    with mock.patch(_GET_JSON, return_value=_fixture("announcements.json")):
        rows = filings.recent_filings("RELIANCE.NS", "2025-04-25", lookback_days=10, limit=10)

    structured = [row for row in rows if row.has_xbrl]
    assert [row.subject for row in structured] == ["Outcome of Board Meeting"]


@pytest.mark.unit
def test_announcements_ttl_follows_how_old_the_window_is():
    today = today_ist()
    with mock.patch(_GET_JSON, return_value=[]) as get_json:
        filings.announcements("RELIANCE.NS", "2025-04-20", "2025-04-30")
        assert get_json.call_args.kwargs["ttl_seconds"] == 7 * 24 * 60 * 60

        filings.announcements(
            "RELIANCE.NS",
            (today - timedelta(days=3)).isoformat(),
            today.isoformat(),
        )
        assert get_json.call_args.kwargs["ttl_seconds"] == 24 * 60 * 60


@pytest.mark.unit
def test_a_payload_with_no_row_list_is_a_failure_not_an_empty_window():
    with (
        mock.patch(_GET_JSON, return_value={}),
        pytest.raises(NoMarketDataError, match="payload"),
    ):
        filings.announcements("RELIANCE.NS", "2025-04-20", "2025-04-30")


@pytest.mark.unit
def test_an_integrated_payload_with_no_row_list_is_a_failure_too():
    with (
        mock.patch(_GET_JSON, return_value={"totalCount": 0}),
        pytest.raises(NoMarketDataError, match="payload"),
    ):
        filings.latest_financials("RELIANCE.NS", "2026-10-25")


@pytest.mark.unit
def test_integrated_filings_filters_to_the_symbol_newest_first():
    payload = {
        "data": [
            _financial("a", "31-MAR-2026", "Original", "Consolidated", "20-Apr-2026 19:05:00"),
            {
                **_financial("b", "31-MAR-2026", "Original", "Standalone", "21-Apr-2026 10:00:00"),
                "symbol": "TCS",
            },
        ],
        "size": 2000,
        "page": 0,
        "totalCount": 2,
    }
    with mock.patch(_GET_JSON, return_value=payload):
        rows = filings.integrated_filings("RELIANCE.NS", "2026-04-01", "2026-04-30")

    assert [row["seq_Id"] for row in rows] == ["a"]


@pytest.mark.unit
def test_integrated_filings_walks_pages_until_the_feed_says_it_is_done():
    set_config({"india": {"integrated_filings_page_size": 2}})
    pages = [
        {
            "data": [
                _financial("a", "31-MAR-2026", "Original", "Consolidated", "20-Apr-2026 19:05:00"),
                _financial("b", "31-MAR-2026", "Original", "Standalone", "20-Apr-2026 19:06:00"),
            ],
            "size": 2,
            "page": 0,
            "totalCount": 3,
        },
        {
            "data": [
                _financial("c", "31-DEC-2025", "Original", "Consolidated", "20-Jan-2026 19:05:00"),
            ],
            "size": 2,
            "page": 0,
            "totalCount": 3,
        },
    ]
    with mock.patch(_GET_JSON, side_effect=pages) as get_json:
        rows = filings.integrated_filings("RELIANCE.NS", "2025-04-01", "2026-04-30")

    assert get_json.call_count == 2
    assert [call.kwargs["params"]["page"] for call in get_json.call_args_list] == [1, 2]
    assert [row["seq_Id"] for row in rows] == ["a", "b", "c"]


@pytest.mark.unit
def test_latest_financials_prefers_original_and_consolidated():
    payload = {
        "data": [
            _financial(
                "standalone", "31-MAR-2026", "Original", "Standalone", "20-Apr-2026 19:00:00"
            ),
            _financial(
                "consolidated", "31-MAR-2026", "Original", "Consolidated", "20-Apr-2026 19:05:00"
            ),
            _financial(
                "revised", "31-MAR-2026", "Revised", "Consolidated", "02-May-2026 10:00:00"
            ),
        ],
        "size": 2000,
        "page": 0,
        "totalCount": 3,
    }
    with mock.patch(_GET_JSON, return_value=payload):
        row = filings.latest_financials("RELIANCE.NS", "2026-06-30")

    assert row is not None
    assert row["seq_Id"] == "consolidated"


@pytest.mark.unit
def test_latest_financials_ignores_a_quarter_not_yet_filed():
    payload = {
        "data": [
            _financial("filed", "31-MAR-2026", "Original", "Consolidated", "20-Apr-2026 19:05:00"),
            _financial("next", "30-JUN-2026", "Original", "Consolidated", "17-Jul-2026 19:50:00"),
            _financial("future", "30-SEP-2026", "Original", "Consolidated", "20-Oct-2026 19:50:00"),
        ],
        "size": 2000,
        "page": 0,
        "totalCount": 3,
    }
    with mock.patch(_GET_JSON, return_value=payload):
        before = filings.latest_financials("RELIANCE.NS", "2026-05-31")
        after = filings.latest_financials("RELIANCE.NS", "2026-07-31")

    assert before is not None and before["seq_Id"] == "filed"
    assert after is not None and after["seq_Id"] == "next"


@pytest.mark.unit
def test_latest_financials_skips_governance_and_returns_none_when_nothing_is_structured():
    payload = {
        "data": [
            {
                "seq_Id": "gov",
                "symbol": "RELIANCE",
                "type": "Integrated Filing- Governance",
                "qe_Date": "30-JUN-2026",
                "broadcast_Date": "28-Jul-2026 18:43:22",
                "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SYNT_gov.xml",
            }
        ],
        "size": 2000,
        "page": 0,
        "totalCount": 1,
    }
    with mock.patch(_GET_JSON, return_value=payload):
        assert filings.latest_financials("RELIANCE.NS", "2026-08-01") is None


@pytest.mark.unit
def test_format_recent_filings_states_the_ist_time_and_the_missing_xbrl():
    rows = [
        filings.Filing(
            subject="Outcome of Board Meeting",
            published=parse_ist("25-Apr-2025 20:05:15"),
            url="https://nsearchives.nseindia.com/corporate/SYNT_FR.pdf",
            has_xbrl=False,
            symbol="RELIANCE",
        )
    ]
    text = filings.format_recent_filings(rows, "RELIANCE.NS", "2025-06-02")

    assert text.startswith("### As filed with NSE")
    assert "2025-04-25 20:05 IST" in text
    assert "Outcome of Board Meeting" in text
    assert "https://nsearchives.nseindia.com/corporate/SYNT_FR.pdf" in text
    assert "no structured XBRL" in text


@pytest.mark.unit
def test_format_recent_filings_says_nothing_was_filed_rather_than_inventing_one():
    text = filings.format_recent_filings([], "RELIANCE.NS", "2025-06-02")

    assert "no filing" in text.lower()
    assert "Outcome" not in text
