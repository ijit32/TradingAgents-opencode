"""The IST calendar day is the unit an Indian filing is published in.

A filing at 23:30 IST on the run date is known on that date; one at 00:15 IST
the next morning is not. A window bounded by UTC midnights says the opposite at
both edges, which is the regression ``in_window_ist`` exists to close.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tradingagents.dataflows.date_window import in_window, in_window_ist
from tradingagents.dataflows.ist import IST, ist_date, parse_ist, to_ist, today_ist


@pytest.mark.unit
def test_a_late_evening_filing_on_the_end_date_is_included():
    pub = datetime(2026, 10, 24, 23, 30, tzinfo=IST)
    assert in_window_ist(pub, "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_an_early_morning_filing_the_next_day_is_excluded():
    pub = datetime(2026, 10, 25, 0, 15, tzinfo=IST)
    assert not in_window_ist(pub, "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_the_start_day_is_included():
    assert in_window_ist(datetime(2026, 10, 20, 0, 0, tzinfo=IST), "2026-10-20", "2026-10-24")
    assert in_window_ist(datetime(2026, 10, 20, 23, 30, tzinfo=IST), "2026-10-20", "2026-10-24")
    assert not in_window_ist(datetime(2026, 10, 19, 23, 59, tzinfo=IST), "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_a_utc_timestamp_is_converted_not_truncated():
    """18:45 UTC on the 24th is 00:15 IST on the 25th: a different day, excluded."""
    pub = datetime(2026, 10, 24, 18, 45, tzinfo=UTC)
    assert not in_window_ist(pub, "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_a_naive_timestamp_is_read_as_utc():
    assert not in_window_ist(datetime(2026, 10, 24, 18, 45), "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_the_utc_bounded_window_misses_the_wrong_edges():
    """The regression: UTC midnights are five and a half hours off the exchange day."""
    start = datetime(2026, 10, 20, tzinfo=UTC)
    end = datetime(2026, 10, 24, tzinfo=UTC)
    early_on_the_start_day = datetime(2026, 10, 20, 0, 15, tzinfo=IST)  # 18:45 UTC on the 19th
    next_day = datetime(2026, 10, 25, 0, 15, tzinfo=IST)                 # 18:45 UTC on the 24th

    assert not in_window(early_on_the_start_day, start, end)
    assert in_window(next_day, start, end)
    assert in_window_ist(early_on_the_start_day, "2026-10-20", "2026-10-24")
    assert not in_window_ist(next_day, "2026-10-20", "2026-10-24")


@pytest.mark.unit
def test_an_undated_item_is_kept_when_the_window_reaches_today():
    today = today_ist()
    assert in_window_ist(None, (today - timedelta(days=6)).isoformat(), today.isoformat())


@pytest.mark.unit
def test_an_undated_item_is_dropped_when_the_window_ends_before_today():
    today = today_ist()
    start = (today - timedelta(days=7)).isoformat()
    assert not in_window_ist(None, start, (today - timedelta(days=1)).isoformat())


@pytest.mark.unit
def test_a_past_trade_date_never_keeps_an_undated_item():
    assert not in_window_ist(None, "2025-01-01", "2025-01-31")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24-Oct-2026 21:48:04", datetime(2026, 10, 24, 16, 18, 4, tzinfo=UTC)),
        ("21-OCT-2026 16:25:15", datetime(2026, 10, 21, 10, 55, 15, tzinfo=UTC)),
        (" 24-Oct-2026 21:48:04Z ", datetime(2026, 10, 24, 16, 18, 4, tzinfo=UTC)),
        ("2026-10-24 21:48:04", datetime(2026, 10, 24, 16, 18, 4, tzinfo=UTC)),
        ("2026-10-24T21:48:04", datetime(2026, 10, 24, 16, 18, 4, tzinfo=UTC)),
        ("24-Oct-2026", datetime(2026, 10, 23, 18, 30, tzinfo=UTC)),
    ],
)
def test_parse_ist_accepts_the_formats_nse_returns(value, expected):
    assert parse_ist(value) == expected


@pytest.mark.unit
def test_parse_ist_names_the_offending_value():
    with pytest.raises(ValueError, match="24/10/2026"):
        parse_ist("24/10/2026")


@pytest.mark.unit
def test_ist_date_is_the_ist_day_not_the_utc_day():
    """00:15 IST on the 24th is still the 23rd in UTC; the IST date is the one to gate on."""
    assert ist_date("2026-10-24 00:15:00") == date(2026, 10, 24)
    assert parse_ist("2026-10-24 00:15:00").date() == date(2026, 10, 23)


@pytest.mark.unit
def test_to_ist_reads_a_naive_value_as_utc():
    assert to_ist(datetime(2026, 10, 24, 16, 0)) == datetime(2026, 10, 24, 21, 30, tzinfo=IST)


@pytest.mark.unit
def test_today_ist_is_the_date_in_kolkata():
    assert today_ist() == to_ist(datetime.now(UTC)).date()
