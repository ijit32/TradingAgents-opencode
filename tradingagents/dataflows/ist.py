"""NSE timestamps are IST, while every comparison in this repo is UTC.

Exchange announcements and filings carry a wall-clock time in India Standard
Time with no offset (``"24-Oct-2026 21:48:04"``), and the point-in-time rules
compare aware UTC instants (``date_window.in_window``). Parsing them here keeps
one conversion rule for every India vendor, and makes the published day the IST
calendar day rather than whatever UTC happens to say at the time — a 00:15 IST
filing belongs to the day India calls it, not to the previous UTC date.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE writes the month as an English three-letter abbreviation in any case
# ("Oct", "OCT"). ``%b`` would follow the process locale instead, so the names
# are spelled out and a run on a non-English host still reads the same feed.
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# The two date layouts NSE returns, each with an optional clock separated by a
# space or ``T``; a date with no clock is midnight IST.
_DMY = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:[ T](\d{1,2}):(\d{2}):(\d{2}))?")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}):(\d{2}))?")


def _clock(hour: str | None, minute: str | None, second: str | None) -> tuple[int, int, int]:
    """Hours, minutes and seconds; a date-only value is midnight."""
    return int(hour or 0), int(minute or 0), int(second or 0)


def _parts(value: str) -> tuple[int, int, int, int, int, int] | None:
    """Year, month, day, hour, minute, second for a timestamp NSE emits, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    # A trailing Z marks the same instant and carries no offset of its own.
    text = text[:-1].strip() if text.endswith("Z") else text
    if dmy := _DMY.fullmatch(text):
        month = _MONTHS.get(dmy.group(2).upper())
        if month is None:
            return None
        return int(dmy.group(3)), month, int(dmy.group(1)), *_clock(*dmy.group(4, 5, 6))
    if iso := _ISO.fullmatch(text):
        return int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), *_clock(*iso.group(4, 5, 6))
    return None


def parse_ist(value: str) -> datetime:
    """Parse an exchange timestamp as an aware UTC datetime.

    Accepts the formats NSE returns in practice: ``24-Oct-2026 21:48:04``
    (month name in any case), ``2026-10-24 21:48:04``, ``2026-10-24T21:48:04``
    and the date alone (midnight IST). Surrounding whitespace and a trailing
    ``Z`` are tolerated. Anything else raises ValueError naming the value, so a
    new layout is reported rather than quietly read as the wrong day.
    """
    parts = _parts(value)
    if parts is None:
        raise ValueError(
            f"unrecognised IST timestamp {value!r}; expected e.g. '24-Oct-2026 21:48:04'"
        )
    try:
        wall_clock = datetime(*parts, tzinfo=IST)
    except ValueError as exc:  # a date that does not exist, e.g. 31-Feb
        raise ValueError(f"invalid IST timestamp {value!r}: {exc}") from exc
    return wall_clock.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    """``dt`` in IST; a naive value is assumed to be UTC, as everywhere else here."""
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(IST)


def ist_date(value: str) -> date:
    """The IST calendar day an exchange timestamp falls on — not its UTC day."""
    return parse_ist(value).astimezone(IST).date()


def today_ist() -> date:
    """Today in IST.

    ``date.today()`` follows the host's time zone, which may be UTC or anything
    else, so it would name the wrong day for a run in the Indian evening.
    """
    return datetime.now(IST).date()
