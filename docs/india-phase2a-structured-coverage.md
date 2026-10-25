# Phase 2a — structured-coverage spike (NSE/BSE)

Run date: **2026-10-25** (IST). Measurement only: no parser, no vendor
registration, no product code.

Script: `scripts/india_coverage_audit.py` (stdlib + `requests` + the repo's
`nse.client` / `nse.issuer` / `ist`). Raw evidence:
`docs/india-phase2a-artifacts/` (`coverage.json`, one
`coverage-<SYMBOL>.json` per issuer, and the first parseable quarter plus the
annual instance per issuer under `instances/`).

## 1. Method and exact endpoints

Every request went through `tradingagents.dataflows.vendors.nse.client`, so the
same throttle (2 s for `nse`, 1 s for `archives`), disk cache and circuit
breaker applied as they will in the adapters. No cookies were used.

| Feed | Request |
|---|---|
| Announcements | `GET https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=01-07-2023&to_date=31-10-2025&symbol=<NSE symbol>` |
| Integrated filings | `GET https://www.nseindia.com/api/integrated-filing-results?index=equities&from_date=01-07-2023&to_date=31-10-2025&symbol=<NSE symbol>&size=2000&page=1` |
| XBRL instance | the row's `xbrl` URL on `https://nsearchives.nseindia.com` |

Notes on the integrated feed:

* `from_date` / `to_date` are `DD-MM-YYYY`; `size=2000` overrides the default of
  20; `page` is 1-based. `{data, size, page, totalCount}` came back as expected.
* A `symbol` filter works. The whole 2023-07-01..2025-10-31 span returned 7–12
  rows per issuer, so one request per issuer covered all eight quarters and
  three year-ends. Each row was filtered client-side by `qe_Date`.
* The unfiltered feed is large (13 991 rows for 2025-04-01..2025-08-31), which
  is why the per-issuer symbol filter matters for a backtest.

Selection rules:

* Quarter end: the eight calendar quarters ending 2025-06-30 … 2023-09-30.
* Integrated row: `type` contains `Financials`, `qe_Date` equals the quarter
  end, consolidated preferred over standalone, `Original` preferred over a
  revision, earliest publication within a class.
* Announcement: a results-related row (`Financial Result*`,
  `Outcome of Board Meeting`, or text mentioning results/outcome) published
  between the quarter end and quarter end + 90 days. SEBI's quarterly deadline
  is 45 days; 90 is deliberately generous.
* Instance parse: XML must parse, the root must be `xbrli:xbrl`, and there must
  be a **non-dimensional** context whose duration is exactly the quarter
  (or, for an annual, 1 April–31 March) carrying at least one revenue and one
  profit fact. Dimensional contexts are discarded. For an annual balance sheet,
  a non-dimensional instant context at the year end must carry `Assets`,
  `EquityAndLiabilities`, `CapitalAndLiabilities`, `TotalAssets` or
  `TotalEquityAndLiabilities`.
* A filing counts as on time only if `broadcast_Date`/`an_dt` is within the
  90-day window. All 30 parseable instances were on time (observed lags 10–58
  days).

## 2. Sample

NSE-keyed (15):

* **Large (5):** RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK.
* **Mid (5):** FEDERALBNK, MPHASIS, GODREJPROP, INDHOTEL, CUMMINSIND.
* **Small (5):** KEC, CARBORUNIV, JYOTHYLAB, RAIN, TEJASNET.

All 15 were confirmed present in NSE's `EQUITY_L` master during the run. The
mid/small labels are classification by market convention, not a measured
market-cap cut: the spike has no cap feed.

`.BO`-only (5): NAPEROL.BO (Naperol Investments), SKPSEC.BO (SKP Securities),
WARRENTEA.BO (Warren Tea), SURAJ.BO (Suraj Products), GALXBRG.BO (Galaxy
Bearings).

These five were identified from a reachable two-source check on 2026-10-25:
each has a live Yahoo BSE quote (price and name returned by the Yahoo chart
endpoint), and neither the Yahoo ticker nor the company name appears in NSE's
`EQUITY_L` master. BSE's own JSON API is not usable from this network (the
scrip-list endpoint returned HTTP 200 with a 14 KB HTML single-page-app shell
instead of JSON; the planning note records 403), so the BSE-only status is
inferred from Yahoo + NSE's master, not from a BSE listing file. They are
reported separately and **not** mixed into the NSE headline denominator.

## 3. Per-issuer result

`✓` = integrated Financials row exists and the instance is parseable for that
quarter. `·` = no Financials row (no instance to test). All NSE names have a
results announcement for all eight quarters.

