"""The caller's book, as the decision agents see it.

Optional input to a run: what is held, at what average price, and how much cash
is free. Without it the agents cannot tell adding to a full position from
opening a new one. Three states are distinct and must stay so: a position, a
flat book, and no context at all, since treating "not provided" as "flat" would
invent a fact about the caller's account.

Broker-neutral by construction: quantities are generic units and the currency is
one of the ISO codes in ``CURRENCIES``, so nothing here implies a venue or an
execution path.

The currency allowlist is deliberate. The renderer compares the book's currency
with the analysed instrument's verified quote currency and must be able to say
when the two differ; a free-text label such as ``"dollars"`` or ``"XYZ"`` would
be mixed into that comparison and into the report without anyone able to reason
about it. Rejecting such a label before the run starts is the only point where
the caller can still fix it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from tradingagents.dataflows.markets import report_currency

# Quote currencies the renderer can reason about. Covers the markets named by
# ``benchmark_map`` so a caller's own book is as wide as the analysis can be;
# ``GBp`` (pence) is not here because no suffix rule can distinguish it from
# ``GBP``, and ``report_currency`` leaves London listings unverified instead.
CURRENCIES = frozenset(
    {
        "USD", "INR", "EUR", "GBP", "JPY", "HKD", "CAD", "AUD",
        "CHF", "SGD", "TWD", "KRW", "CNY", "BRL",
    }
)


class Position(BaseModel):
    ticker: str = Field(description="Instrument symbol, e.g. AAPL")
    quantity: float = Field(description="Signed units held; negative is short")
    average_price: float | None = Field(default=None, description="Average entry price per unit")


class PortfolioContext(BaseModel):
    cash: float | None = Field(default=None, description="Free cash available")
    currency: str | None = Field(default=None, description="ISO code (e.g. USD, INR) for cash and prices")
    positions: list[Position] = Field(default_factory=list)

    @field_validator("currency", mode="before")
    @classmethod
    def _known_currency(cls, value: object) -> str | None:
        """Normalize the label, or reject one the renderer cannot reason about."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"currency must be a string, got {value!r}")
        label = value.strip().upper()
        if not label:
            return None
        if label not in CURRENCIES:
            accepted = ", ".join(sorted(CURRENCIES))
            raise ValueError(f"currency {value!r} is not accepted; expected one of: {accepted}")
        return label

    def position_in(self, ticker: str) -> Position | None:
        return next((p for p in self.positions if p.ticker.upper() == ticker.strip().upper()), None)

    def render(self, ticker: str) -> str:
        """The portfolio block for the decision agents, led by the analyzed instrument."""
        symbol = ticker.strip().upper()
        instrument_currency = report_currency(symbol)
        held = self.position_in(symbol)
        if held is None:
            lines = [f"- No current position in {symbol}"]
        else:
            price = ""
            if held.average_price is not None:
                price = f", average price {held.average_price:,.2f}"
                if instrument_currency:
                    price += f" {instrument_currency}"
            lines = [f"- Current position in {symbol}: {held.quantity:,.4g} units{price}"]
        if self.cash is not None:
            lines.append(f"- Cash available: {self.cash:,.2f}{' ' + self.currency if self.currency else ''}")
        others = [p for p in self.positions if p is not held]
        if others:
            lines.append("- Other positions: " + ", ".join(f"{p.ticker.upper()} {p.quantity:,.4g}" for p in others))
        if self.currency and instrument_currency and self.currency != instrument_currency:
            lines.append(
                f"- Portfolio currency is {self.currency}, but {symbol} is quoted in "
                f"{instrument_currency}: cash and prices above are in their own currencies "
                "and are not converted."
            )
        elif not self.currency and instrument_currency:
            lines.append(
                f"- {symbol} is quoted in {instrument_currency}; the cash currency was not provided."
            )
        return "Portfolio at the analysis date:\n" + "\n".join(lines)

    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]


def load_portfolio(path: str | Path) -> PortfolioContext:
    """Read a portfolio JSON file, failing before the run rather than mid-graph."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return PortfolioContext.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"portfolio file {path} is not usable: {exc}") from exc
