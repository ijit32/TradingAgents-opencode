"""Which market a symbol belongs to, decided by its exchange suffix alone.

The suffix is the only input an India-aware path needs: ``.NS`` and ``.BO`` are
India, a bare symbol is US (``TCS`` is the Container Store, ``TCS.NS`` is Tata
Consultancy), and any other suffix is a market this package has no India
handling for. Resolving it in one pure function keeps suffix knowledge in two
places at most — here and ``benchmark_map`` — instead of at every vendor's
front door, and means a run never has to be told which market it is analysing.
"""

from __future__ import annotations

from typing import Literal

Market = Literal["US", "IN", "OTHER"]

# Yahoo's suffixes for the two Indian exchanges: NSE and BSE.
INDIA_SUFFIXES = (".NS", ".BO")

# Indian indices carry no exchange suffix, so they are named here: quotes, the
# benchmark and the macro pack all treat them exactly as they treat a listing.
INDIA_INDEX_SYMBOLS = frozenset(
    {
        "^NSEI", "^BSESN", "^INDIAVIX",
        "^CNXIT", "^CNXFIN", "^CNXAUTO",
        "^CNXPHARMA", "^CNXMETAL",
    }
)


def _canonical(symbol: str) -> str:
    """The symbol as the exchange writes it: no surrounding space, upper case."""
    return symbol.strip().upper()


def market_for(symbol: str) -> Market:
    """The market ``symbol`` trades in: ``"IN"``, ``"US"`` or ``"OTHER"``.

    Only a symbol with no dot is US, mirroring the empty-suffix entry in
    ``benchmark_map``. A suffix this module does not model is ``"OTHER"``
    rather than US, so a ``.L`` or ``.T`` listing is never handed to an India
    vendor by accident — and ``BRK.B``, a US stock whose dot is part of its
    ticker, lands there too instead of being mistaken for a foreign listing.
    """
    s = _canonical(symbol)
    if s in INDIA_INDEX_SYMBOLS or s.endswith(INDIA_SUFFIXES):
        return "IN"
    return "US" if "." not in s else "OTHER"


def is_india(symbol: str) -> bool:
    """Whether ``symbol`` is an NSE/BSE listing or an Indian index."""
    return market_for(symbol) == "IN"


def india_ticker(symbol: str) -> str:
    """The bare NSE/BSE symbol a filing is keyed by: ``RELIANCE.NS`` -> ``RELIANCE``.

    An index is already bare and is returned unchanged. Raises ValueError for
    anything that is not India: a vendor that queried the NSE with a US ticker
    would be answered about another company, or not at all.
    """
    s = _canonical(symbol)
    if not is_india(s):
        raise ValueError(f"{symbol!r} is not an NSE/BSE listing")
    for suffix in INDIA_SUFFIXES:
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def india_exchange(symbol: str) -> str:
    """``"NSE"`` for a ``.NS`` symbol, ``"BSE"`` for ``.BO``, ``""`` otherwise."""
    s = _canonical(symbol)
    if s.endswith(".NS"):
        return "NSE"
    return "BSE" if s.endswith(".BO") else ""


def report_currency(symbol: str) -> str:
    """The ISO code a report labels ``symbol``'s figures with, or ``""``.

    ``""`` means the quote currency has not been verified, and the caller omits
    the currency line rather than print a wrong one: London-listed lines quote
    in GBp (pence) as often as in GBP, and no suffix rule can tell which. Only
    the two markets this package models are mapped — INR for NSE/BSE listings
    and Indian indices, USD for a symbol with no suffix.
    """
    s = _canonical(symbol)
    if is_india(s):
        return "INR"
    return "USD" if "." not in s else ""
