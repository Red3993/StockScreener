# Source probe: `runner` (2026-10-24 00:14 UTC)

| Need | Source | Status | Detail |
|---|---|---|---|
| Ticker→CIK map | SEC company_tickers.json | ok | HTTP 200, 797,862 bytes, 10,425 tickers |
| Industry (SIC) | EDGAR submissions | ok | HTTP 200, 163,917 bytes, AAPL SIC=3571 Electronic Computers |
| Fundamentals | EDGAR companyfacts (per co.) | ok | HTTP 200, 3,789,099 bytes, 503 us-gaap concepts for AAPL |
| Fundamentals | EDGAR frames API | ok | HTTP 200, 383,777 bytes, 2,508 filers report Revenues CY2024 |
| Fundamentals (bulk) | EDGAR companyfacts.zip | ok | HTTP 200, 1.41 GB, Last-Modified Fri, 23 Oct 2026 23:48:08 GMT |
| Insider trades | EDGAR Form 4 (current feed) | ok | HTTP 200, 22,792 bytes, 40 recent Form 4 entries |
| Insider/13F/13D index | EDGAR daily form index | ok | 2026-10-22: Form4=507, 13F-HR=203, 13D=0, 13G=0 |
| Universe + GICS sector | iShares IWB holdings CSV | ok | 1023 equities, 11 sectors |
| Universe + GICS sector | iShares IVV holdings CSV | ok | 504 equities, 11 sectors |
| Universe + GICS sector | iShares IJH holdings CSV | ok | 406 equities, 11 sectors |
| Universe + GICS sector | iShares IJR holdings CSV | ok | 637 equities, 11 sectors |
| Daily prices | yfinance history (3y) | ok | 753 rows, last 2026-10-23 |
| Daily prices (batch) | yfinance download 20 tickers | ok | 20/20 tickers returned closes |
| Daily prices | Tiingo | ok | HTTP 200, 1,851 bytes |
| Daily prices | Stooq (API key since 2026) | skipped | no STOOQ_API_KEY set |
| Daily prices | Alpha Vantage | skipped | no ALPHAVANTAGE_API_KEY set |
| Analyst / earnings | Finnhub stock/recommendation | ok | HTTP 200, 393 bytes, 4 monthly periods of history |
| Analyst / earnings | Finnhub stock/price-target | FAIL | PREMIUM-GATED: HTTP 403, 51 bytes, items=0, '{"error":"You don\'t have access to this resource."}' |
| Analyst / earnings | Finnhub stock/upgrade-downgrade | FAIL | PREMIUM-GATED: HTTP 403, 51 bytes, items=0, '{"error":"You don\'t have access to this resource."}' |
| Analyst / earnings | Finnhub stock/eps-estimate | FAIL | PREMIUM-GATED: HTTP 403, 51 bytes, items=0, '{"error":"You don\'t have access to this resource."}' |
| Analyst / earnings | Finnhub calendar/earnings | ok | HTTP 200, 237,188 bytes, items=1500, '{"earningsCalendar":[{"symbol":"AEI","date":"2026-11-07","hour":"","quarter":3,"year":2026,"epsEstimate":null,"epsActual' |
| Analyst / earnings | FMP grades-consensus (WDFC) | FAIL | PREMIUM-GATED: HTTP 402, 215 bytes, items=0, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription please visit our s" |
| Analyst / earnings | FMP price-target-consensus (WDFC) | FAIL | PREMIUM-GATED: HTTP 402, 215 bytes, items=0, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription please visit our s" |
| Analyst / earnings | FMP grades (WDFC) | FAIL | PREMIUM-GATED: HTTP 402, 215 bytes, items=0, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription please visit our s" |
| Analyst / earnings | FMP analyst-estimates (WDFC) | FAIL | PREMIUM-GATED: HTTP 402, 215 bytes, items=0, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription please visit our s" |
| Analyst / earnings | FMP earnings-calendar (all) | ok | HTTP 200, 16,255 bytes, items=78, '[\n  {\n    "symbol": "META",\n    "date": "2026-10-21",\n    "epsActual": 7.4,\n    "epsEstimated": 6.54,\n    "revenueActual": 64601000000,\n    ' |
| Analyst / earnings | FMP historical-price-eod/light (WDFC) | FAIL | PREMIUM-GATED: HTTP 402, 215 bytes, items=0, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription please visit our s" |
| Analyst | Alpha Vantage OVERVIEW (target + ratings) | skipped | no ALPHAVANTAGE_API_KEY set |
| Committee membership | unitedstates/congress-legislators | ok | HTTP 200, 292,419 bytes |
| Congress trades | LuxAlgo market-trackers-data | FAIL | rows=175, lastIngested=2026-09-07T15:11:27.497Z, generated=2026-09-07T15:13:09.121Z |
| Congress trades (raw) | House Clerk FD zip | ok | HTTP 200, 64,502 bytes |
| Congress trades (raw) | Senate eFD search | ok | HTTP 200, 11,880 bytes, needs agreement+CSRF session |
| Congress trades | Bargo free API | FAIL | HTTP 403, 5,515 bytes, '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title><meta http-equiv="Content-Type" content="text/htm' |
| Executive branch trades | OGE 278-T index | ok | HTTP 200, 97,967 bytes, 0 PDF links on index page (PDF-only, no structured feed) |
| Macro calendar | Fed FOMC calendar page | ok | HTTP 200, 165,751 bytes |
| Macro calendar | BLS release schedule (ics) | FAIL | HTTP 403, 1,325 bytes |
| Macro calendar | FRED release dates | ok | HTTP 200, 6,717 bytes, 50 upcoming release dates |
| Earnings dates | yfinance .calendar | ok | MSFT next earnings [datetime.date(2026, 10, 28)], ex-div 2026-11-19 |
| Notifications | Telegram getMe | skipped | no TELEGRAM_BOT_TOKEN set |
| Analyst | yfinance .recommendations | ok | populated 20/20 |
| Analyst | yfinance .recommendations_summary | ok | populated 20/20 |
| Analyst | yfinance .analyst_price_targets | ok | populated 20/20 |
| Analyst | yfinance .upgrades_downgrades | ok | populated 20/20 |
| Analyst | yfinance .earnings_estimate | ok | populated 20/20 |
| Analyst | yfinance .revenue_estimate | ok | populated 20/20 |
| Analyst | yfinance .eps_trend | ok | populated 20/20 |
| Analyst | yfinance .eps_revisions | ok | populated 20/20 |

Universe (IWB ∪ IVV ∪ IJH ∪ IJR, raw tickers before cleanup): **1654**
