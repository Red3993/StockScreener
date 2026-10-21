# Build Prompt: Personal Stock Research & Alert App

You are helping me build a personal stock research web app. Claude Code is the build tool. The app is for me only: one user, not commercial, not shared, and that won't change, so don't design for future multi-user support. **The hard constraint is $0 running cost.** Build it in phases, confirm each phase works before moving on, and tell me whenever a feature can't be done for free.

The app is inspired by StockUnlock.com's 1–5 "Stock Scores," expanded to a 1–10 scale, plus alerts and tracking features StockUnlock doesn't have. Don't copy StockUnlock's branding, UI, or text. Build our own design around the same scoring *concept*.

### Project memory
This build will span many sessions. Create and maintain a `CLAUDE.md` at the repo root that records:
- Decisions made and why (stack choices, chosen data sources, config defaults)
- The primary and backup provider for each data need, with free-tier limits as verified
- Phase status: done, in progress, known issues
- Free-tier budgets: database size, Actions minutes, API call volumes
- History depths (see "History depth" below)

Read it at the start of every session and update it at the end of every phase.

### History depth
**3 years is the standard history window across the app.** This applies to stored daily prices, own-history valuation percentiles, backfilled score history, insider trades (Form 4), politician trades, and 13F holdings (12 quarters). Fundamentals pulled from XBRL may go back further where a metric needs it (e.g., 5-year growth rates, 10-year share count trend), because those are a few derived numbers per ticker, not daily rows.

---

## 0. Before writing code: verify the data sources

**Setup first:** Create the GitHub repo and a minimal test workflow before verifying sources, so every source can be tested from a runner (see below), not just locally.

Free data APIs change often. Before building, check each source below. For each one, confirm that it still exists, what the free tier allows today, its rate limits, and its terms for personal use. Then give me a short table and recommend a primary and a backup source for each data need. Design a data layer with swappable providers so a dead source never breaks the app.

| Need | Candidates to evaluate |
|---|---|
| Universe constituents | iShares holdings CSVs for IWB (Russell 1000), IVV (S&P 500), IJH (S&P 400), IJR (S&P 600), or another free constituent source |
| Sector classification (peer group) | **Primary: the sector column in the iShares holdings CSVs** (GICS-based). **Fallback: SIC code from EDGAR `submissions`**, mapped to sectors via an editable config file. Use the fallback only for watchlist stocks outside the universe. |
| Industry classification (metric-set routing) | **SIC code from EDGAR `submissions` for every stock.** The iShares sector column only gives GICS *sector*, so it can't separate banks, insurers, and REITs. Use SIC to choose the sector-specific metric set (section 3). Store the SIC → metric-set mapping in the config file. |
| Fundamentals (financial statements, share counts) | **SEC EDGAR XBRL APIs.** Use the **`frames` API** (one concept across all filers for a period, ideal for sector percentiles) and the nightly **`companyfacts.zip` bulk file** for the initial load. Use per-company `companyfacts`/`submissions` calls only for incremental updates. Requires a User-Agent header with contact email; respect SEC rate limits. |
| Daily prices / history | yfinance (unofficial, personal use only; fine since this is single-user), plus free tiers of Finnhub, Alpha Vantage, FMP, Tiingo, Stooq |
| Insider trades | SEC EDGAR Form 4 filings (parse directly) |
| Institutional / big-holder changes | EDGAR 13F (quarterly holdings), 13D/13G (5%+ stakes) |
| Congressional trades | LuxAlgo `market-trackers-data` (open CC0 dataset on GitHub), Disclosed Capitol free API, Bargo Congress Trades, `crnicholson/capitol-api` (self-hosted, House only), with raw House Clerk / Senate eFD filings as fallback |
| Committee membership | `unitedstates/congress-legislators` GitHub repo (committee-membership-current.yaml) |
| Executive branch trades | OGE Form 278-T periodic transaction reports, or any aggregator that covers them. **Expect this one may not be feasible:** 278-Ts are mostly PDFs with no free structured feed. If there's no reliable free source, tell me and drop it rather than building a fragile PDF scraper. |
| Earnings dates | Free tier of Finnhub/FMP, or yfinance |
| Macro calendar | federalreserve.gov FOMC schedule, BLS release calendar (CPI, jobs), FRED API |
| **Analyst data (high priority, see below)** | **yfinance** (primary candidate), **Finnhub free tier**, **FMP free tier**, Alpha Vantage |

