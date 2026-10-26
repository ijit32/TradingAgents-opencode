"""NSE XBRL instance parser: contexts, durations, units and statement rows.

Every test is offline. The fixtures in ``tests/fixtures/india/xbrl`` are
instances exactly as NSE served them (no hand-editing), and the
``.expected.json`` beside each was read from the raw element values, not
produced by the parser. The synthetic instances below cover the cases a live
fixture cannot: a larger dimensional fact, a lying basis declaration, a
non-INR unit and broken XML.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from tradingagents.dataflows.vendors.nse.xbrl import parse_instance

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "india" / "xbrl"

_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.xml"))


def _load(name: str):
    """The instance bytes and its hand-verified expected statements."""
    xml = (FIXTURES / name).read_bytes()
    expected = json.loads(
        (FIXTURES / f"{name.removesuffix('.xml')}.expected.json").read_text()
    )
    return xml, expected


@pytest.mark.parametrize("name", _FIXTURES)
def test_fixture_matches_hand_verified_rows(name):
    xml, expected = _load(name)

    parsed = parse_instance(xml, date.fromisoformat(expected["period_end"]))

    assert parsed.basis == expected["basis"]
    assert parsed.taxonomy == expected["taxonomy"]
    assert parsed.period_start.isoformat() == expected["period_start"]
    assert parsed.period_end.isoformat() == expected["period_end"]
    assert set(parsed.statements) == {
        "income_statement",
        "balance_sheet",
        "cashflow",
    }

    for statement, expected_rows in expected.items():
        if statement in ("basis", "taxonomy", "period_start", "period_end", "units"):
            continue
        rows = parsed.statements[statement]
        assert [row.label for row in rows] == list(expected_rows), statement
        for row in rows:
            want = expected_rows[row.label]
            assert math.isclose(row.value, want, rel_tol=1e-6), (
                f"{statement}/{row.label}: {row.value} != {want}"
            )
            assert row.unit == expected["units"].get(row.label, "INR")


def test_quarter_selects_quarter_not_year_to_date():
    xml, expected = _load("RAIN-2025-06-30-quarter.xml")
    # The instance carries both 2025-04-01..2025-06-30 (90 days) and the
    # 2025-01-01..2025-06-30 year to date (180 days) under the same end date.
    assert b"2025-01-01" in xml

    parsed = parse_instance(xml, date(2025, 6, 30))

    assert parsed.period_start == date(2025, 4, 1)
    assert parsed.period_end == date(2025, 6, 30)
    revenue = {row.label: row.value for row in parsed.statements["income_statement"]}
    assert revenue["Revenue"] == expected["income_statement"]["Revenue"]
    # The half-year figure is nearly twice the quarter's; selecting it would be
    # visible here and would also drag the YTD profit in with it.
    assert revenue["Net Profit"] == expected["income_statement"]["Net Profit"]


@pytest.mark.parametrize(
    "name",
    [
        "RELIANCE-2025-06-30-quarter.xml",
        "TCS-2025-06-30-quarter.xml",
        "RAIN-2025-06-30-quarter.xml",
    ],
)
def test_quarterly_instance_returns_no_balance_sheet(name):
    xml, _ = _load(name)

    parsed = parse_instance(xml, date(2025, 6, 30))

    assert parsed.statements["income_statement"]
    assert parsed.statements["balance_sheet"] == {}
    assert parsed.statements["cashflow"] == {}


def test_annual_returns_balance_sheet_and_cashflow():
    xml, expected = _load("RELIANCE-2025-03-31-annual.xml")

    parsed = parse_instance(xml, date(2025, 3, 31))

    balance_sheet = {row.label: row for row in parsed.statements["balance_sheet"]}
    assert balance_sheet["Total Assets"].value == expected["balance_sheet"]["Total Assets"]
    assert balance_sheet["Equity"].value == expected["balance_sheet"]["Equity"]
    assert parsed.statements["cashflow"]
    assert {row.label for row in parsed.statements["cashflow"]} == {
        "Operating Activities",
        "Investing Activities",
        "Financing Activities",
    }
    # Annual P&L is the year, not the Q4 quarter that shares its end date.
    revenue = {row.label: row.value for row in parsed.statements["income_statement"]}
    assert revenue["Revenue"] == 9801360000000.0


def test_bank_annual_has_no_invented_equity_row():
    xml, expected = _load("HDFCBANK-2025-03-31-annual.xml")

    parsed = parse_instance(xml, date(2025, 3, 31))

    labels = {row.label for row in parsed.statements["balance_sheet"]}
    assert "Total Assets" in labels
    assert "Capital and Liabilities" in labels
    # A bank files capital and reserves separately; the parser must not add
    # them together and call the sum Equity.
    assert "Equity" not in labels
    assert expected["income_statement"]["Net Profit"] == 707922500000.0


def _instance(
    facts: str,
    *,
    basis: str | None = "Consolidated",
    extra_units: str = "",
    dimensional_revenue: str = "",
) -> bytes:
    """A minimal in-capmkt instance with a 90-day context and one instant."""
    declaration = (
        "<in-capmkt:NatureOfReportStandaloneConsolidated "
        f'contextRef="Q">{basis}</in-capmkt:NatureOfReportStandaloneConsolidated>'
        if basis is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"'
        ' xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"'
        ' xmlns:iso4217="http://www.xbrl.org/2003/iso4217"'
        ' xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        '<xbrli:context id="Q"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.bseindia.com/in-capmkt/ScripCode">1'
        "</xbrli:identifier></xbrli:entity><xbrli:period>"
        "<xbrli:startDate>2025-04-01</xbrli:startDate>"
        "<xbrli:endDate>2025-06-30</xbrli:endDate>"
        "</xbrli:period></xbrli:context>"
        '<xbrli:context id="QD"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.bseindia.com/in-capmkt/ScripCode">1'
        "</xbrli:identifier></xbrli:entity><xbrli:period>"
        "<xbrli:startDate>2025-04-01</xbrli:startDate>"
        "<xbrli:endDate>2025-06-30</xbrli:endDate>"
        "</xbrli:period><xbrli:scenario>"
        '<xbrldi:explicitMember dimension="in-capmkt:ReportableSegmentsAxis">'
        "in-capmkt:ReportableSegments1Member"
        "</xbrldi:explicitMember></xbrli:scenario></xbrli:context>"
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure>'
        "</xbrli:unit>"
        f"{extra_units}"
        f"{declaration}"
        f"{facts}"
        f"{dimensional_revenue}"
        "</xbrli:xbrl>"
    ).encode()


def test_dimensional_revenue_is_discarded():
    xml = _instance(
        '<in-capmkt:RevenueFromOperations contextRef="Q" unitRef="INR">'
        "1000000</in-capmkt:RevenueFromOperations>"
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100000</in-capmkt:ProfitLossForPeriod>",
        dimensional_revenue=(
            '<in-capmkt:RevenueFromOperations contextRef="QD" unitRef="INR">'
            "999999999</in-capmkt:RevenueFromOperations>"
        ),
    )

    parsed = parse_instance(xml, date(2025, 6, 30))

    rows = {row.label: row.value for row in parsed.statements["income_statement"]}
    assert rows["Revenue"] == 1000000.0
    assert rows["Net Profit"] == 100000.0


def test_declared_basis_contradicting_argument_raises():
    xml = _instance(
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100</in-capmkt:ProfitLossForPeriod>",
        basis="Standalone",
    )

    with pytest.raises(ValueError, match="standalone.*consolidated"):
        parse_instance(xml, date(2025, 6, 30), consolidated=True)


def test_matching_declaration_is_used_without_argument():
    xml = _instance(
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100</in-capmkt:ProfitLossForPeriod>",
        basis="Standalone",
    )

    parsed = parse_instance(xml, date(2025, 6, 30))

    assert parsed.basis == "standalone"


def test_no_declaration_uses_argument_then_unknown():
    xml = _instance(
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100</in-capmkt:ProfitLossForPeriod>",
        basis=None,
    )

    assert parse_instance(xml, date(2025, 6, 30), consolidated=False).basis == "standalone"
    assert parse_instance(xml, date(2025, 6, 30)).basis == "unknown"


def test_fact_in_non_inr_unit_is_skipped():
    xml = _instance(
        '<in-capmkt:RevenueFromOperations contextRef="Q" unitRef="USD">'
        "5000000</in-capmkt:RevenueFromOperations>"
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100</in-capmkt:ProfitLossForPeriod>",
        extra_units=(
            '<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure>'
            "</xbrli:unit>"
        ),
    )

    parsed = parse_instance(xml, date(2025, 6, 30))

    labels = {row.label for row in parsed.statements["income_statement"]}
    assert "Revenue" not in labels
    assert "Net Profit" in labels


def test_fact_without_unit_is_skipped():
    xml = _instance(
        '<in-capmkt:RevenueFromOperations contextRef="Q">5000000'
        "</in-capmkt:RevenueFromOperations>"
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">'
        "100</in-capmkt:ProfitLossForPeriod>"
    )

    parsed = parse_instance(xml, date(2025, 6, 30))

    labels = {row.label for row in parsed.statements["income_statement"]}
    assert "Revenue" not in labels


def test_malformed_xml_raises_value_error():
    with pytest.raises(ValueError, match="well-formed"):
        parse_instance(b"<xbrli:xbrl", date(2025, 6, 30))


def test_missing_period_raises_value_error():
    xml = _instance(
        '<in-capmkt:ProfitLossForPeriod contextRef="Q" unitRef="INR">100'
        "</in-capmkt:ProfitLossForPeriod>"
    )

    with pytest.raises(ValueError, match="no non-dimensional context"):
        parse_instance(xml, date(2024, 12, 31))
