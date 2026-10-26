"""Statements as filed with NSE/BSE, read from the XBRL instance itself.

An Indian listed company files its results twice over: a PDF a human reads and
an XBRL instance a machine can. Only the instance carries figures at the vintage
the exchange published, so this module reads it offline and returns exactly the
facts the filer tagged. Nothing is derived: a value is present or the row is
absent, and a filer that reported ``1,284,600,000,000.00`` keeps that number
rather than being rescaled to lakh crore.

The two rules that make Indian instances readable are structural, not cosmetic:

* **Local names, not namespace URIs.** Filings move between taxonomy versions
  (``.../2020-03-31/in-bse-fin``, ``.../2025-01-31/in-capmkt``); the element
  names ``RevenueFromOperations`` and ``ProfitLossForPeriod`` do not move with
  them. Matching the URI would break on the next taxonomy release.
* **Dimensional contexts are dropped whole.** Segment members repeat
  ``SegmentRevenue``, ``OtherExpenses`` and ``SegmentAssets`` once per segment,
  so mixing them into the consolidated totals double-counts. A context with an
  ``explicitMember`` or ``typedMember`` is not a total and is discarded before
  any fact is considered.

One filing also carries several periods ending on the same date (a Q4 quarter
and the full year, a quarter and its year to date). Only the duration separates
them, and the shortest non-dimensional duration is the quarter; an annual
context, when the filer provides one, is the year and brings the balance sheet
and cash flow with it.

No network, no configuration, no logging of values: :func:`parse_instance`
takes the instance bytes and a period end, and returns the rows.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date

# The taxonomy a filing uses is reported as the last path segment of its
# namespace. Older filings use in-bse-fin; 2025 filings use in-capmkt, and a
# bank files under the same taxonomy with a different tag set.
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

# Duration classes in days. The SEBI quarterly template is a calendar quarter,
# so the bands are wide enough for a 28-day February and a 92-day quarter but
# do not let a half-year answer a quarter request.
_DURATION_CLASSES: tuple[tuple[str, int, int], ...] = (
    ("quarter", 60, 115),
    ("half_year", 150, 200),
    ("nine_months", 240, 290),
    ("annual", 300, 400),
)

# Statement rows, each with the tags filers use for it, best first. First match
# wins and values are never summed across tags: a company that tags both a total
# and its components would otherwise be counted twice. Names are pinned against
# tests/fixtures/india/xbrl; where §5.3 of docs/india-parity-plan.md differs, the
# fixture wins and the comment says so.
_INCOME_STATEMENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Revenue", ("RevenueFromOperations",)),
    ("Total Income", ("Income",)),
    ("Interest Earned", ("InterestEarned",)),
    ("Other Income", ("OtherIncome",)),
    ("Total Expenses", ("Expenses",)),
    # Banks do not tag Expenses; this is their nearest filed total, and it is
    # deliberately a separate label rather than being folded into Total Expenses.
    ("Expenses Excluding Provisions and Contingencies",
     ("ExpenditureExcludingProvisionsAndContingencies",)),
    ("Finance Costs", ("FinanceCosts",)),
    ("Interest Expended", ("InterestExpended",)),
    ("Depreciation & Amortisation",
     ("DepreciationDepletionAndAmortisationExpense",)),
    # ProfitBeforeTax is the non-bank tag; banks tag the ordinary-activities
    # line, or the pre-extraordinary-items line.
    ("Profit Before Tax", ("ProfitBeforeTax",
                           "ProfitLossFromOrdinaryActivitiesBeforeTax",
                           "ProfitBeforeExtraordinaryItemsAndTax")),
    ("Tax", ("TaxExpense",)),
    # Banks spell it ProfitLossForThePeriod; the §5.3 map (and non-banks) use
    # ProfitLossForPeriod.
    ("Net Profit", ("ProfitLossForPeriod", "ProfitLossForThePeriod")),
    ("Basic EPS",
     ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
      "BasicEarningsPerShareBeforeExtraordinaryItems",
      "BasicEarningsPerShareAfterExtraordinaryItems")),
    ("Diluted EPS",
     ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
      "DilutedEarningsPerShareBeforeExtraordinaryItems",
      "DilutedEarningsPerShareAfterExtraordinaryItems")),
)

_BALANCE_SHEET: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Total Assets", ("Assets",)),
    ("Non-current Assets", ("NoncurrentAssets",)),
    ("Current Assets", ("CurrentAssets",)),
    ("Cash and Cash Equivalents", ("CashAndCashEquivalents",)),
    # A bank's cash line is balances with the RBI, not cash equivalents, so it
    # keeps its own label instead of being reported as something it is not.
    ("Cash and Balances with Reserve Bank of India",
     ("CashAndBalancesWithReserveBankOfIndia",)),
    ("Current Investments", ("CurrentInvestments",)),
    ("Non-current Investments", ("NoncurrentInvestments",)),
    ("Investments", ("Investments",)),
    ("Advances", ("Advances",)),
    ("Deposits", ("Deposits",)),
    ("Borrowings", ("Borrowings",)),
    ("Borrowings (Current)", ("BorrowingsCurrent",)),
    ("Borrowings (Non-current)", ("BorrowingsNoncurrent",)),
    ("Current Liabilities", ("CurrentLiabilities",)),
    ("Current Financial Liabilities", ("CurrentFinancialLiabilities",)),
    ("Total Liabilities", ("Liabilities",)),
    ("Share Capital", ("EquityShareCapital", "Capital")),
    # Banks have no Equity tag: capital and reserves are filed separately, and
    # adding them here would be a derived figure.
    ("Equity", ("Equity",)),
    ("Total Equity and Liabilities", ("EquityAndLiabilities",)),
    ("Capital and Liabilities", ("CapitalAndLiabilities",)),
)

_CASHFLOW: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Operating Activities", ("CashFlowsFromUsedInOperatingActivities",)),
    ("Investing Activities", ("CashFlowsFromUsedInInvestingActivities",)),
    ("Financing Activities", ("CashFlowsFromUsedInFinancingActivities",)),
)

# The filer's own declaration of basis. Both names appear in Indian filings;
# WhetherConsolidated is a boolean, the other is the words "Consolidated" or
# "Standalone".
_BASIS_DECLARATIONS: tuple[str, ...] = (
    "NatureOfReportStandaloneConsolidated",
    "WhetherConsolidated",
)


@dataclass(frozen=True)
class Row:
    """One filed line item, in absolute units."""

    label: str
    value: float
    unit: str  # "INR", "INR/share", "count", "ratio"


@dataclass(frozen=True)
class ParsedInstance:
    """The statements a single instance states for one period end."""

    basis: str  # "consolidated" | "standalone" | "unknown"
    period_start: date
    period_end: date
    taxonomy: str  # local namespace URI tail, e.g. "in-bse-fin"
    statements: dict[str, list[Row]]  # income_statement | balance_sheet | cashflow


@dataclass(frozen=True)
class _Context:
    id: str
    start: date | None
    end: date
    dimensional: bool


@dataclass(frozen=True)
class _Fact:
    name: str
    context: str
    unit: str | None
    text: str


def _local(tag: str) -> str:
    """The element name without its namespace URI."""
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    """The namespace URI of an element, or an empty string when unqualified."""
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _date(text: str, what: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"XBRL {what} is not an ISO date: {text!r}") from exc


def _parse_contexts(root: ET.Element) -> dict[str, _Context]:
    contexts: dict[str, _Context] = {}
    for element in root.iter():
        if _local(element.tag) != "context":
            continue
        context_id = element.get("id")
        if not context_id:
            continue
        period = next(
            (child for child in element.iter() if _local(child.tag) == "period"),
            None,
        )
        if period is None:
            continue
        start_text = next(
            (child.text for child in period if _local(child.tag) == "startDate"),
            None,
        )
        end_text = next(
            (child.text for child in period if _local(child.tag) == "endDate"),
            None,
        )
        instant_text = next(
            (child.text for child in period if _local(child.tag) == "instant"),
            None,
        )
        if instant_text:
            start = None
            end = _date(instant_text, f"context {context_id} instant")
        elif start_text and end_text:
            start = _date(start_text, f"context {context_id} start")
            end = _date(end_text, f"context {context_id} end")
        else:
            continue
        dimensional = any(
            _local(child.tag) in ("explicitMember", "typedMember")
            for child in element.iter()
        )
        contexts[context_id] = _Context(
            id=context_id, start=start, end=end, dimensional=dimensional
        )
    return contexts


def _unit_kind(unit: ET.Element) -> str | None:
    """Normalise a unit to the four labels a Row may carry.

    A measure the parser cannot place returns ``None``, which makes every fact
    using it ineligible. Skipping is deliberate: converting USD or a lakh-scaled
    unit would invent a value the filer did not state.
    """
    measures = [
        (child.text or "").strip()
        for child in unit.iter()
        if _local(child.tag) == "measure"
    ]
    has_inr = any("INR" in measure for measure in measures)
    has_shares = any("shares" in measure.lower() for measure in measures)
    has_pure = any("pure" in measure.lower() for measure in measures)
    if has_inr:
        return "INR/share" if has_shares else "INR"
    if has_pure:
        return "ratio"
    if has_shares:
        return "count"
    return None


def _parse_units(root: ET.Element) -> dict[str, str | None]:
    units: dict[str, str | None] = {}
    for element in root.iter():
        if _local(element.tag) != "unit":
            continue
        unit_id = element.get("id")
        if unit_id:
            units[unit_id] = _unit_kind(element)
    return units


def _parse_facts(root: ET.Element, units: dict[str, str | None]) -> list[_Fact]:
    facts: list[_Fact] = []
    for element in root:
        context_ref = element.get("contextRef")
        if not context_ref:
            continue
        if element.get(_XSI_NIL, "").lower() in ("true", "1"):
            continue
        text = (element.text or "").strip()
        if not text:
            continue
        unit_ref = element.get("unitRef")
        facts.append(
            _Fact(
                name=_local(element.tag),
                context=context_ref,
                unit=units.get(unit_ref) if unit_ref else None,
                text=text,
            )
        )
    return facts


def _duration_class(start: date, end: date) -> str | None:
    days = (end - start).days
    for name, low, high in _DURATION_CLASSES:
        if low <= days <= high:
            return name
    return None


def _select_context(
    contexts: dict[str, _Context], period_end: date
) -> tuple[_Context, str]:
    """Pick the period this instance reports against ``period_end``.

    A duration is the only thing that separates a quarter from its year to date
    when both end on the same date, so duration is the key. A filer that also
    reports an annual context has filed a full year (the Q4 instance carries
    both the quarter and the year); that year is what the filing is for and it
    is the only case that brings a balance sheet and cash flow. Otherwise the
    shortest non-dimensional duration is the quarter.
    """
    candidates: list[tuple[_Context, str]] = []
    for context in contexts.values():
        if context.dimensional or context.start is None:
            continue
        if context.end != period_end:
            continue
        kind = _duration_class(context.start, context.end)
        if kind is not None:
            candidates.append((context, kind))
    if not candidates:
        raise ValueError(
            f"no non-dimensional context ends on {period_end.isoformat()}"
            " with a quarter, half-year, nine-month or annual duration"
        )

    annual = [item for item in candidates if item[1] == "annual"]
    if annual:
        return max(annual, key=lambda item: (item[0].end - item[0].start).days)
    return min(
        candidates,
        key=lambda item: ((item[0].end - item[0].start).days, item[0].id),
    )


def _fact_for(
    by_name: dict[str, list[_Fact]], candidates: tuple[str, ...], contexts: set[str]
) -> _Fact | None:
    """First tagged candidate that is usable, never a sum of several tags."""
    for name in candidates:
        for fact in by_name.get(name, ()):
            if fact.context not in contexts or fact.unit is None:
                continue
            try:
                value = float(fact.text)
            except ValueError:
                continue
            if math.isfinite(value):
                return fact
    return None


def _rows(
    mapping: tuple[tuple[str, tuple[str, ...]], ...],
    by_name: dict[str, list[_Fact]],
    contexts: set[str],
) -> list[Row]:
    rows: list[Row] = []
    for label, candidates in mapping:
        fact = _fact_for(by_name, candidates, contexts)
        if fact is None or fact.unit is None:
            continue
        rows.append(Row(label=label, value=float(fact.text), unit=fact.unit))
    return rows


def _declared_basis(
    by_name: dict[str, list[_Fact]],
    contexts: dict[str, _Context],
    period_end: date,
) -> str | None:
    """The filer's own consolidated/standalone declaration, if it made one.

    A declaration attached to a non-dimensional context ending on the requested
    date is preferred; other declarations in the same document are a fallback.
    """

    def recognised(fact: _Fact) -> str | None:
        text = fact.text.strip().lower()
        if fact.name == "WhetherConsolidated":
            if text in ("true", "1", "yes"):
                return "consolidated"
            if text in ("false", "0", "no"):
                return "standalone"
            return None
        if "consolidat" in text:
            return "consolidated"
        if "standalone" in text:
            return "standalone"
        return None

    for name in _BASIS_DECLARATIONS:
        facts = list(by_name.get(name, ()))
        facts.sort(
            key=lambda fact: 0
            if fact.context in contexts
            and not contexts[fact.context].dimensional
            and contexts[fact.context].end == period_end
            else 1
        )
        for fact in facts:
            basis = recognised(fact)
            if basis is not None:
                return basis
    return None


def parse_instance(
    xml: bytes, period_end: date, *, consolidated: bool | None = None
) -> ParsedInstance:
    """Read one NSE/BSE XBRL instance for the period ending ``period_end``.

    ``consolidated`` is the caller's request, not a guess: ``True`` means
    consolidated, ``False`` standalone, ``None`` means take the filer's
    declaration. A request that contradicts the declaration raises
    ``ValueError`` naming both, because silently serving the other basis would
    present a different company than the caller asked about.

    Malformed XML raises ``ValueError`` (the input is wrong), not
    ``NoMarketDataError`` (which means a vendor did not answer).
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"XBRL instance is not well-formed XML: {exc}") from exc

    if _local(root.tag) != "xbrl":
        raise ValueError(
            f"not an XBRL instance: root element is {_local(root.tag)!r}"
        )

    contexts = _parse_contexts(root)
    units = _parse_units(root)
    facts = _parse_facts(root, units)

    by_name: dict[str, list[_Fact]] = {}
    for fact in facts:
        by_name.setdefault(fact.name, []).append(fact)

    selected, selected_kind = _select_context(contexts, period_end)

    declared = _declared_basis(by_name, contexts, period_end)
    if consolidated is not None:
        requested = "consolidated" if consolidated else "standalone"
        if declared is not None and declared != requested:
            raise ValueError(
                f"instance declares {declared} results, but consolidated="
                f"{consolidated} requests {requested}"
            )
        basis = requested
    else:
        basis = declared or "unknown"

    income_contexts = {selected.id}
    statements: dict[str, list[Row]] = {
        "income_statement": _rows(_INCOME_STATEMENT, by_name, income_contexts),
        "balance_sheet": {},
        "cashflow": {},
    }

    if selected_kind == "annual":
        instant_contexts = {
            context.id
            for context in contexts.values()
            if not context.dimensional
            and context.start is None
            and context.end == period_end
        }
        statements["balance_sheet"] = _rows(
            _BALANCE_SHEET, by_name, instant_contexts
        )
        statements["cashflow"] = _rows(_CASHFLOW, by_name, income_contexts)

    namespaces = Counter(
        _namespace(element.tag) for element in root if element.get("contextRef")
    )
    taxonomy = "unknown"
    if namespaces:
        namespace = namespaces.most_common(1)[0][0]
        tail = namespace.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            taxonomy = tail

    return ParsedInstance(
        basis=basis,
        period_start=selected.start or period_end,
        period_end=selected.end,
        taxonomy=taxonomy,
        statements=statements,
    )
