"""The NSE equity master: BOM header, EQ filter, name joins and the alias map.

The fixture is synthetic — real ISINs are never invented — but the header is
byte-for-byte NSE's, including the BOM, because that is the part of the format a
naive reader gets wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.nse import client, issuer

_FIXTURE = Path(__file__).parent / "fixtures" / "india" / "EQUITY_L_sample.csv"


@pytest.fixture(autouse=True)
def _fake_master(monkeypatch):
    """Serve the fixture through the transport; every test is offline."""
    data = _FIXTURE.read_bytes()
    calls: list[tuple[str, dict]] = []

    def get_bytes(url, **kwargs):
        calls.append((url, kwargs))
        return data

    monkeypatch.setattr(client, "get_bytes", get_bytes)
    return calls


@pytest.mark.unit
def test_the_bom_header_parses_into_the_master(_fake_master):
    table = issuer.issuers()

    assert set(table) == {
        "RELIANCE", "TCS", "M&M", "M&MFIN", "J&KBANK", "ABCETF", "COLLA", "COLLB",
    }
    assert table["RELIANCE"] == issuer.Issuer(
        symbol="RELIANCE",
        name="RELIANCE INDUSTRIES LIMITED",
        isin="INE000A01001",
        series="EQ",
    )
    assert _fake_master[0][1]["ttl_seconds"] == 7 * 24 * 60 * 60


@pytest.mark.unit
def test_the_name_join_only_considers_the_eq_series():
    assert issuer.issuers()["ABCETF"].series == "BE"
    assert issuer.nse_symbol_for_name("ABC Sample ETF") is None


@pytest.mark.unit
def test_a_listing_resolves_to_its_issuer_row():
    assert issuer.issuer_for("RELIANCE.NS") == issuer.Issuer(
        symbol="RELIANCE",
        name="RELIANCE INDUSTRIES LIMITED",
        isin="INE000A01001",
        series="EQ",
    )


@pytest.mark.unit
def test_a_non_india_symbol_does_not_even_read_the_master(_fake_master):
    assert issuer.issuer_for("AAPL") is None
    assert _fake_master == []


@pytest.mark.unit
@pytest.mark.parametrize(("name", "expected"), [
    ("Mahindra and Mahindra Ltd", "M&M"),
    ("MAHINDRA & MAHINDRA LIMITED", "M&M"),
    ("Mahindra & Mahindra Ltd.", "M&M"),
    ("Tata Consultancy Services Ltd", "TCS"),
])
def test_name_matching_folds_case_ampersands_and_suffix_words(name, expected):
    assert issuer.nse_symbol_for_name(name) == expected


@pytest.mark.unit
def test_an_ambiguous_normalised_name_returns_none():
    # COLLA and COLLB differ only by "Company" vs "Co." after normalisation.
    assert issuer.nse_symbol_for_name("Sample Company Limited") is None
    assert issuer.nse_symbol_for_name("Sample Co. Ltd") is None


@pytest.mark.unit
def test_a_configured_alias_overrides_the_name_match():
    set_config({"india_symbol_aliases": {"XYZ.BO": "TCS"}})

    assert issuer.nse_symbol_for_bse("xyz", "Mahindra & Mahindra Limited") == "TCS"
    assert issuer.nse_symbol_for_bse("XYZ.BO") == "TCS"


@pytest.mark.unit
def test_a_bse_symbol_falls_back_to_the_normalised_name():
    set_config({"india_symbol_aliases": {}})

    assert issuer.nse_symbol_for_bse("M&M.BO", "Mahindra & Mahindra Limited") == "M&M"
    assert issuer.nse_symbol_for_bse("UNKNOWN.BO", "Nobody Limited") is None


@pytest.mark.unit
def test_a_malformed_row_is_skipped_not_fatal():
    header = (
        "SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,"
        "PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE"
    )
    text = (
        f"{header}\n"
        "RELIANCE,RELIANCE INDUSTRIES LIMITED,EQ,29-NOV-1995,10,1,INE000A01001,10\n"
        ",BROKEN ROW,EQ\n"
        "JUNK\n"
    )

    assert set(issuer._parse(text)) == {"RELIANCE"}


@pytest.mark.unit
def test_a_missing_master_is_unavailable_not_empty(monkeypatch):
    def fail(url, **kwargs):
        raise VendorUnavailableError("NSE archives returned HTTP 404")

    monkeypatch.setattr(client, "get_bytes", fail)

    with pytest.raises(VendorUnavailableError, match="404"):
        issuer.issuers()
