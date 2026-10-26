# India (NSE/BSE) parity — implementation plan

Status: in progress. Phases 0, 1, 2a, 2c, 3, 4 and 5 have shipped; 2b is deferred
behind the Phase 2a gate and §13; Phases 6–7 remain.

Scope: make a run on an NSE or BSE listing as complete as a US run — prices,
technicals, fundamentals as filed, company news, exchange filings, social
sentiment, macro, memory/backtest settlement, and INR reporting — without
changing US behaviour or the OpenCode plugin protocol.

Non-goals: broker execution, real-time streaming quotes, options/F&O chains,
mutual funds, PDF statement extraction, and any adapter that bypasses a
vendor's access controls or impersonates a browser identity to defeat blocking.

---

## 1. Measured baseline (2026-10-25)

Verified against this checkout and the live endpoints from this machine. The
list separates what already works from what has to be built, and from what the
exchange simply does not publish.

| Layer | Today | Evidence |
|---|---|---|
| OHLCV `.NS` | works | `RELIANCE.NS`, `RELIANCE.BO`, `TCS.NS`, `HDFCBANK.NS` returned rows |
| OHLCV `.BO` | works, sometimes sparse | `RELIANCE.BO` 199 rows Jan–Oct 2026, 1 row 1–24 Oct |
| Technicals | works | computed from the same closes |
| Live profile fundamentals | works, INR | `RELIANCE.NS` → Reliance Industries, Energy, market cap `16103642234880 INR` |
| Live statements | works, INR | `get_income_statement` returned rows for today's date |
| Historical statements | withheld | point-in-time guard: "Income Statement data is withheld for this date" |
| Historical profile | withheld | by design (`withhold_live_profile`) |
| Company news | unavailable | `DATA_UNAVAILABLE` for `RELIANCE.NS`, `.BO`, `TCS.NS`, `INFY.NS`; `AAPL` fine |
| Global news | US/EU queries only | `default_config.global_news_queries` |
| Macro pack | US-only | `collect.MACRO_INDICATORS` |
| Social | US-only | Reddit defaults; StockTwits 404 for `.NS`, and bare `TCS` hits the US Container Store |
| Alpha benchmark | works | `.NS` → `^NSEI`, `.BO` → `^BSESN` |
| `&` tickers | rejected | `M&M.NS`, `M&MFIN.NS`, `J&KBANK.NS` fail `safe_ticker_component`; yfinance itself serves them |

Endpoint probes the plan depends on:

| Endpoint | Result |
|---|---|
| `nseindia.com/api/corporate-announcements?index=equities&from_date=…&to_date=…&symbol=…` | 200, JSON, no cookies from this host; `RELIANCE` 135 rows Jan–Oct 2026; 5 rows Jan 2010. Universal, symbol-scoped, date-bounded |
| `nseindia.com/api/integrated-filing-results?index=equities&from_date=…&to_date=…&size=…&page=…` | 200, `{data,size,page,totalCount}`; current results route (period ended Mar 2025 onward); `broadcast_Date`, `qe_Date`, `xbrl`, `ixbrl`, `pdf_attach`, `type` |
| `nseindia.com/api/corporates-financial-results?…` | 200 but **legacy**: date params do not bound the result, no date window returns 29 rows, and the newest `RELIANCE` row was Q3 FY25 even in an Oct 2026 query |
| `nseindia.com/api/corporates-pit?index=equities&from_date=…&to_date=…&symbol=…` | 200, `intimDt`, `acqfromDt`, `tdpTransactionType`, `secAcq` |
| `nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=…` | 200, quarterly rows 2021→ with `broadcastDate` |
| `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | 200, 2 614 rows, symbol/name/ISIN master |
| `nsearchives.nseindia.com/corporate/xbrl/INDAS_*.xml` | 200, taxonomy `in-bse-fin`; quarterly = P&L + segment + ratios, annual = + balance sheet + cash flow |
| `api.bseindia.com/BseIndiaAPI/api/AnnGetData/w` | **403 Access Denied** (Akamai) from this host |
| `news.google.com/rss/search?q=…&hl=en-IN&gl=IN&ceid=IN:en` | 200; `after:`/`before:` returned 39 dated items for Jan 2025; 100-item cap |
| `api.gdeltproject.org/api/v2/doc/doc` | 429 above 1 request / 5 s; DOC 2.0 covers a rolling 3 months only |
| Publisher RSS (ET markets, Moneycontrol, Business Standard) | 200, live items only |
| `api.stlouisfed.org` | reachable (400 without key); `fred.stlouisfed.org` HTML timed out from this host |
| Yahoo `INR=X`, `^INDIAVIX`, `^NSEI`, `^BSESN`, `^CNXIT` | 200 via yfinance |

Three findings that shape the design:

1. **The filings backbone is NSE, not BSE.** BSE's JSON API is blocked from this
   network, and BSE-only scrips have no public filing feed we can reach.
2. **Not every results filing is structured.** `RELIANCE` filed FY25 results on
   2025-04-25 20:05 IST as announcement "Outcome of Board Meeting" with two PDFs
   and `hasXbrl: false`; the results index has no Financials row for it. XBRL
   coverage is real but uneven, including among large caps.
3. **FRED's India series are unverified** — the FRED website is unreachable from
   this host and no key is configured. Phase 5 starts with a probe script, not a
   hard-coded series list.

---

## 2. Design pillars

1. **Auto-detect, never ask.** The suffix decides the market: `.NS` and `.BO`
   are India. No `--market` flag. A bare symbol stays US (`TCS` is the Container
   Store, `TCS.NS` is Tata Consultancy).
2. **One chain, self-gating vendors.** Every India vendor is registered in the
   existing `VENDOR_METHODS` map and joins the configured chain. It fails fast
   with `NoMarketDataError` for a non-India ticker, exactly as `sec_edgar` does
   for a non-US filer ("not a US SEC filer"). No router rewrite.
3. **Publication date or nothing.** A figure is served only if a filing that
   carried it was public on or before `trade_date`. Period-end dates are never
   treated as publication dates.
4. **Unavailable is a value, not an error.** A missing feed returns the existing
   `DATA_UNAVAILABLE` / `NO_DATA_AVAILABLE` sentinels or a `coverage_gap`
   notice. It never returns an empty list that reads as "nothing happened".
5. **US path is untouched.** Same chains, same functions, same tests. India is
   additive: new keys, new vendors, new branches guarded by the suffix.

---

## 3. Architecture

### 3.1 Market resolution

New module `tradingagents/dataflows/markets.py`:

```python
Market = Literal["US", "IN", "OTHER"]