### Test from where the jobs will actually run
Yahoo and some other providers throttle or block datacenter IPs, and GitHub Actions runners use datacenter IPs. **Test every source from a GitHub Actions runner (or whatever scheduler we pick), not just locally.** Report the results separately for local vs. runner. If a source works locally but fails on the runner, flag it and propose a fallback (see "Local runner fallback" in section 1).

### Analyst data is a must-have
The Analyst score is very important to me. Do not drop it. Work hard to find a reliable free source.
- **Start with yfinance.** It exposes `recommendations` (buy/hold/sell counts), `analyst_price_targets` (mean/median/high/low), `upgrades_downgrades` (firm-level rating and target changes), `earnings_estimate` / `revenue_estimate`, `eps_trend` (how consensus EPS changed over 7/30/60/90 days), and `eps_revisions` (count of up vs. down revisions). Test each field across ~20 tickers of different sizes, **from a runner as well as locally**, and report how often each one is populated.
- **Backups:** Finnhub's free tier has recommendation trends (I believe history is short, ~4 months) and may gate price targets behind paid tiers; verify. Check what FMP's free tier allows for grades, price-target consensus, and estimates.
- **Build our own history.** Free sources mostly give a current snapshot, not a long history. Snapshot analyst data for the whole universe **weekly** (watchlist stocks **daily**) and store it, so revision trends and score history accumulate from day one.
- If coverage is thin for a stock (e.g., fewer than 3 analysts), show the score with a "thin coverage" badge rather than presenting it with false confidence.

## 1. Stack (all free tiers; adjust if you find a better $0 option)
- **Frontend:** Next.js (or similar) as a **Progressive Web App**, installable on my iPhone home screen.
- **Database + auth:** Supabase free tier (Postgres). **Single user:** lock auth to my email only (magic link or password). No sign-up flow, no multi-user tables, no sharing.
- **Scheduled jobs:** GitHub Actions cron or Supabase scheduled functions, for data pulls, score recalculation, and alert checks.
- **Hosting:** Vercel or Netlify free tier.
- Keep API keys in environment variables / repo secrets. Never commit them.

### Free-tier budgets
- **Database size.** Supabase's free database is small. **Never store raw EDGAR data in the database.** Download and process `companyfacts.zip`, `frames` responses, and raw filings inside the job (or a temporary cache), and write only the derived metrics, parsed trades, and scores we actually display. Cap stored daily price history at **3 years** per ticker (about 1.4M rows for the universe); store close and volume only unless a feature needs OHLC. Before Phase 1 ends, estimate the steady-state database size by table and record it in `CLAUDE.md`.
- **GitHub Actions minutes.** Estimate total monthly runtime for all scheduled jobs (daily, weekly, monthly, initial backfill). Private repos have a monthly minute cap; public repos get unlimited minutes on standard runners, and secrets stay private either way. Recommend public vs. private based on the estimate, and ask me before deciding.
- Track database size and warn me via Telegram when it passes 70% of the free-tier limit.

### Keep-alive and health checks
- Free Supabase projects pause after inactivity, and GitHub disables scheduled workflows on inactive repos. Build a keep-alive so neither happens.
- Add a daily health check that sends me a Telegram alert if any job fails, any data source errors repeatedly, or no new data arrived when it should have.

### Local runner fallback
If any source (most likely yfinance) is blocked from cloud runners, build a fallback: a small script I can run on my own computer on a schedule (cron / launchd / Task Scheduler) that pulls that data and writes it to Supabase. Keep it to one command with setup instructions. The health check should alert me if the local job hasn't reported in when expected.

## 2. Stock Universe
- The universe is the **union of the Russell 1000 and the S&P 1500** (S&P 500 + 400 + 600), de-duplicated. That should be roughly 1,700–1,900 tickers; report the actual count.
- Refresh constituents monthly. When a stock leaves the universe, keep its stored history but stop pulling new data unless it's on one of my watchlists.
- Any stock I add to a watchlist is always tracked, even if it's outside the universe. Score it against its sector peers within the universe.
- Sector percentiles are computed across the universe only.

## 3. Stock Scores (1–10)
**Important:** StockUnlock's exact formulas are proprietary. Mirror their *published structure* and build our own transparent logic.