| Issuer | Bucket | Q1FY26 | Q4FY25 | Q3FY25 | Q2FY25 | Q1FY25 | Q4FY24 | Q3FY24 | Q2FY24 | (a) Announcements | (b) Financials rows | (c) Parseable | Annual BS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RELIANCE | large | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| TCS | large | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| HDFCBANK | large | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| INFY | large | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| ICICIBANK | large | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| FEDERALBNK | mid | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| MPHASIS | mid | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| GODREJPROP | mid | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| INDHOTEL | mid | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| CUMMINSIND | mid | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| KEC | small | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| CARBORUNIV | small | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| JYOTHYLAB | small | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| RAIN | small | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | none |
| TEJASNET | small | ✓ | ✓ | · | · | · | · | · | · | 8/8 | 2/8 | 2/8 | 2025 |
| NAPEROL.BO | BSE-only | – | – | – | – | – | – | – | – | 0/8 measurable | 0/8 | 0/8 | no BSE feed |
| SKPSEC.BO | BSE-only | – | – | – | – | – | – | – | – | 0/8 measurable | 0/8 | 0/8 | no BSE feed |
| WARRENTEA.BO | BSE-only | – | – | – | – | – | – | – | – | 0/8 measurable | 0/8 | 0/8 | no BSE feed |
| SURAJ.BO | BSE-only | – | – | – | – | – | – | – | – | 0/8 measurable | 0/8 | 0/8 | no BSE feed |
| GALXBRG.BO | BSE-only | – | – | – | – | – | – | – | – | 0/8 measurable | 0/8 | 0/8 | no BSE feed |

Taxonomies seen: `in-bse-fin` and `in-capmkt` (plus bank-specific tags). The
accepted quarterly pair was typically `RevenueFromOperations` /
`ProfitLossForPeriod`; for banks `TotalIncome` / `ProfitLossForPeriod` and
`Assets` / `CapitalAndLiabilities` were the relevant names. The exact tags per
issuer are in `coverage.json`.

## 4. Headline coverage

| Measure | NSE-keyed (15 issuers) | Including `.BO`-only (20) |
|---|---|---|
| Issuer-quarters measured | 120 | 160 |
| Quarters with a parseable instance | **30 / 120 = 25.0 %** | **30 / 160 = 18.8 %** |
| Quarters with a parseable, on-time instance | 30 / 120 = 25.0 % | 30 / 160 = 18.8 % |
| Results announcement exists | 120 / 120 = 100 % | 120 / 160 = 75 % (`.BO` unmeasurable) |
| Issuers with ≥1 annual balance sheet | **14 / 15 = 93.3 %** | **14 / 20 = 70.0 %** |
| Quarters with an integrated Financials row | 30 / 120 = 25.0 % | 30 / 160 = 18.8 % |

The 25 % is entirely a **history-depth** result, not a parsing result:

* the integrated index carries no `Financials` row before Q4 FY25 for any
  issuer in the sample. Q3 FY25 has a `Governance` row only; Q3 FY24 back to
  Q2 FY24 have nothing. The first Financials rows in the sample were published
  in April 2025 (Q4 FY25).
* for the two quarters where the regime has Financials rows — Q4 FY25 and
  Q1 FY26 — **30 / 30 = 100 %** parsed with a duration-matched, non-dimensional
  revenue+profit context.

So the correct pair of numbers is: **25.0 % over the fixed eight-quarter
window**, **100 % over the two quarters the feed actually covers**.

Annuals: only FY25 has an integrated Financials row (published Apr–May 2025).
FY24 and FY23 have no row at all, like the older quarters. 14 of 15 issuers
have an FY25 balance sheet in a non-dimensional instant context. The exception
is RAIN (see below); its fiscal year is December, and its 31-Dec-2024
integrated row is `Governance` only, so there is no annual Financials instance
at either year-end.

### The RELIANCE FY25 case

The planning note said RELIANCE FY25 had `hasXbrl: false` and no Financials row
in the integrated index. Re-checked live on 2026-10-25:

* announcement: **25-Apr-2025 20:05:15 IST**, `Outcome of Board Meeting`,
  `hasXbrl: false`, attachment PDF;
* integrated index: a `Financials / Consolidated / Original` row for
  `qe_Date 31-MAR-2025` published **25-Apr-2025 21:57:16 IST**, with an XBRL
  instance that parses and carries Q4 + FY25 duration contexts and an instant
  balance sheet (`Assets`, `EquityAndLiabilities`).

The row is counted as coverage because the instance parses today. This is not
a memory fill: both the 20:05 announcement and the later 21:57 filing are in
the raw JSON. It does show a point-in-time gap of about 1 h 52 min on the
publication evening: a run at 20:30 IST on 2025-04-25 would have seen the
announcement but not the instance. Excluding RELIANCE FY25 from the numerator
changes the headline only from 30/120 (25.0 %) to 29/120 (24.2 %).

## 5. Why every miss happened

NSE-keyed quarters (120 total):

| Reason | Count | Detail |
|---|---|---|
| No integrated `Financials` row | 90 | Q3 FY25, Q2 FY25, Q1 FY25, Q4 FY24, Q3 FY24, Q2 FY24. Q3 FY25 has a Governance row only; the rest have no row. |
| No XBRL link | 0 | No Financials row existed to carry one. Announcement rows for those quarters have no XML link (`hasXbrl` is false). |
| Dimensional-only | 0 | — |
| XML unparseable | 0 | — |
| Other | 0 | — |

