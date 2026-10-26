"""Quarterly shareholding: select on the broadcast date, print the period end.

Offline: ``client.get_json`` is mocked from a fixture whose newest row is
broadcast on 2027-01-20 and whose next one is broadcast on 2026-10-21, so a run
dated 2026-10-01 must serve the June quarter and say the September pattern was
not yet public.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.nse import shareholding

_FIXTURE = Path(__file__).parent / "fixtures" / "india" / "shareholding.json"
_GET_JSON = "tradingagents.dataflows.vendors.nse.client.get_json"


def _rows() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _table_periods(text: str) -> list[str]:
    return [
        line.split("|")[1].strip()
        for line in text.splitlines()
        if line.startswith("| 20")
    ]


@pytest.mark.unit
def test_a_pattern_broadcast_after_the_run_date_is_excluded():
    with mock.patch(_GET_JSON, return_value=_rows()):
        text = shareholding.get_shareholding("RELIANCE.NS", "2026-10-01")

    assert "Source quarter: period ended 2026-06-30" in text
    assert "Promoter & promoter group: 50.48%" in text
    # The September quarter was filed on 2026-10-21, after the run's date.
    assert "A pattern for the quarter ended 2026-09-30 was filed after 2026-10-01" in text
    assert _table_periods(text)[0] == "2026-06-30"


@pytest.mark.unit
def test_history_is_ordered_by_period_end_and_capped_at_the_requested_quarters():
    with mock.patch(_GET_JSON, return_value=_rows()):
        text = shareholding.get_shareholding("RELIANCE.NS", "2026-10-25", quarters=2)

    assert _table_periods(text) == ["2026-09-30", "2026-06-30"]
    assert "2026-03-31" not in text


@pytest.mark.unit
def test_the_latest_snapshot_names_its_period_end_and_all_three_shares():
    with mock.patch(_GET_JSON, return_value=_rows()):
        text = shareholding.get_shareholding("RELIANCE.NS", "2026-10-25")

    assert "period ended 2026-09-30, broadcast 2026-10-21 16:25 IST" in text
    assert "not on 2026-10-25" in text
    assert "Promoter & promoter group: 50.44%" in text
    assert "Public: 49.56%" in text
    assert "Employee trusts: 0%" in text


@pytest.mark.unit
def test_a_pattern_published_after_the_run_date_is_named_not_shown():
    with mock.patch(_GET_JSON, return_value=_rows()):
        text = shareholding.get_shareholding("RELIANCE.NS", "2026-10-25")

    assert "2027-01-20" not in text
    assert "A pattern for the quarter ended 2026-12-31 was filed after 2026-10-25" in text


@pytest.mark.unit
def test_the_request_names_the_symbol_and_caches_for_a_day():
    with mock.patch(_GET_JSON, return_value=_rows()) as get_json:
        shareholding.get_shareholding("RELIANCE.NS", "2026-10-25")

    assert get_json.call_args.kwargs["params"] == {"index": "equities", "symbol": "RELIANCE"}
    assert get_json.call_args.kwargs["ttl_seconds"] == 24 * 60 * 60
    assert get_json.call_args.kwargs["key"] == "RELIANCE"


@pytest.mark.unit
def test_no_pattern_is_stated_as_unavailable_without_percentages():
    with mock.patch(_GET_JSON, return_value=[]):
        text = shareholding.get_shareholding("RELIANCE.NS", "2026-10-25")

    assert "shareholding pattern is unavailable" in text.lower()
    assert "%" not in text


@pytest.mark.unit
def test_a_non_india_ticker_is_refused_before_any_request():
    with (
        mock.patch(_GET_JSON) as get_json,
        pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
    ):
        shareholding.get_shareholding("AAPL", "2026-10-25")

    get_json.assert_not_called()