- **7 equally weighted categories:** Valuation, Profitability, Financial Health, Growth, Dividends, Management (capital allocation), Analyst. Combine them into an **Overall score**.
- **Each category is a 1–10 score** built from several underlying metrics. Suggested starting metrics:
  - *Valuation:* For each stock, pick the most meaningful ratios (P/E, P/FCF, P/S, EV/EBITDA, P/B for financials). Score each ratio two ways: **(a) percentile within its sector** and **(b) percentile against the stock's own 3-year history.** Blend them **50/50** by default, with the blend weight set in the config file. Also factor in valuation relative to growth rate (e.g., PEG).
  - *Profitability:* gross, operating, and net margins; ROIC; ROE; FCF margin; and their trends.
  - *Financial Health:* current ratio, debt/equity, interest coverage, Altman Z-score, net cash/debt.
  - *Growth:* revenue, EPS, and FCF growth over 1/3/5 years, plus consistency.
  - *Dividends:* yield, payout ratio (earnings and FCF), growth streak, safety. Non-payers get "N/A," not a 1, and are excluded from Overall.
  - *Management:* share count trend (buybacks vs. dilution), ROIC vs. cost of capital, insider ownership and buying.
  - *Analyst:* consensus rating, price target vs. current price, EPS estimate revisions (eps_trend / eps_revisions), and net upgrades minus downgrades over the last 90 days.

### Fundamentals normalization (TTM rules)
Get this right before computing any ratio. Document the rules in `CLAUDE.md`.
- **Flow items** (revenue, net income, EPS, operating cash flow, capex, D&A, dividends paid): trailing twelve months = sum of the last four quarters. 10-Ks report the full fiscal year, not Q4, so **derive Q4 as FY minus the first nine months** (10-Qs often report 9-month year-to-date values; use those or sum Q1–Q3).
- **Balance-sheet items** (debt, cash, equity, share counts, current assets/liabilities): use the most recent point-in-time value. Don't sum them.
- **Restatements and duplicates:** XBRL often reports the same concept/period more than once (prior-year comparatives, amended filings). As of any date, use the most recently *filed* value for each period.
- **Fiscal years:** Handle non-calendar fiscal years. Align quarters by period end date, not fiscal labels. When using the `frames` API, use its calendar-aligned periods (e.g., `CY2025Q3I` for instantaneous values) and note any stocks it misaligns.
- **Concept fallbacks:** Companies tag the same item with different concepts (e.g., `Revenues` vs. `RevenueFromContractWithCustomerExcludingAssessedTax`). Keep an ordered fallback list per metric in the config file.
- Spot-check derived TTM values against a public source for ~10 tickers (including a non-calendar fiscal year company) and report the results.

### Sector-specific metric sets
Several standard metrics are meaningless for some sectors. Define **alternate metric sets in the config file** and apply them by industry, **routed by SIC code** (not the iShares sector column):
- **Banks / Financials:** drop current ratio, Altman Z, EV/EBITDA, gross margin, and FCF-based metrics. Use P/B and P/TBV, ROE, ROA, efficiency ratio, net interest margin, and capital ratios where XBRL provides them.
- **Insurance:** P/B, ROE, combined ratio where available; drop current ratio and Altman Z.
- **REITs:** use FFO/AFFO-based metrics (P/FFO, FFO growth, FFO payout ratio) in place of EPS/P/E and net-income payout; drop Altman Z. If FFO isn't reported in XBRL, approximate it (net income + depreciation & amortization − gains on property sales) and label it "approx. FFO."
- **Utilities:** keep the defaults, but don't penalize structurally high debt/equity relative to other sectors (percentiles are within-sector anyway; just confirm).
- Any metric that doesn't apply shows "N/A (not applicable to sector)," not a low score.

### Scoring method
- Rank each metric as a **percentile within the stock's sector** (peer group from the iShares CSV, SIC fallback). Valuation uses the 50/50 blend above. Average the metric percentiles in each category, then map to 1–10 by deciles.
- **Overall score = the simple average of the available category scores** (the 1–10 values), shown to one decimal place.
- **Missing categories:** If a category is N/A (no dividend, or no analyst coverage at all), average only the remaining categories and show which ones were excluded.
- **Transparency:** Tapping any score shows every metric, its raw value, its percentile, and how it contributed.
- Make the metric list, sector-specific sets, SIC routing, concept fallbacks, weights, and the Valuation blend a config file so I can tune them later.

### When scores are recalculated
Most categories come from quarterly filings, so don't recompute everything daily. Recalculate based on what actually changes:

| Category | Driven by | Recalculate |
|---|---|---|
| Profitability, Financial Health, Growth, Management, Dividends | 10-Q / 10-K filings (plus dividend declarations, Form 4s for Management) | When the company files a new 10-Q/10-K or a relevant event occurs. Also re-rank the whole universe **weekly**, since peers' new filings shift sector percentiles. |
| Valuation | Price × fundamentals | **Weekly** for the universe, **daily** for watchlist stocks |
| Analyst | Ratings, targets, estimates | **Weekly** for the universe, **daily** for watchlist stocks |