All 90 misses do have a results announcement (100 % announcement coverage), so
Phase 2c's filings block is unaffected.

NSE-keyed annuals (45 total):

| Reason | Count | Detail |
|---|---|---|
| No integrated `Financials` row | 30 | FY24 and FY23, all 15 issuers. |
| No balance sheet | 1 | RAIN FY25. The Mar-2025 instance has only Q1-CY2025 and segment contexts; 18 dimensional duration contexts, no non-dimensional annual context and no balance-sheet instant. RAIN's own Dec-2024 year-end has a Governance row only. |
| Parsed with a balance sheet | 14 | FY25, all issuers except RAIN. |

`.BO`-only bucket (40 quarters + 15 annuals): not measurable. The NSE feeds are
keyed by NSE symbol, these scrips have none, and the BSE API is not returning
usable JSON from this network. Recorded as `no_nse_key`, not as a zero that
would silently change the NSE denominator.

## 6. Limitations

1. **`.BO`-only is unmeasured.** BSE-only status is inferred (Yahoo BSE quote +
   absence from NSE `EQUITY_L`), and no BSE filing feed was reachable. The
   18.8 % "including .BO" number is a sensitivity, not a coverage estimate for
   BSE scrips.
2. **BSE API is blocked in practice.** `api.bseindia.com` did not return JSON;
   the response was an HTML shell. No BSE filing/XBRL evidence was used.
3. **Legacy endpoint not used.** `corporates-financial-results` is excluded by
   the plan; it was not used, even though older XBRL might exist there, because
   its date handling is unbounded and it is stale.
4. **History starts in 2025.** The integrated index has no Financials rows
   before Q4 FY25 in this sample. This is the dominant limitation; it affects
   large caps as much as small caps.
5. **Quarterly balance sheets are the exception, not the rule.** Of the 30
   parseable quarterly instances, 15 carry a non-dimensional balance sheet: the
   14 Q4 FY25 annual instances plus RAIN's Q1 FY26 instance. In the 15 non-Q4
   quarters only RAIN has one. Outside the Q4/annual filing, a point-in-time
   balance sheet is not reliably available.
6. **Announcement `hasXbrl` is not a reliable signal.** It is false for
   announcements whose matching integrated Financials row has a working XBRL
   instance (e.g. TCS Q1 FY26). The XBRL link must come from the integrated
   index, not from the announcement.
7. **Fiscal-year mismatch.** The script fixes annual year-ends at 31 March. RAIN
   is a December-year issuer; the fixed date is wrong for it, but even its own
   December row is Governance-only, so the conclusion does not change.
8. **Tag matching is a spike heuristic.** Revenue/profit/balance-sheet tag sets
   were chosen from observed `in-bse-fin` / `in-capmkt` instances. A production
   parser needs per-taxonomy fixtures and a stricter unit/basis check (Phase 2b).
9. **No PDFs were downloaded**; “no XBRL” means no XBRL link in either feed, not
   that the filing is absent.

## 7. Verdict

**Invoke the §13 build-vs-buy gate before writing the Phase 2b parser.** The
exit criterion is not met:

* quarters parseable over the specified eight-quarter window: **25.0 %**
  (30/120) against the **60 %** threshold;
* annual balance sheet per NSE issuer: **14/15** (93.3 %) against “≥1 per
  issuer” — this one nearly passes, and with the `.BO` bucket included it is
  14/20 (70 %).

The decisive number is the 25 %, and it is not a parser failure: every
Financials instance the feed exposes parses (30/30). The feed simply has no
structured history before Q4 FY25. Building a statement parser now would give a
historical run empty fundamentals for six of eight quarters, including all five
large caps.

Against §13, two buy conditions are live:

* structured figures are needed for filings where XBRL is absent, including
  large caps — true for 90/120 quarters in the sample;
* point-in-time balance sheets for quarters are needed — 14 of the 15 quarterly
  balance sheets in the sample come from the Q4/annual instance; Q1–Q3 have
  none (apart from RAIN's Q1 FY26), so a quarterly balance sheet is not
  generally available.

The BSE-only condition is unknown: 5 of 20 sample slots are BSE-only by
construction, but this spike cannot measure their share of real usage or
whether a paid feed carries their filings. Repo-rate and news conditions are
outside this spike.

If the product only needs the current quarter plus the latest annual, Phase 2b
is viable now (100 % of the two covered quarters, 14/15 annual balance sheets).
If it needs point-in-time quarterly fundamentals back to 2023, take 25.0 % to
the gate and do not build on hope.

## 8. Reproducing

```bash
.venv/bin/python scripts/india_coverage_audit.py            # resume completed issuers
.venv/bin/python scripts/india_coverage_audit.py --refresh  # recompute all
```

The script writes each issuer's JSON as soon as it finishes, then rewrites
`coverage.json` with the summary. If the circuit breaker opens it stops and
leaves the partial files in place. Cached JSON lives for 7 days and cached XBRL
for 90 days, so a rerun is cheap.
