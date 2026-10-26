# India (NSE and BSE)

A run on an NSE or BSE listing gets prices, technicals, statements as filed, the filing
record, company news, India's investing communities and the India macro series, with every
figure reported in INR. Nothing has to be configured: the exchange suffix is what marks the
market.

This page covers what an India run does and where it stops. Setup, provider keys and the
general CLI are documented in [cli.md](cli.md).

## Tickers and markets

An NSE listing ends in `.NS` (`RELIANCE.NS`, `TCS.NS`) and is the better choice. A BSE
listing ends in `.BO` (`RELIANCE.BO`) and works, but Yahoo's BSE price history is sparse;
when it is too sparse to price honestly, the run says so instead of pricing from a handful
of bars, and names the NSE ticker when it can resolve one. The suffix decides the market, so
a bare symbol stays US: `TCS` is the US Container Store, `TCS.NS` is Tata Consultancy.

Symbols with `&` work: `M&M.NS`, `M&MFIN.NS`, `J&KBANK.NS`. The Python validator and the
plugin's ticker check both accept them, the raw symbol is passed to the vendors, and no
ticker ever reaches a shell.

Quotes, technicals and the company profile come from Yahoo; the profile is present-day, so a
historical run withholds it. Statements, shareholding and the filing record come from NSE,
with XBRL from the integrated filings where the company filed it. This is exchange-published
data and Yahoo prices, not a real-time exchange feed, and nothing here connects to a broker.

## Fundamentals as filed

Statements and shareholding are gated by their broadcast date, so a run dated in the past
reads only what had been published by then; the statement header carries the broadcast
timestamp the filing was selected under. Consolidated statements are the default, and a
filing whose own basis differs is reported with that basis rather than relabelled.

The latest period's P&L is served with the latest annual balance sheet and cash flow where
XBRL exists. Not every filing carries XBRL — a results filing can lack it while a later
integrated filing has it — and a filing with no readable instance is named as such, with no
figures invented in its place. Values are absolute INR as filed, and a later restatement does
not rewrite an earlier filing.

The free structured history begins with the integrated filings index in April 2025; earlier
periods are named as unavailable rather than backfilled. Announcements are searched 90 days
back by default. A BSE-only listing has no reachable NSE filing feed, so a `.BO` run may have
prices and news but names the filing gap.

## News and social

A listing's news pack searches by the issuer's name rather than Yahoo's symbol sample. Google
News RSS is the primary pass, GDELT is a second pass whose reach is 90 days (printed as a
note, never shown as an empty section), and the Economic Times, Moneycontrol and Business
Standard feeds are used for windows that reach today. The same section carries the issuer's
NSE filing record for the window. A source that cannot be reached is named, so a gap never
reads as an absence of news. India news is windowed on IST calendar days: an item published
at 23:30 IST on the trade date is included, and one at 00:15 IST the next morning is not.

The sentiment pack reads India's investing communities (`r/IndiaInvestments`,
`r/IndianStockMarket`, `r/DalalStreetBets`, `r/IndianStreetBets`) and searches Reddit by the
bare NSE symbol. Indian retail discussion is thin, so an empty social block is not read as
indifference. StockTwits does not list NSE/BSE instruments, so it is never called for one:
the section states that the coverage is missing, not neutral.

## Reporting currency and portfolio

An NSE/BSE run is told to report every price, target, stop and position size in INR, never
converted to another currency. A market whose quote currency is unverified gets no currency
claim at all, rather than a guessed one.

A portfolio file names its currency as an ISO code from a short allowlist; `"inr"` is
normalized to `INR`, and an unknown label such as `"dollars"` is rejected before the run
starts. The average price is labelled in the instrument's currency. When the book and the
instrument disagree, the block names both and says they are not converted:

```text
- Current position in RELIANCE.NS: 2 units, average price 1,200.00 INR
- Cash available: 10,000.00 USD
- Portfolio currency is USD, but RELIANCE.NS is quoted in INR: cash and prices above are in
  their own currencies and are not converted.
```

A settled decision names its benchmark's currency too, so an NSE run's lesson reads
"Alpha vs ^NSEI" with "Benchmark ^NSEI is quoted in INR", not as a USD return.

## Macro series

The news pack carries `cpi_india`, `gsec_10y_india`, `usd_inr`, `india_vix` and `nifty_50`,
and its global news searches India-phrased policy and market headlines. The rupee, India VIX
and Nifty/Sensex quotes come from Yahoo, clipped to the analysis date so a historical run
never reads a later close. The four FRED aliases (`cpi_india`, `gsec_10y_india`, `iip_india`,
`real_gdp_india`) need the optional `FRED_API_KEY`; without it they report missing coverage
rather than a flat reading, and the rest of the section still appears. See
[FRED macro data](cli.md#fred-macro-data) for the free key, where to set it, and how to
verify it.

The RBI repo rate is an explicit gap: there is no free point-in-time source wired in, so the
section says the series is not covered instead of printing today's rate.

## Known gaps

- A `.BO`-only listing has prices and news but no filing coverage; `.NS` is preferred.
- Structured statement history starts with the April 2025 integrated filings index.
- The RBI repo rate is not covered.
- StockTwits is not applicable to NSE/BSE instruments.
- FRED's India series need `FRED_API_KEY`; Yahoo serves the currency, VIX and index quotes
  without one.

## OpenCode plugin

India tickers need no plugin configuration and there is no protocol change: `.NS`/`.BO` is
detected from the suffix, and a US ticker behaves as before. See the
[plugin README](../.opencode/plugins/tradingagents/README.md) for its options.
