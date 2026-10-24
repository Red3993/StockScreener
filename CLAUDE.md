# Stock Research App — project memory

Read this at the start of every session. Update it at the end of every phase.
Full spec: `docs/BUILD_PROMPT.md`. Owner: Tom (single user, personal, never shared).

## Hard rules
- **$0 running cost.** Ask before adding anything that could cost money.
- Single user. No sign-up, no multi-user tables, no sharing features.
- US stocks only. Never fabricate data: show "N/A" plus the reason.
- Store derived data only. Raw EDGAR/13F/filings are processed in the job and discarded.
- Disclaimer in the UI: "For personal research. Not financial advice."
- Don't copy StockUnlock branding, UI, or text. Mirror only the scoring concept.

## History depths
| Data | Depth |
|---|---|
| Daily prices (close + volume) | 3 years, rolling |
| Own-history valuation percentiles | 3 years (min 1 year, else N/A) |
| Score history backfill | 3 years; Analyst excluded (`excludes_analyst = true`) |
| Insider trades (Form 4) | 3 years |
| Politician trades | 3 years |
| 13F holdings | 12 quarters |
| XBRL fundamentals | as far back as a metric needs (5y growth, 10y share count) |
| Analyst snapshots | from first snapshot onward (no free history exists) |

## Phase status
| Phase | Status | Notes |
|---|---|---|
| 1. Repo, source verification, infra | **in progress** | See "Phase 1 progress" |
| 2. Watchlists + price since added + notes | not started | |
| 3. Detail page + 1–10 scores | not started | |
| 4. Insider trades + Telegram | not started | |
| 5. Politician trades + tracked people | not started | |
| 6. Ownership / share count + MA crosses | not started | |
| 7. Calendar | not started | |
| 8. Sector divergence | not started | |

### Phase 1 progress (2026-10-20)
Done:
- Project scaffold, provider fallback layer (`jobs/providers/base.py`), `config/providers.yaml`.
- Source probe script (`jobs/probe/probe_sources.py`) + runner workflow (`.github/workflows/probe-sources.yml`).
  Probe tests every source, the yfinance analyst-field matrix across 20 tickers (large/mid/small,
  incl. bank, insurer, REIT, utility), and Finnhub/FMP premium gating. Output: `probe-results/probe-<env>.md|json`.
- Committee → sector/SIC conflict mapping seeded (`config/committee_sectors.yaml`, all 49 current committees checked).
- Web research on free tiers (table below).

Repo: **github.com/Red3993/StockScreener** (pushed 2026-10-23). Probe workflow runs on push to `jobs/probe/**`
and publishes results to the `probe-results` branch (this workspace has no GitHub API token, so results come back via git).

Runner probe 2026-10-23 (run 3): yfinance fully works, incl. all analyst fields; iShares works with new URL;
Fed, House Clerk, Senate eFD, congress-legislators work. SEC 403 (needs email UA), BLS 403, Bargo Cloudflare-blocked.
No local-runner fallback needed for yfinance so far (re-check once the weekly ~15k-call job runs at volume).

Runner probe 2026-10-24 00:09Z (run 4, with secrets): **all SEC EDGAR endpoints ✅**, Tiingo ✅, FRED ✅ (50 release dates).
FMP free: **402 premium-gated for small-cap symbols** (WDFC) on grades, price targets, estimates and EOD prices;
only earnings-calendar works → FMP is at most a large-cap analyst backup.
Finnhub free (run 5): recommendation trends ✅ (**4 monthly periods only**), earnings calendar ✅ (1,500 items);
price-target, upgrade-downgrade, eps-estimate ❌ 403 premium.
**Analyst decision:** yfinance primary for all 8 fields. Backups: Finnhub recommendation trends (rating mix only),
FMP large caps only. No free backup for targets/upgrades/revisions → if yfinance breaks, those metrics show N/A
and the Analyst score is computed from rating mix alone with a "partial data" badge.
**Earnings dates decision:** yfinance `.calendar` primary, Finnhub earnings calendar backup.
- **Build workspace network only reaches GitHub/package registries**, so live probes of SEC, Yahoo, iShares,
  Finnhub, etc. all failed from the workspace (expected; not a verdict on the sources).
  Re-confirmed 2026-10-20 22:45 ET: 1 ok (congress-legislators), 29 proxy-blocked, 17 skipped (no keys).
  This workspace cannot stand in for a runner. Real verification needs a GitHub Actions runner or Tom's machine.