def market_for(symbol: str) -> Market        # ".NS"/".BO" -> "IN"; no suffix -> "US"; else "OTHER"
def is_india(symbol: str) -> bool
def india_ticker(symbol: str) -> str         # "RELIANCE.NS" -> "RELIANCE"
def india_exchange(symbol: str) -> str       # "NSE" | "BSE"
def report_currency(symbol: str) -> str      # "INR" | "USD" | ... (suffix map, defaults USD)
```

`normalize_symbol` stays as is. `markets.py` is pure syntax, no network, and is
the only place suffix knowledge lives outside `benchmark_map`.

### 3.2 Vendor registration

`tradingagents/dataflows/router.py` gains entries only:

```python
VENDOR_METHODS["get_income_statement"]["nse"]         = nse.get_income_statement
VENDOR_METHODS["get_balance_sheet"]["nse"]            = nse.get_balance_sheet
VENDOR_METHODS["get_cashflow"]["nse"]                 = nse.get_cashflow
VENDOR_METHODS["get_fundamentals"]["nse"]             = nse.get_fundamentals
VENDOR_METHODS["get_insider_transactions"]["nse"]     = nse.get_insider_transactions
VENDOR_METHODS["get_news"]["nse"]                     = india_news.get_news
VENDOR_METHODS["get_macro_indicators"]["india_macro"] = india_macro.get_macro_data
```

Default chains in `default_config.py`:

```python
"data_vendors": {
    "core_stock_apis":      "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data":     "sec_edgar,nse,yfinance",
    "news_data":            "nse,yfinance",
    "macro_data":           "india_macro,fred",
    "prediction_markets":   "polymarket",
},
```

The router already filters a chain down to vendors that implement a given method
(`vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]`), so one
shared list works across `get_news` / `get_global_news` /
`get_insider_transactions`. US runs are unchanged: `sec_edgar` still answers
first, NSE declines a US ticker, and Yahoo serves it as before.

`macro_data` is ordered `india_macro,fred` because FRED answers an alias it does
not know with a guidance string rather than an exception; putting FRED first
would let that string stop the chain before the India alias reached its vendor.
`india_macro` raises `VendorDeclinedError` for every non-India alias, which is a
"not mine" signal, not a no-data verdict, so FRED's missing key or output stays
visible for a US request.

Failure mapping every India vendor must follow:

| Situation | Raise |
|---|---|
| ticker is not `.NS`/`.BO` | `NoMarketDataError(ticker, ticker, "not an NSE/BSE listing")` |
| symbol unknown to the exchange master | `NoMarketDataError(..., "symbol not in the NSE equity master")` |
| no filing in the requested window / by the date | `NoMarketDataError(..., "no X filed by YYYY-MM-DD")` |
| filing exists but carries no structured figures | `NoMarketDataError(..., "filed on YYYY-MM-DD; no structured XBRL")` |
| HTTP 403/429/5xx/timeout, malformed payload | `VendorUnavailableError("NSE announcements request failed (429)")` |

That split is what makes the router's "vendor was the problem, not the
instrument" logic work unchanged.

### 3.3 Point-in-time contract

Every India datum carries a publication timestamp in **IST**. Four rules:

1. **Publication date, not period end.** `an_dt` for announcements,
   `broadcast_Date` for results, `intimDt` for insider trades, `broadcastDate`
   for shareholding. A datum is served when that date is **on or before**
   `trade_date`, inclusive and at date granularity — the same rule `sec_edgar`
   already applies ("filed on or before `as_of_date`"). A filing published at
   20:05 IST on `trade_date` is served; one published the next morning is not.
   Intraday cutoffs are not modelled; that is one deliberate rule, not two.
2. **The window is an IST calendar day.** `date_window.in_window` compares
   instants against `end + 1 day` at UTC midnight, which is 05:30 IST the next
   day. Indian filings cluster between 18:00 and 23:30 IST, so a UTC-bound
   window drops same-day evening filings and can admit the next day's early
   ones. Add:

   ```python
   def in_window_ist(pub_dt, start_date: str, end_date: str) -> bool
   ```

   implemented as `[start 00:00 IST, end+1 00:00 IST)`, used in every India path.
   Undated items are dropped in historical runs and kept only when the window
   reaches the present, matching `in_window`.
3. **A filing is a fact at a vintage.** Restatements are served as filed then,
   never as later corrected.
4. **No derived figures.** A fourth quarter is not computed by subtracting three
   filed quarters; if no filing states it, it is unavailable — the rule
   `sec_edgar` already applies.

### 3.4 Caching, throttling, failure budget

New module `tradingagents/dataflows/vendors/nse/client.py`:

- One `requests.Session`, a `User-Agent` that names the project and version (the
  pattern `sec_edgar._user_agent()` uses), no browser spoofing, no cookie
  harvesting.
- Token bucket: ≥2 s between calls to `www.nseindia.com`, ≥1 s to
  `nsearchives.nseindia.com`; `Retry-After` honoured on 429; one retry.
- Cache under `data_cache_dir/nse/…` through `files.replace_file` (atomic):
  | Payload | TTL |
  |---|---|
  | announcements by (symbol, window) | 24 h |
  | integrated filing index by (window, page) | 7 d |
  | XBRL instance by URL | 30 d (URLs are immutable) |
  | PIT by (symbol, window) | 24 h |
  | shareholding by symbol | 24 h |
  | equity master CSV | 7 d |
- Process-local circuit breaker: 3 consecutive transport failures → the vendor
  reports unavailable for 10 minutes, so a 60-cell backtest cannot turn a block
  into 60 timeouts.

---

## 4. File map

New Python package:

```
tradingagents/dataflows/markets.py
tradingagents/dataflows/ist.py                 # "24-Oct-2026 21:48:04" -> aware UTC
tradingagents/dataflows/vendors/nse/
    __init__.py        # the six entry points the router imports
    client.py          # session, UA, throttle, cache, error mapping
    issuer.py          # EQUITY_L.csv master: symbol <-> name <-> ISIN
    announcements.py   # corporate-announcements API (the universal index)
    filings.py         # integrated-filing-results, paginated
    xbrl.py            # instance parser + statement row maps
    pit.py             # SEBI PIT insider trades
    shareholding.py    # quarterly shareholding pattern
