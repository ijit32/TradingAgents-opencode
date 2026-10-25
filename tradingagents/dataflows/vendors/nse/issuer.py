"""The NSE equity master: which symbol, company name and ISIN belong together.

Yahoo tells a run what it traded, not who the issuer is. A ``.BO`` quote carries
a BSE scrip code and no ISIN, and the NSE filing feeds are keyed by NSE symbol,
so a BSE-only run needs a way back to the filing issuer — or an honest "no
match". ``EQUITY_L.csv`` is NSE's own master (about 2 600 rows: symbol, company
name, series, ISIN) and is the only public source that joins them all.

The join is deliberately conservative. A name is matched only when it is unique
after normalisation; a company that resolves to two EQ scrips resolves to
neither, because attaching another issuer's filings to a report is a worse error
than reporting no filings at all. Dual-listed pairs whose names genuinely differ
are handled by the explicit ``india_symbol_aliases`` config map, never by a
fuzzy guess.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from tradingagents.dataflows import markets
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError
from tradingagents.dataflows.vendors.nse import client

_URL = f"{client.ARCHIVES_BASE}/content/equities/EQUITY_L.csv"
_NAMESPACE = "issuer"
_CACHE_KEY = "EQUITY_L"

# The master changes when a company lists or delists, not hourly; a week of
# cache keeps a backtest at one download per week.
_TTL_SECONDS = 7 * 24 * 60 * 60

# Punctuation that never carries meaning in an issuer's name. Removed rather
# than normalised to a space, so the operation is idempotent either way.
_DROPPED = str.maketrans("", "", ".,'\"()-/")

# The suffix words Indian companies vary most: the same issuer files as
# "Mahindra & Mahindra Limited" on one feed and "... Ltd" on another.
_WORD_ALIASES = {"limited": "ltd", "corporation": "corp", "company": "co"}


@dataclass(frozen=True)
class Issuer:
    symbol: str
    name: str
    isin: str
    series: str


def _normalise_name(name: str) -> str:
    """A comparison form: case, ``&``/``and``, suffix words and punctuation folded.

    ``"Mahindra & Mahindra Limited"`` and ``"Mahindra and Mahindra Ltd"`` both
    read ``mahindra and mahindra ltd``.
    """
    text = name.casefold().replace("&", " and ").translate(_DROPPED)
    return " ".join(_WORD_ALIASES.get(word, word) for word in text.split())


def _parse(text: str) -> dict[str, Issuer]:
    """Parse the master, skipping a row that is not usable instead of the file.

    NSE has occasionally shipped a row with a missing field; one bad line must
    not cost the run every other issuer.
    """
    table: dict[str, Issuer] = {}
    for row in csv.DictReader(io.StringIO(text)):
        fields = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        symbol = fields.get("SYMBOL", "").upper()
        name = fields.get("NAME OF COMPANY", "")
        if not symbol or not name:
            continue
        table[symbol] = Issuer(
            symbol=symbol,
            name=name,
            isin=fields.get("ISIN NUMBER", ""),
            series=fields.get("SERIES", "").upper(),
        )
    return table


def issuers() -> dict[str, Issuer]:
    """The master keyed by upper-case NSE symbol."""
    data = client.get_bytes(
        _URL, bucket="archives", ttl_seconds=_TTL_SECONDS,
        namespace=_NAMESPACE, key=_CACHE_KEY,
    )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VendorUnavailableError("NSE equity master is not UTF-8") from exc
    return _parse(text)


def issuer_for(symbol: str) -> Issuer | None:
    """The issuer a listing belongs to, or None for a non-India or unknown symbol.

    The market check comes first: a US ticker has no business asking NSE who it
    is, and ``TCS`` must stay the Container Store rather than resolve to Tata
    Consultancy.
    """
    if not markets.is_india(symbol):
        return None
    return issuers().get(markets.india_ticker(symbol).upper())


def nse_symbol_for_name(name: str) -> str | None:
    """The NSE symbol of the unique ``EQ`` issuer whose name normalises to ``name``.

    Ambiguity returns None. Several scrips can carry one company's name (a
    demerged entity, a duplicate listing), and attaching the wrong one's filings
    to a report is worse than attaching none.
    """
    wanted = _normalise_name(name)
    if not wanted:
        return None
    matches = [
        issuer.symbol for issuer in issuers().values()
        if issuer.series == "EQ" and _normalise_name(issuer.name) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _bare_nse_symbol(value: str) -> str:
    symbol = value.strip().upper()
    for suffix in (".NS", ".BO"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol


def nse_symbol_for_bse(bse_symbol: str, name: str = "") -> str | None:
    """The NSE symbol for a BSE scrip, by configured alias first, then by name.

    Yahoo exposes no ISIN for a ``.BO`` quote, so an override map is the only
    way to join a pair whose names differ ("Bajaj Auto" vs "Bajaj Auto Ltd" is
    still a name match; a renamed entity is not). Keys may carry ``.BO`` and the
    lookup is case-insensitive.
    """
    from tradingagents.dataflows.config import get_config

    bare = _bare_nse_symbol(bse_symbol)
    aliases = get_config().get("india_symbol_aliases") or {}
    for key, value in aliases.items():
        if _bare_nse_symbol(str(key)) == bare and str(value).strip():
            return _bare_nse_symbol(str(value))
    if not name.strip():
        return None
    return nse_symbol_for_name(name)


def nse_symbol_hint(symbol: str) -> str:
    """A "; NSE lists 'X.NS'" sentence for a BSE symbol, or ``""``.

    A sparse BSE tape is often a dual-listed scrip whose NSE line is complete,
    so the failure detail names the ticker that would work instead of leaving
    the reader to guess. It is a hint, never a substitution: the BSE and NSE
    tapes are different markets.

    Best effort by design. This runs only after a gap was already found, and a
    lookup that fails (NSE throttled, master unreadable, config broken) must
    leave the caller's error exactly as it was rather than replace it with a
    different one, so failures become "no hint".
    """
    try:
        issuer = issuer_for(f"{markets.india_ticker(symbol)}.NS")
    except VendorError:
        return ""
    except Exception:  # noqa: BLE001 - a hint must never mask the real error
        return ""
    if issuer is None:
        return ""
    # The symbol the master actually lists, not the one we guessed: the hint is
    # only useful if the reader can paste it into the next request.
    return (
        f"; NSE lists {issuer.symbol + '.NS'!r} for the same symbol — "
        "request that ticker for full history"
    )