### Score history
- **Store a new score row only when a score actually changes**, with the date and the trigger (new filing, weekly re-rank, price move, analyst change). This keeps the database small while keeping full history.
- **Backfill 3 years** of score history.
- **No look-ahead:** When reconstructing or backfilling historical scores, use the **`filed` date** on each XBRL fact, not the period end date. A Q3 number filed in November didn't exist in October.
- **Own-history valuation in the backfill:** early in the backfill window there's less than 3 years of price history behind each point. Use whatever history exists, require a sensible minimum (e.g., 1 year), and show N/A for the own-history half before that.
- **Analyst has no backfill.** There's no free historical analyst data, so the Analyst category only exists from the day we start snapshotting. Backfilled Overall scores must be computed **without** Analyst, stored with a flag (e.g., `excludes_analyst = true`), and visibly marked on the history chart (e.g., a shaded "pre-analyst-data" region or a legend note). Don't splice the two series together silently.
- Chart score history on the stock detail page as a step chart.

## 4. Insider Trading
- Ingest all Form 4s for **all insiders**: officers, directors, and 10% owners, for stocks in the universe plus watchlists. **Backfill 3 years.**
- Show open-market buys (transaction code P) prominently. Show sales too, but **filter out routine/planned sales**, meaning Rule 10b5-1 plan trades. Also exclude non-market codes (option exercises, tax withholding, gifts, grants).
- **Detecting 10b5-1 trades:** Form 4s filed from April 2023 onward have a dedicated 10b5-1 checkbox, and the 3-year backfill falls entirely after that date, so use the checkbox. As a safety net, if a filing lacks the checkbox field, scan the footnotes and remarks for plan language (e.g., "10b5-1," "trading plan," "pre-arranged plan") and flag matches as "likely 10b5-1 (footnote)." Show which detection method applied.
- No minimum dollar threshold.
- Badge **CEO/CFO buys** and **cluster buys** (3+ insiders buying within ~14 days).
- **Alerts:** any insider buy on a watchlist stock; any CEO buy in the universe; cluster buys in the universe.

## 5. Politician Trades (Congress + Executive Branch)
- Ingest trades from **all members of Congress**, plus executive branch officials where data exists (see the note in section 0). **Backfill 3 years.**
- **Committee conflict flag (critical feature):** Map each member's committee and subcommittee assignments to the sectors those committees oversee. Examples: Armed Services → Defense/Aerospace; Energy & Commerce → Energy, Healthcare, Telecom; Financial Services / Banking → Financials; Agriculture → Ag/Food; Intelligence → Defense/Cyber. Store the mapping in an editable config file. **Flag and highlight** any trade where the stock's sector matches a committee the member sits on. Do the same for executive officials whose agency oversees the sector.
- The committee data is current membership. For backfilled trades, note in the UI that the flag reflects current assignments, which may differ from assignments at the time of the trade.
- Include spouse and dependent trades, labeled as such.
- Show trade date vs. disclosure date. Make clear that filings can lag trades by up to 45 days, and that amounts are reported as dollar ranges.
- **Alerts:** any politician trade in a watchlist stock; any committee-flagged trade market-wide.

## 6. Tracked People
- A "Follow a person" feature. Each tracked person gets a profile that links every public disclosure trail they have: SEC CIK (Form 4, 13D/13G), 13F filer ID for their fund, and Congress member ID.
- Seed list: Nancy Pelosi (House disclosures, including spouse trades), Elon Musk (Form 4 / 13D filings), Peter Thiel (filings for his funds/entities), Jared Kushner (Affinity Partners or any filing entity).
- **Be honest in the UI** when someone has limited public visibility. Private investors only appear when they cross a filing threshold, and some fund holdings are only disclosed quarterly. Show "last filing date" so I know how stale the data is.
- I can add more people by name. Help me look up their filer IDs.
- **Alerts:** any new filing by a followed person.

