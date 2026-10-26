"""SEBI PIT disclosures: the intimation date is the gate, the trade date is context.

Offline: ``client.get_json`` is mocked. The fixture's first row trades on
2026-02-09 and is intimated on 2026-02-16 — the week that must keep a run dated
inside it from seeing the disclosure.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.nse import pit

_FIXTURE = Path(__file__).parent / "fixtures" / "india" / "pit.json"
_GET_JSON = "tradingagents.dataflows.vendors.nse.client.get_json"


def _payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_a_trade_is_served_on_its_intimation_date_not_its_trade_date():
    with mock.patch(_GET_JSON, return_value=_payload()):
        before = pit.get_insider_transactions("RELIANCE.NS", "2026-02-13")
        on = pit.get_insider_transactions("RELIANCE.NS", "2026-02-16")

    assert "SYNTHETIC PROMOTER ONE" not in before
    assert "SYNTHETIC PROMOTER ONE" in on
    # The trade date is still shown, because the gap is the point.
    assert "2026-02-09" in on
    assert "2026-02-16" in on


@pytest.mark.unit
def test_disclosures_are_newest_first_and_never_from_another_issuer():
    with mock.patch(_GET_JSON, return_value=_payload()):
        text = pit.get_insider_transactions("RELIANCE.NS", "2026-10-25")

    assert text.index("SYNTHETIC HOLDER TWO") < text.index("SYNTHETIC PROMOTER ONE")
    assert "SYNTHETIC OTHER ISSUER INSIDER" not in text
    # Filed on 2026-11-20, after the run's date.
    assert "46813579" not in text


@pytest.mark.unit
def test_the_header_does_not_imply_the_disclosure_existed_before_intimation():
    with mock.patch(_GET_JSON, return_value=_payload()):
        text = pit.get_insider_transactions("RELIANCE.NS", "2026-10-25")

    assert "trade date and the intimation date differ" in text
    assert "2026-04-28 to 2026-10-25" in text


@pytest.mark.unit
def test_the_window_is_sent_as_dd_mm_yyyy_and_cached_for_a_day():
    with mock.patch(_GET_JSON, return_value=_payload()) as get_json:
        pit.get_insider_transactions("RELIANCE.NS", "2026-10-25")

    assert get_json.call_args.kwargs["params"] == {
        "index": "equities",
        "from_date": "28-04-2026",
        "to_date": "25-10-2026",
        "symbol": "RELIANCE",
    }
    assert get_json.call_args.kwargs["ttl_seconds"] == 24 * 60 * 60


@pytest.mark.unit
def test_the_row_cap_is_fifty_newest_first():
    start = date(2026, 1, 1)
    rows = [
        {
            "acqName": f"ACQ {index}",
            "acqfromDt": (start + timedelta(days=index)).strftime("%d-%b-%Y"),
            "acqtoDt": (start + timedelta(days=index)).strftime("%d-%b-%Y"),
            "intimDt": (start + timedelta(days=index)).strftime("%d-%b-%Y"),
            "afterAcqSharesNo": "1234567",
            "afterAcqSharesPer": "1.23",
            "befAcqSharesNo": "2345678",
            "personCategory": "Promoter",
            "secAcq": "111111",
            "symbol": "RELIANCE",
            "tdpTransactionType": "Buy",
        }
        for index in range(60)
    ]
    with mock.patch(_GET_JSON, return_value={"data": rows, "acqNameList": []}):
        text = pit.get_insider_transactions("RELIANCE.NS", "2026-10-25")

    body = [line for line in text.splitlines() if line.startswith("| ACQ ")]
    assert len(body) == 50
    assert "ACQ 59" in body[0]
    assert all("ACQ 0 " not in line for line in body)


@pytest.mark.unit
def test_no_disclosures_is_one_line_not_an_invented_table():
    with mock.patch(_GET_JSON, return_value={"data": [], "acqNameList": []}):
        text = pit.get_insider_transactions("RELIANCE.NS", "2026-10-25")

    assert "no pit disclosures" in text.lower()
    assert "| Acquirer |" not in text


@pytest.mark.unit
def test_a_non_india_ticker_is_refused_before_any_request():
    with (
        mock.patch(_GET_JSON) as get_json,
        pytest.raises(NoMarketDataError, match="not an NSE/BSE listing"),
    ):
        pit.get_insider_transactions("AAPL", "2026-10-25")

    get_json.assert_not_called()