Live-verified from the workspace (GitHub-hosted):
- `unitedstates/congress-legislators`: 49 committees, 181 subcommittees, 531 members with assignments; last commit 2026-09-24.
- `LuxAlgo/market-trackers-data`: published dump is **thin and stale** — 175 congress trades total,
  last generated 2026-09-07. Not usable as our backfill. The MIT toolkit (`LuxAlgo/market-trackers`)
  parses House Clerk + Senate eFD keylessly and is a good reference for our own parser.

Next (in order):
1. Tom: add `SEC_USER_AGENT` secret; sign up for free keys (Finnhub, FMP, Tiingo, FRED; optional AV, Stooq, Bargo).
2. Run the probe on a runner and locally on Tom's machine; fill in the "verified" columns below.
3. Decide public vs private repo (see Actions estimate).
4. Supabase project, schema, single-user auth; Next.js PWA shell on Vercel.
5. Universe builder, SIC mapping, keep-alive, health checks, Telegram system alerts.
6. Local-runner fallback if yfinance fails from the runner.

## Data sources (provisional — verify with probes)
"Research" = what public docs/pages said on 2026-10-20. "Runner"/"Local" = probe results (pending).

| Need | Primary | Backup | Free-tier facts (research) | Runner | Local |
|---|---|---|---|---|---|
| Universe | iShares CSVs (IWB, IVV, IJH, IJR) via `/us/products/<id>/x/latest-holdings.csv` | last good stored copy | Free, unofficial endpoint; **old `.ajax?fileType=csv` URL now returns HTML** | ✅ 1023/504/406/637 equities, **union 1,654** | pending |
| GICS sector | iShares `Sector` column | SIC → sector map | — | ✅ 11 sectors | pending |
| Industry routing | EDGAR `submissions` SIC | — | Free; User-Agent with email; ≤10 req/s | ✅ (after `SEC_USER_AGENT` secret) | pending |
| Fundamentals | `companyfacts.zip` (init) + `frames` | per-company `companyfacts` | Free, same SEC limits | ✅ frames 2,508 filers; companyfacts.zip 1.41 GB | pending |
| Daily prices | yfinance (✅ runner: 3y AAPL 753 rows; 20/20 batch) | Tiingo, then Stooq | yfinance: unofficial, IP rate-limits (maintainer: fetch less, cache). Tiingo: free "Starter" plan (limits to confirm). **Stooq: API key required since Mar 2026.** Alpha Vantage: 25 req/day — too small, not used. | pending | pending |
| **Analyst** | yfinance (all 8 fields) — **✅ runner: 8/8 fields populated for 20/20 tickers; analyst counts 3–58 (CALM/WDFC = 3, AAON/ALG = 4)** | Finnhub (recommendation trends), FMP (grades/targets/estimates — tier unclear) | Finnhub free ~60 calls/min, personal use; which analyst endpoints are free must be confirmed by probe. FMP free = 250 calls/day; whether analyst endpoints work on small caps must be probed. | pending | pending |
| Earnings / dividend dates | yfinance `.calendar` | Finnhub earnings calendar | — | ✅ | pending |
| Insider trades | EDGAR Form 4 (parse) | — | Free | ✅ | pending |
| 13F / 13D / 13G | EDGAR | — | Free | ✅ | pending |
| Congress trades | Parse House Clerk + Senate eFD ourselves (✅ both reachable from runner) | ~~Bargo~~ (❌ Cloudflare challenge from runner — dropped as backup) | Bargo: free JSON API, anonymous or free key, attribution required, no bulk redistribution (fine for personal). **Disclosed Capitol: no permanent free tier** (750 trial credits, then $6+/mo) → not used. LuxAlgo dump too thin. | pending | pending |
| Committees | `unitedstates/congress-legislators` | — | Free, CC0 | ✅ | ✅ |
| Executive branch trades | **dropped** | — | OGE 278-Ts are PDFs; only structured source found (Disclosed Capitol) is paid. | — | — |
| Macro calendar | Fed FOMC page; **FRED release dates** for CPI/jobs/PCE/GDP | BLS `.ics` | Free | Fed ✅; **BLS ❌ 403 from runner** → FRED promoted to primary; FRED needs key | pending |