## 7. Ownership & Share Count View
- Share count over 1/3/5/10 years from EDGAR XBRL (a few values per ticker, so it isn't bound by the 3-year price window), labeled **Buybacks (decreasing)** vs. **Dilution (increasing)**, with % change.
- **13F scope:** Don't parse every 13F filer. Track (a) a curated, editable list of notable funds (seed it with ~25 well-known hedge funds and large managers, and let me add more), and (b) the top holders of each watchlist stock. **Backfill 12 quarters (3 years).** Show by quarter: top holders, new positions, exits, and notable funds adding or trimming. Store parsed positions only, not raw 13F filings.
- Show 5%+ stakeholder filings (13D/13G).

## 8. Technical Signals
- Charts with 50-day and 200-day simple moving averages.
- Detect on **daily close**, for watchlist stocks:
  - Price crossing above/below the 200-day SMA
  - Golden cross (50 above 200) and death cross (50 below 200)
- Show the signal history on the chart.
- **Alerts:** any of these crosses on watchlist stocks.

## 9. Watchlists
- **Multiple named watchlists.**
- When a stock is added, save the **date and closing price**. Display the absolute and % change since added. No benchmark comparison is needed.
- Per-stock **notes**, editable and timestamped.
- The watchlist table shows ticker, price, change since added, Overall score, and category scores, plus icons for recent insider, politician, or tracked-person activity.

## 10. Calendar
- A calendar view (month + agenda list) that combines:
  - Earnings dates for watchlist stocks
  - Ex-dividend and dividend pay dates for watchlist stocks
  - FOMC meetings and rate decisions
  - Major macro releases (CPI, jobs report, PCE, GDP)
- Filter by watchlist. Send an optional reminder alert the day before watchlist earnings.

## 11. Sector Divergence / Catch-up Signal
- I want a view that spots when **historically correlated stocks or sectors diverge**. The idea is that if one moves and its usual partner hasn't, the laggard may catch up.
- Implementation:
  - Compute rolling correlations (e.g., 60/120-day) between sector ETFs (XLK, XLE, XLF, etc.) and between a watchlist stock and its closest peers.
  - For pairs with high historical correlation, compute the spread of relative performance as a z-score. Flag pairs where it exceeds a threshold (e.g., ±2).
  - Also flag lead-lag relationships, where one series historically moves a few days before the other.
- Label this clearly as a **statistical signal, not a prediction**. Show how often similar divergences closed historically (a simple backtest over the 3-year price window) so I can judge whether it's worth anything. Note in the UI that 3 years is a short backtest sample.

## 12. Notifications: Telegram bot
- Real SMS costs money and free carrier email-to-text gateways are shut down, so **use a Telegram bot** as the notifier.
- Walk me through setup step by step: creating the bot with @BotFather, getting the bot token, getting my chat ID, and storing both as environment variables.
- Design the notifier as a pluggable interface so I can add another channel (e.g., Twilio SMS) later if I decide to pay.
- Alert settings page: toggle each alert type on or off, and choose watchlist-only vs. universe-wide. Bundle alerts into a daily digest option so I don't get spammed.
- Every alert includes ticker, what happened, the key numbers, and a link to the stock's page in the app.
- System alerts (job failures, stale data, missed local-runner check-ins, database size warnings) also go to Telegram.

## 13. Stock Detail Page
Price chart with MAs and signals, score breakdown (all 7 + Overall), score history chart (with the pre-analyst-data period marked), analyst panel (ratings distribution, price targets, recent upgrades/downgrades, estimate revisions), insider trades, politician trades (with committee flags), institutional ownership, share count trend, upcoming events, my notes, and an "add to watchlist" button.

## 14. Build Phases
1. Create the repo and a minimal test workflow, then verify data sources (section 0, with extra effort on analyst data, **tested from a runner**). Create `CLAUDE.md`, set up the project, database, single-user auth, data layer, universe list, sector and SIC mapping, keep-alive, and health checks. Deliver the database size and Actions-minutes estimates, plus the public/private repo recommendation. Build the local runner fallback if any source failed from the runner.
2. Watchlists + price tracking since added + notes (the core MVP).
3. Stock detail page + 1–10 scores (including TTM normalization, sector-specific metric sets, the Analyst score, and score history with the no-backfill flag).
4. Insider trades (including 10b5-1 detection) + Telegram alerts pipeline.
5. Politician trades + committee flags + tracked people.
6. Ownership/share count view + MA crosses.
7. Calendar.
8. Sector divergence signal.

After each phase: run it, show me screenshots or test output, report database size and Actions minutes used vs. the estimate, update `CLAUDE.md`, and list anything that didn't work or isn't free.

## Ground rules
- US stocks only.
- Don't fabricate data. If a source is missing a value, show "N/A" and explain why.
- Cache aggressively to stay under free-tier rate limits. Prefer bulk endpoints over per-ticker calls.
- Store derived data, not raw downloads.
- Add a small disclaimer: "For personal research. Not financial advice."
- Ask me before adding any dependency or service that could cost money.