tradingagents/dataflows/vendors/india_news/
    __init__.py
    google.py          # date-ranged Google News RSS
    gdelt.py           # DOC 2.0, 3-month window, throttled
    publishers.py      # ET / Moneycontrol / Business Standard RSS (live only)
    query.py           # company-name + symbol query builder, dedup
tradingagents/dataflows/vendors/india_macro.py
scripts/probe_india_sources.py                 # dev aid, not shipped
```

Modified:

```
tradingagents/dataflows/symbols.py            # allow '&' in ticker path component
tradingagents/default_config.py               # chains, India macro list, India news queries, aliases
tradingagents/dataflows/vendors/reddit.py     # India subreddits
tradingagents/dataflows/vendors/stocktwits.py # skip India cleanly
tradingagents/dataflows/vendors/yahoo/news.py # India branch for global-news queries
tradingagents/agents/tools.py                 # get_global_news(ticker=...) passthrough
tradingagents/agents/context.py               # quote-currency line for non-USD instruments
tradingagents/opencode/collect.py             # market-aware macro pack + section titles
tradingagents/portfolio/*                     # accept INR as a portfolio currency
.opencode/plugins/tradingagents/runtime.ts    # allow '&' in ticker regex
.opencode/plugins/tradingagents/sweep.ts      # same
docs/cli.md
CHANGELOG.md
tests/…                                       # see §10
```

No new third-party dependency: XML via `xml.etree.ElementTree`, CSV via `csv`,
RSS via `xml.etree`, HTTP via the existing `requests`. Nothing to add to
`pyproject.toml`.

---

## 5. Source contracts

### 5.1 Announcements — the universal index

```
GET https://www.nseindia.com/api/corporate-announcements
    ?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&symbol=<SYMBOL>
```

Array of objects; the fields that matter:

| Field | Use |
|---|---|
| `symbol` | join key; prefer over `sm_name` |
| `sm_name` | issuer name for the header |
| `desc` | subject ("Outcome of Board Meeting", "Credit Rating", "General Updates", …) |
| `an_dt` | announcement datetime, IST — the point-in-time key |
| `attchmntFile` | PDF URL, cited in the report |
| `hasXbrl` | whether a structured filing exists |

Depth verified to 2010, symbol-scoped, so an empty window is a real absence for
that issuer. This is the one feed that covers **every** issuer, so it is both
the events feed and the pointer that tells the fundamentals path whether a
structured filing exists.

### 5.2 Integrated filing results — current structured route

```
GET https://www.nseindia.com/api/integrated-filing-results
    ?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&size=<n>&page=<0-based>
```

Paginated: `{"data": [...], "size": n, "page": p, "totalCount": N}`. One month of
results is ~7 000 rows, so the vendor asks for `size=2000` and walks pages, or
narrows the window to a filing season. Fields:

| Field | Use |
|---|---|
| `symbol`, `cmName` | identity (client-side symbol filter) |
| `broadcast_Date` | publication datetime, IST — point-in-time key |
| `qe_Date` | period end — never a publication date |
| `consolidated` (`Consolidated` / `Standalone`), `audited` | basis labels |
| `type` (`Integrated Filing- Financials` / `- Governance`), `type_Sub` (`Original` / `Revised`) | which filing |
| `xbrl`, `ixbrl`, `pdf_attach` | structured instance, inline XBRL page, PDF |

`corporates-financial-results` is **not** used: its date parameters do not bound
the result and it stops at Q3 FY25. It is kept only as a documented legacy note
so a future reader does not reintroduce it.

### 5.3 XBRL instance

Taxonomy namespace observed:
`http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin` (Ind-AS). Older filings
may use others, so the parser **matches local element names and ignores the
namespace URI**.

- Build a context table `contextRef -> (start, end, dimensional?)`.
- Discard dimensional contexts — segment members produce dozens of duplicate
  `SegmentRevenue`, `OtherExpenses`, `SegmentAssets` facts.
- Pick the context whose `end` equals `qe_Date` and whose duration matches the
  period (quarter 60–115 d, half-year 150–200 d, nine-month 240–290 d, annual
  300–400 d). One filing reports the quarter and the year to date under the same
  end date, so duration is what separates them.
- Values are absolute INR (`RevenueFromOperations` = `1282600000000.00` is
  ₹1.28 lakh crore). Do not rescale to crore/lakh; print raw INR with the unit in
  the header.
- Quarterly instances carry the P&L, segment note and ratios only. The balance
  sheet and cash flow appear in the **annual** instance:

  | Method | India source |
  |---|---|
  | `get_income_statement` | quarterly or annual instance, as requested |
  | `get_balance_sheet` | latest annual instance filed ≤ `as_of_date`; a quarterly request is served from the most recent annual filing, labelled with its period |
  | `get_cashflow` | same as balance sheet |

  The limitation is stated in the header, not hidden.

Candidate element map (pinned against fixtures in Phase 2):

```
income_statement
  RevenueFromOperations            Revenue
  OtherIncome                      Other Income
  Expenses                         Total Expenses
  FinanceCosts                     Finance Costs
  DepreciationDepletionAndAmortisationExpense   Depreciation & Amortisation
  ProfitBeforeTax                  Profit Before Tax
  TaxExpense                       Tax
  ProfitLossForPeriod              Net Profit
  BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations   Basic EPS
  DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations Diluted EPS

balance_sheet (annual only)
  Assets, CurrentAssets, CashAndCashEquivalents, CurrentInvestments,
  BorrowingsCurrent, BorrowingsNoncurrent, CurrentLiabilities,
  CurrentFinancialLiabilities, Equity   — exact set pinned per fixture

cashflow (annual only)
  CashFlowsFromUsedInOperatingActivities
  CashFlowsFromUsedInInvestingActivities
  CashFlowsFromUsedInFinancingActivities
```

Header format mirrors `sec_edgar` so the analyst prompt shape does not change:

```
# Income Statement for RELIANCE.NS (quarterly), INR unless the row says otherwise
# NSE XBRL filed on or before 2026-10-25 (broadcast 2026-10-18 19:04 IST,
# consolidated, unaudited, Ind-AS), period 2026-07-01 to 2026-09-30
```

When a filing has no structured instance:

```
# Income Statement for RELIANCE.NS as of 2025-04-25
# Results were filed on 2025-04-25 20:05 IST ("Outcome of Board Meeting") but
# carry no structured XBRL. The filing is at <pdf url>. No figures are stated
# here: the report would have to read a PDF to obtain them.
```

That is the honest answer for a large cap like `RELIANCE` in FY25, and it is
strictly better than a withheld notice or an invented number.

### 5.4 SEBI PIT (insider) and shareholding

```
GET https://www.nseindia.com/api/corporates-pit?index=equities&from_date=…&to_date=…&symbol=…
GET https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=…
```

PIT rows: `acqName`, `personCategory`, `tdpTransactionType`, `secAcq`,
`befAcqSharesNo`, `afterAcqSharesNo`, `afterAcqSharesPer`, `acqfromDt`,
`acqtoDt`, `intimDt`, `anex` (`7(2)`), `xbrl`. Served when `intimDt ≤ trade_date`;
the trade date is shown beside the filing date, because the gap is the point.

Shareholding rows: `date` (period end), `broadcastDate` (publication),
`pr_and_prgrp` (promoter & promoter group %), public and employee columns,
history back to 2021. Served when `broadcastDate ≤ trade_date`.

### 5.5 India news

```
GET https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en
    query modifiers: after:YYYY-MM-DD before:YYYY-MM-DD   (history works, verified)
```

- Item: `title` (includes " - Publisher"), `link` (a Google redirect URL),
  `pubDate`, `description` (HTML), `<source url=…>Publisher</source>`.
- Report the publisher name and title; do not resolve the redirect (extra
  requests, brittle).
- Query builder: `"<company name>" (<SYMBOL> OR NSE OR BSE) after:<start>
  before:<end+1>`, falling back to the cleaned name alone when the symbol is
  ambiguous (`M&M` alone matches music results; `"Mahindra & Mahindra"` does
  not).
- Cap per query, dedupe on normalised title, filter with `in_window_ist`.
- GDELT DOC 2.0 is a second pass, not the primary: 6 s between calls, and a run
  whose window starts before `today - 90 days` gets an explicit note that GDELT
  cannot reach it. Never a silent gap.
- Publisher RSS (ET, Moneycontrol, Business Standard) only for windows that
  reach the present; their feeds carry the latest 20–40 items.
- `get_news` for India returns: matched articles, then "Exchange filings (NSE)"
  from §5.1 for the same window. Filings are the best-dated feed we have and
  cost no extra request when the fundamentals pack already fetched them (shared
  cache).

### 5.6 India macro

`india_macro` is keyless and built on data already reachable:

| Alias | Source |
|---|---|
| `usd_inr` | Yahoo `INR=X` |
| `india_vix` | Yahoo `^INDIAVIX` |
| `nifty_50`, `sensex` | Yahoo `^NSEI`, `^BSESN` (level + trailing return) |
| `cpi_india` | FRED `CPALTT01INM659N`, CPI growth rate same period previous year |
| `gsec_10y_india` | FRED `INDIRLTLT01STM`, 10-year g-sec yield, percent |
| `iip_india` | FRED `INDPROINDMISMEI`, industrial production index, 2015=100 |
| `real_gdp_india` | FRED `INDGDPRQPSMEI`, real GDP growth rate same period previous year |
| `repo_rate_india` | not covered. Returns an explicit "no free point-in-time source" notice. |

The four FRED aliases are served only when `FRED_API_KEY` is set; without it
they return an explicit missing-coverage notice rather than a flat reading.
FRED's India series are vintage-pinned, so they satisfy §3.3 rule 1. The probe
prints, per candidate ID: existence, title, frequency, last observation, and
whether a realtime vintage parameter is accepted. The shipped probe verified all
four IDs keylessly on 2026-10-26; only the vintage-pin branch needs a key.

The repo-rate gap is a build-vs-buy item (§13), not something to paper over with
a scraped homepage value.

---

## 6. Phases

Each phase is shippable on its own and leaves the US path identical.

### Phase 0 — Foundations (≈2 days)

- `markets.py`, `ist.py`, the `default_config` India block, `nse/client.py`,
  `nse/issuer.py`.
- `&` support: add `&` to `_TICKER_PATH_RE` in `symbols.py` and to the two
  TypeScript regexes (`runtime.ts:78`, `sweep.ts:44`). Audit that no ticker
  reaches a shell: `grep -rn "subprocess\\|os.system" tradingagents cli` must
  show no ticker interpolation.
- Confirm `M&M.NS`, `M&MFIN.NS`, `J&KBANK.NS` flow end to end (yfinance already
  serves them; only validation blocks them).

Done when: `M&M.NS` runs a market-only analysis; `bun test` and
`bun run typecheck` pass; Python suite unchanged.

### Phase 1 — Prices and technicals (≈1 day)

- `.BO` sparse-history detection: when Yahoo returns fewer bars than the window
  needs, return `NoMarketDataError` with the detail already in use ("latest row
  is …", "only N bars"), plus a hint naming `.NS` when the issuer master maps
  the name to an NSE symbol.
- Do **not** silently substitute `.NS` data for a `.BO` request: different
  exchange, different tape. A hint is honest; a substitution is not.

Done when: `RELIANCE.BO` over a sparse window reports the reason and
`RELIANCE.NS` is unaffected.

**Shipped 2026-10-25.** Measured 19 BSE scrips: complete tapes cover 94–95 % of
weekdays; truncated tapes return one bar whatever the window; windows longer
than the listing reach 33–42 % over five years. Nothing measured between 42 %
and 59 %, so the guard fires below 50 % of weekdays (windows of 20+ weekdays
only, `.BO` only) and names the `.NS` ticker without substituting it. The
indicator and snapshot paths, which never see the requested window, reject a
`.BO` frame shallower than five bars for the same reason.

### Phase 2a — Structured-coverage spike (≈2 days, gate)

Before writing statement code, measure what actually exists. For a fixed sample
of 20 issuers (5 Nifty 50, 5 mid cap, 5 small cap, 5 `.BO`-only), for the last 8
quarters plus 3 annual reports, record:

- does the announcement exist, and what is its `desc` and `hasXbrl`;
- does the integrated-filing index carry a Financials row, and does it have
  `xbrl`;
- does the instance parse with a duration-matched, non-dimensional context.

Exit criteria for continuing to 2b:

- ≥60 % of sampled quarters have a parseable XBRL instance; **and**
- ≥1 annual instance per sampled issuer parses with a balance sheet.

**Result 2026-10-25: the gate fails — 30/120 quarters = 25.0 %**
(`docs/india-phase2a-structured-coverage.md`). All 30 misses are "no filing
row", not parser failures: every instance the integrated index exposes parses
(30/30), and 14/15 NSE issuers have an annual balance sheet. The index itself
starts April 2025, so older quarters exist only as announcements. Decision:
ship Phase 2c (named and dated announcements for all history, XBRL statements
for the two covered quarters, annual history where present) and defer 2b's
eight-quarter promise. `.BO`-only coverage stays unmeasurable — BSE is blocked.

If coverage is below that, do not write a statement parser on hope: stop, and
take the coverage number to the build-vs-buy gate (§13) before proceeding. The
announcements-based fundamentals block from Phase 2c still ships, because it does
not depend on XBRL.

### Phase 2b — XBRL statements (≈5 days, conditional on 2a)

1. Freeze fixtures from the spike into `tests/fixtures/india/` — the index JSON,
   the instance XML, the expected rows — captured once and committed so all
   later work is offline.
2. `xbrl.py`: context table, dimensional rejection, period matching, unit
   assertion, statement row maps. Pure function:
   `parse_instance(xml_bytes, qe_date, consolidated) -> dict[str, list[Row]]`.
3. `filings.py`: paginate the integrated index, filter by symbol client-side,
   prefer `type_Sub == "Original"` over a later revision, pick the latest
   `broadcast_Date <= as_of_date`.
4. Public entry points with the headers from §5.3.

Done when:

- A run dated one day before a filing does not see it; a run dated the filing
  day does (inclusive, §3.3).
- Consolidated and standalone are both labelled; consolidated is the default.
- An issuer with no XBRL returns the §5.3 "filed but not structured" notice
  naming the date and the PDF.
- `AAPL` output is byte-identical to before the change.

### Phase 2c — Filings in the fundamentals pack (≈2 days)

`nse_filings.get_fundamentals` for India = the existing
`yahoo.fundamentals.get_fundamentals` output (unchanged, still withheld when
historical) **plus** an "As filed with NSE/BSE" block:

- the last three filings by `an_dt` ≤ `trade_date`, with subject, date and link;
- the latest structured P&L and annual balance sheet when XBRL exists;
- the latest shareholding pattern (`§5.4`);
- one line stating whether the run is live (Yahoo profile included) or
  historical (profile withheld, filings only).

One profile formatter, not two, and no change to the fundamentals prompt shape —
the block is appended text with the same heading levels.

`nse_pit.get_insider_transactions` ships here too.

Done when: a `RELIANCE.NS` run dated 2025-06-02 shows the FY25 results filing of
2025-04-25 with its link and states that no structured figures accompany it,
plus a Q3 FY25 or Q4 FY25 P&L where XBRL exists.

### Phase 3 — India news (≈5 days)

- `google_news.py`, `query.py`, `gdelt.py`, `publishers.py`, `__init__.py`.
- Company-name resolution from `issuer.py`, with `&` and punctuation handling
  pinned by tests (`M&M` vs `M & M` vs `Mahindra`).
- Section layout in `get_news`: `## <ticker> news, <start> to <end>`, then
  `## Exchange filings (NSE), <start> to <end>`, then a
  `<india_news unavailable …>` notice when a source could not be reached,
  reusing `coverage_gap` wording.
- `collect.py`: the news pack header says which sources answered.

Done when: `RELIANCE.NS` on a dated historical window returns dated articles plus
filings, with no item after `trade_date`; `AAPL` news output is unchanged;
GDELT's 3-month limit appears as a note rather than an empty section.

**Shipped 2026-10-26.** Live `RELIANCE.NS` over 2025-06-01..07 returned thirteen
dated articles from Google News and three exchange filings (17:49, 19:41 and
19:03 IST), with GDELT printing its 90-day notice instead of an empty section
and no publisher request (the window has closed). The query builder resolves the
issuer name from `EQUITY_L.csv`, drops the symbol for punctuated ones (`M&M`),
and falls back to the quoted name when the symbol query returns nothing. The
default chain is `nse,yfinance`: NSE self-gates a US ticker, so `AAPL` reaches
Yahoo with no extra request and unchanged output. Publisher feeds are fetched
only when the window reaches today, and each source that fails is named in the
output rather than read as an absence of news.

### Phase 4 — Social (≈1 day)

- `reddit.subreddits_for`: India branch → `IndiaInvestments`,
  `IndianStockMarket`, `DalalStreetBets`, `IndianStreetBets`. Verified reachable
  but low-volume; an empty result there is a real empty result.
- `stocktwits`: for `.NS`/`.BO`, return
  `<stocktwits not applicable to NSE/BSE listings>` without a request. That
  removes both the 404 noise and the `TCS` false positive, because a bare `TCS`
  never enters the India branch.
- The sentiment pack states that Indian retail discussion is thin on these
  sources, so silence is not read as indifference.

Done when: `collect_sentiment` for `TCS.NS` makes no StockTwits call, and for
`TCS` (US) still does.

**Shipped 2026-10-26.** `subreddits_for` branches on the exchange suffix before
crypto, so `TCS.NS` searches `IndiaInvestments`, `IndianStockMarket`,
`DalalStreetBets` and `IndianStreetBets`, while bare `TCS` stays the Container
Store and keeps the US defaults. Reddit searches the bare NSE symbol (`TCS`),
still in one combined request. StockTwits returns
`<stocktwits not applicable to NSE/BSE listings>` from a single constant before
any URL is built, and both the sentiment pack and the graph prompt skip the call
for India rather than spending it on a 404; the section says the coverage is
missing, not neutral, and that Indian discussion is thin, so silence is not
indifference.

### Phase 5 — Macro (≈3 days)

- `scripts/probe_india_sources.py`, then `india_macro.py` with §5.6 aliases.
- `collect.py`: `INDIA_MACRO_INDICATORS = ("cpi_india", "gsec_10y_india",
  "usd_inr", "india_vix", "nifty_50")`, selected by `market_for(ticker)`.
  Section title becomes "Macro series" for both markets.
- `default_config`: `global_news_queries_by_market = {"IN": [...]}`;
  `get_global_news` gains an optional `ticker` argument
  (`InjectedState("company_of_interest")`, default `""`), and
  `get_global_news_yfinance` picks the overlay for India. The worker protocol is
  unaffected — the argument never crosses the TS boundary.

Done when: an `AAPL` news pack has the same six series and title text as today; a
`RELIANCE.NS` pack has the India series and India-phrased global news queries.

**Shipped 2026-10-26.** `india_macro` serves the four Yahoo quotes (clipped to
`as_of_date`, no future close) and delegates the four FRED aliases to
`fred.get_macro_data` so the realtime vintage pin is reused. A missing
`FRED_API_KEY` and `repo_rate_india` both return explicit gap notices, never a
zero or a scraped current value. The probe verified every Yahoo alias and every
FRED ID keylessly (the vintage-pin branch needs a key). The default macro chain
is `india_macro,fred`: a non-India alias is a `VendorDeclinedError`, so the
router falls through without turning the decline into `NO_DATA_AVAILABLE` and
masking FRED's real answer. The news pack switches to
`INDIA_MACRO_INDICATORS`, titles the block "Macro series", records the repo-rate
gap as a note, and passes the run's ticker to `get_global_news` so an NSE/BSE
run searches India-phrased headlines while `AAPL` keeps the configured global
list unchanged. The Graph news analyst also gets the India aliases in its tool
description and prompt.

### Phase 6 — Reporting currency and portfolio (≈2 days)

- `agents/context.py`: add a currency line for non-USD instruments ("quote
  currency INR; report prices, targets and position sizes in INR, never
  converted to USD"), derived from `markets.report_currency`.
- `agents/schemas.py`: the target-price descriptions already say "the
  instrument's quote currency"; add a test that an INR run's schema text does not
  say USD.
- `portfolio`: accept `INR` in `PortfolioContext.currency` against a short
  allowlist; a USD portfolio on an INR instrument must state both currencies
  rather than mixing them silently.
- Memory/backtest: `resolve_benchmark` already maps `.NS`/`.BO`. Add the
  benchmark's currency to the reflection context so "alpha vs ^NSEI" is never
  compared against a USD return downstream.

Done when: a plugin run on `RELIANCE.NS` writes reports whose every figure is
labelled INR, and the memory entry reads "Alpha vs ^NSEI".

### Phase 7 — Docs and release (≈2 days)

- `docs/cli.md` "Markets and tickers": replace the one-line India mention with a
  subsection — `.NS` preferred, `.BO` and its sparse-history caveat, what is
  filed vs live, what is unavailable, and the `&` symbols.
- `docs/cli.md` "Fundamentals as filed": add the NSE/BSE paragraph beside the
  EDGAR one — broadcast date, consolidated default, quarterly P&L only, annual
  balance sheet, filings without XBRL named as such, restatements as filed.
- `.opencode/plugins/tradingagents/README.md`: one paragraph under
  "Configuration" that India tickers need no configuration; no protocol change.
- `CHANGELOG.md` under `[Unreleased]`: an `Added` bullet for the India vendors, a
  `Changed` bullet for `&` tickers, a `Fixed` bullet for IST windows if it landed
  as a fix.

Done when: `docs/cli.md` has no claim the code does not keep, and the root
README is still OpenCode-only.

---

## 7. Ticker handling

| Input | Today | After |
|---|---|---|
| `RELIANCE.NS` | works | works |
| `RELIANCE.BO` | works, sparse | works, sparse, with a reason |
| `M&M.NS` | `ValueError` from `safe_ticker_component` | works |
| `M&MFIN.NS`, `J&KBANK.NS` | rejected | work |
| `TCS` | US Container Store | unchanged — US |
| `TCS.NS` | prices only | full India |
| `^NSEI` | works (index) | unchanged; India macro reuses it |

`&` is allowed because it cannot traverse a path and every filesystem write goes
through `pathlib`, never a shell. The vendor call passes the raw canonical symbol
and lets `requests`/`yfinance` percent-encode it; verified that yfinance already
returns data for `M&M.NS`, `M&MFIN.NS`, `J&KBANK.NS`.

`.BO`-only companies: `issuer.py` can map a BSE symbol to an NSE symbol only by
normalised company-name match against `EQUITY_L.csv` (Yahoo exposes no ISIN).
Exact normalised match only, plus a small configurable override map
`india_symbol_aliases` for dual-listed names that differ. No fuzzy matching — a
wrong join would attach another company's filings to the report.

---

## 8. OpenCode plugin impact

No protocol change. `protocol.schema.json`, the generated TypeScript types, the
worker, and the plugin's stage orchestration are untouched.

- `tradingagents/opencode/contract.py` inherits `&` support from
  `safe_ticker_component` automatically.
- `collect.py` gains the market-aware macro list and section titles. The pack
  size caps and trim ranks stay; the India fundamentals pack is larger (filings
  block), so the 20 k cap is checked with a fixture-based test rather than by
  eye.
- `runtime.ts` / `sweep.ts` regexes change in lockstep with the Python one; a
  test asserts the patterns agree.
- Nothing in the plugin needs a new option. `FRED_API_KEY` stays optional and is
  read the same way the CLI reads it.

---

## 9. Configuration additions

```python
# default_config.py, new keys only
"india": {
    "preferred_exchange": "NSE",            # .NS preferred when both listings exist
    "statement_basis": "consolidated",      # consolidated | standalone
    "announcements_lookback_days": 90,
    "gdelt_enabled": True,
    "publisher_feeds_enabled": True,
    "integrated_filings_page_size": 2000,
    "request_timeout_seconds": 30,
    "min_interval_seconds": {
        "nse": 2.0, "archives": 1.0, "gdelt": 6.0,
        "google_news": 2.0, "publishers": 2.0,
    },
},
"global_news_queries_by_market": {
    "IN": [
        "RBI repo rate monetary policy inflation India",
        "NSE Nifty Sensex earnings results quarterly",
        "rupee USD/INR FII foreign institutional flows",
        "SEBI corporate governance India regulatory",
    ],
},
"india_symbol_aliases": {},                  # BSE<->NSE overrides
```

The news pack's India series list is `INDIA_MACRO_INDICATORS` in `collect.py`,
not a config key: it is the pack's shape, not a vendor choice, and the analyst's
tool still accepts any alias. `FRED_API_KEY` stays the only optional key.

---

## 10. Test plan

Offline by default — the suite's `_no_network` fixture already refuses sockets
unless a test is marked `integration`.

| File | Covers |
|---|---|
| `tests/test_markets.py` | suffix → market, currency, exchange; `TCS` vs `TCS.NS` |
| `tests/test_ticker_ampersand.py` | `&` accepted by Python and both TS regexes; `..`, `/`, NUL still rejected |
| `tests/test_ist_window.py` | 23:30 IST item on `trade_date` included; 00:15 IST next day excluded; UTC-boundary regression |
| `tests/test_nse_client.py` | throttle spacing, 429 retry once, cache TTL, breaker opens after 3 failures |
| `tests/test_nse_filings_index.py` | pagination walk, symbol filter, Original over Revised, latest filing ≤ date |
| `tests/test_nse_xbrl_parse.py` | fixtures: quarterly P&L, dimensional facts ignored, Q vs 9M context selection, annual balance sheet, units |
| `tests/test_nse_filings_unstructured.py` | `RELIANCE` FY25 case: filing date served, PDF link present, no invented figures |
| `tests/test_nse_pit.py` | `intimDt` gate; trade date shown beside filing date |
| `tests/test_nse_shareholding.py` | `broadcastDate` gate; promoter % row |
| `tests/test_india_news.py` | query builder with `&`; dedupe; window filter; GDELT 3-month note; `after:`/`before:` format |
| `tests/test_india_macro.py` | alias table; missing FRED key degrades; repo-rate notice |
| `tests/test_india_social.py` | India subreddits; no StockTwits call for `.NS`; `TCS` US unaffected |
| `tests/test_india_no_us_regression.py` | snapshot: `AAPL` fundamentals, statements, news, macro unchanged |
| `tests/test_layering.py` | still only `dataflows` imports vendor libs |
| `.opencode/plugins/tradingagents/*.test.ts` | regex parity, `M&M.NS` accepted |

Integration tests (marked, skipped without network): one live `RELIANCE.NS`
pipeline, one `M&M.NS` price run, one `RELIANCE.BO` sparse-window run.

Acceptance gate for the whole plan:

1. `uv run pytest` green, including the no-US-regression snapshot.
2. `bun test` and `bun run typecheck` green.
3. `/ta-analyze RELIANCE.NS 2025-06-02` produces dated filings, structured
   figures where XBRL exists, dated news, INR figures, and no USD phrasing.
4. `/ta-analyze RELIANCE.BO 2026-10-25` produces prices + news and names the
   filing gap.
5. `/ta-analyze AAPL 2025-06-02` is unchanged.
6. `graphify-rs build --path . --output ~/.graphify-rs/TradingAgents-opencode-24-10-2026 --no-llm --update` runs after each phase.

---

## 11. Rollout

1. Phases 0–1: `feat(india): market foundation` — additive, no behaviour change
   for any existing ticker.
2. Phase 2a spike: measurements in the PR description, no code shipped. This is
   a decision gate, not a feature.
3. Phases 2b–2c: `feat(india): filings backbone`. Vendors are additive, so
   rollback is a config line (`fundamental_data: "sec_edgar,yfinance"`).
4. Phases 3–4: `feat(india): news and social`.
5. Phases 5–6: `feat(india): macro and INR reporting`.
6. Phase 7 docs and changelog last, so the docs describe what shipped.

Every phase keeps a live integration test that is skipped when the network is
refused, so a CI-only failure means "no network", not "vendor broke".

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| NSE public endpoints are unofficial and can block or change | filings vanish mid-run | descriptive UA, ≥2 s throttle, cache, breaker, `VendorUnavailableError`, config rollback to `yfinance` |
| Structured XBRL missing for large caps (verified: `RELIANCE` FY25) | no historical P&L for that issuer | Phase 2a measures coverage; unstructured filings are named with their date and PDF; PDF extraction is explicitly out of scope |
| `corporates-financial-results` looks usable but is not | silent stale data | endpoint documented as legacy and unused; tests assert the integrated route |
| BSE API blocked (verified 403) | `.BO`-only scrips have no filings | documented as price+news coverage; `.NS` preferred; build-vs-buy gate |
| XBRL taxonomy drift | parse failure or wrong row | namespace-agnostic local-name matching, strict context selection, per-taxonomy fixtures, fail closed |
| XBRL quarterly has no balance sheet | "balance sheet" for a quarter is older | serve latest annual, labelled with its period |
| Google News is a sample, not an archive | false "no news" | `coverage_gap` semantics, source count in the header, GDELT as second pass |
| GDELT 3-month window and 429s | no long-range news | explicit note when the window is unreachable; throttle |
| IST vs UTC window | evening filings dropped or next-day leaked | `in_window_ist` everywhere, regression test |
| Consolidated vs standalone mix-up | double counting, wrong margins | basis label on every header, consolidated default, test |
| Corporate actions (splits, bonuses) | XBRL rows not comparable across years | show period and basis; never chain a growth rate across an unadjusted action |
| `&` in filenames | path handling | allow-list entry only; no shell; TS parity test |
| Symbol reuse/renames (`M&MFIN`) | wrong company's filings | exact normalised name match + explicit alias map, no fuzzy join |
| ToS / rate limits | IP block, legal exposure | public endpoints only, low volume, no identity spoofing, no bulk redistribution, cache-first |

The `.BO`, unstructured-filing and repo-rate gaps are where the honest answer is
"partial". All three are buy candidates, not workarounds.

---

## 13. Build vs buy gate

Re-open after Phase 2a and again after Phases 0–6 ship, with these numbers in
hand: structured-filing coverage on the user's watchlist, BSE-only share of that
list, fraction of runs whose balance sheet is stale by a quarter, and news recall
against a hand-checked sample.

Buy only if at least two hold:

- BSE-only scrips are a material part of real usage and the paid feed carries
  their filings with publication timestamps.
- Structured figures are required for filings where XBRL is absent, including
  large caps.
- Point-in-time balance sheets for quarters are required, not just annual.
- Repo rate, G-sec curve and CPI must be first-class and keyless.
- News must be exhaustive rather than a search sample.

Verified pricing data point: NSE's own EOD Corporate Data (fundamentals,
announcements, shareholding) is ₹5,00,000/year domestic, $8,000/year
international, delivered over SFTP after 20:00 IST daily. That buys coverage and
a licence, not a lower engineering cost — the adapters in this plan still apply,
only the transport changes. Other candidates (BSE corporate data, EODHD, Finnhub,
LSEG) are **unverified** here and must be judged on point-in-time fidelity, Indian
coverage, and redistribution terms before any integration work.

---

## 14. Open questions for approval

1. **Repo rate.** Accept "no free point-in-time source" (the news analyst is told
   the series is unavailable), or add a small RBI reader for the current value
   only, clearly marked live-only and withheld for historical runs?
2. **Unstructured filings.** Confirm the "name it, date it, link it, state no
   figures" policy for filings like `RELIANCE` FY25, rather than adding PDF
   extraction to this project.
3. **Phase 2a gate.** Accept stopping after the spike if structured coverage
   falls below 60 %, or is a below-threshold result acceptable if every filing is
   at least named and dated?
4. **Quarterly balance sheet.** Latest annual filing is one quarter behind in the
   worst case. Acceptable for the fundamentals analyst?
5. **`.BO`-only scrips.** Confirm the price+news-only position, or fund a BSE
   access route that is not blocked.
6. **News breadth.** Google News + GDELT + three publisher feeds is a search
   sample. Is sample-level recall acceptable, given the header states the source
   count?
7. **`&` in directory names.** Allow `&` in results directories (proposed), or
   normalise paths to `_` while keeping the raw symbol in report text?

---

## 15. Effort

| Phase | Days |
|---|---|
| 0 Foundations | 2 |
| 1 Prices/technicals | 1 |
| 2a Structured-coverage spike | 2 |
| 2b XBRL statements | 5 |
| 2c Filings in the fundamentals pack | 2 |
| 3 India news | 5 |
| 4 Social | 1 |
| 5 Macro | 3 |
| 6 INR reporting | 2 |
| 7 Docs/release | 2 |
| **Total** | **≈25 person-days**, 23 if 2a stops the work |

Critical path is Phase 2a: it decides whether structured historical fundamentals
are a build or a buy. Everything else is additive and can land in parallel once
Phase 0 is in.