## Free-tier budgets (preliminary estimates)
Limits verified 2026-10-20: Supabase free = 500 MB DB, 5 GB egress/mo, **pauses after 7 days inactive**, 2 projects.
GitHub Actions: **2,000 min/mo for private repos on Free; unlimited for public repos** on standard runners.
GitHub disables scheduled workflows in public repos after 60 days with no repo activity → keep-alive needed.

### Database size (steady state, year 1)
| Table group | Rows | Est. size |
|---|---|---|
| Daily prices, row per ticker-day (1,850 × 756) | ~1.4M | ~120 MB incl. index |
| — same, packed as one row per ticker-month (arrays) | ~67k | **~20 MB** (recommended) |
| Insider trades (market buys/sells only, 3y) | ~300k | ~60 MB |
| Politician trades (3y) | ~45k | ~15 MB |
| 13F positions (notable funds + watchlist holders, 12q) | ~90k | ~20 MB |
| Quarterly derived fundamentals (10y) | ~74k | ~25 MB |
| Score history (one row per change, all 8 scores as columns) | ~200k | ~16 MB |
| Current metric detail (for score transparency) | ~75k | ~10 MB |
| Analyst snapshots (weekly universe + daily watchlist) | grows ~95k/yr | ~19 MB/yr |
| Universe, watchlists, notes, calendar, alerts, health | small | <10 MB |
| **Total** | | **~285 MB (57%) with row-per-day prices; ~185 MB (37%) packed** |
Growth after year 1: ~25–30 MB/yr (analyst snapshots), since 3-year windows roll off.
Decision pending: packed monthly price rows (recommended) vs row-per-day.

### GitHub Actions minutes (per month)
| Job | Cadence | Est. min/mo |
|---|---|---|
| Daily: prices, Form 4, congress, watchlist valuation/analyst, alerts | 22 weekdays × ~15 min | ~330 |
| Weekly: analyst snapshot for ~1,850 tickers (~15k yfinance calls, throttled) + universe re-rank | 4.3 × ~130 min | ~560 |
| Monthly: universe refresh + `companyfacts.zip` | 1 × ~20 min | ~20 |
| Keep-alive + health check | daily × ~1 min | ~30 |
| **Steady state** | | **~940** |
| One-time backfill (3y Form 4 ≈ 12h at SEC limits, 3y congress, 12q 13F, prices, scores) | once | ~900–1,000 |
Private repo: steady state fits (47% of 2,000), but **month 1 with backfill ≈ 1,900** — too tight.
Recommendation: **public repo** (unlimited minutes; secrets stay private; code has no personal data). Alternative: private,
with the backfill spread across two months. **Tom to decide.**

## Fundamentals normalization (TTM rules) — to implement in Phase 3
- Flow items: TTM = sum of last 4 quarters. Q4 = FY − 9-month YTD (or FY − Q1−Q2−Q3).
- Balance-sheet items: latest point-in-time value, never summed.
- Duplicates/restatements: for each period, take the most recently *filed* value as of the evaluation date.
- Fiscal years: align by period end date, not fiscal labels. `frames` uses calendar periods (e.g. `CY2025Q3I`); log misaligned tickers.
- Concept fallbacks: ordered list per metric in config.
- Backfills use the fact's `filed` date (no look-ahead).

## Decisions log
- 2026-10-20: Executive-branch trades dropped (no free structured feed). Revisit if a free source appears.
- 2026-10-20: Disclosed Capitol rejected (paid). Congress trades: parse primary filings; Bargo as backup.
- 2026-10-20: Alpha Vantage not used for bulk (25 calls/day). Stooq kept as third price backup (needs free key).
- 2026-10-20: Committee conflict flag uses GICS sector + SIC ranges (defense contractors sit inside Industrials).

## How to run the probe
```
pip install -r jobs/requirements.txt
cp .env.example .env   # fill in what you have
PROBE_ENV=local python jobs/probe/probe_sources.py      # on Tom's machine
# on GitHub: Actions → "Probe data sources" → Run workflow (env=runner)
```
