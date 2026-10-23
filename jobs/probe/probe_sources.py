#!/usr/bin/env python3
"""Phase 1 data-source probe.

Hits every candidate source once (lightly), records whether it works from
*this* machine, and measures yfinance analyst-field coverage across 20 tickers.
Run it in three places and compare: this workspace, a GitHub Actions runner,
and Tom's own computer.

    PROBE_ENV=runner python jobs/probe/probe_sources.py --out probe-results

Keys are read from the environment (all optional; probes without a key are
reported as "skipped: no key"):
    SEC_USER_AGENT        e.g. "Stock Research tsnell39@gmail.com"  (required by SEC)
    FINNHUB_API_KEY  FMP_API_KEY  ALPHAVANTAGE_API_KEY  TIINGO_API_KEY
    STOOQ_API_KEY    FRED_API_KEY  BARGO_API_KEY  TELEGRAM_BOT_TOKEN

No data is stored beyond the small result files; nothing goes to a database.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# Load a local .env (repo root) without adding a dependency; real env vars win.
_dotenv = Path(__file__).resolve().parents[2] / ".env"
if _dotenv.exists():
    for _ln in _dotenv.read_text().splitlines():
        if "=" in _ln and not _ln.lstrip().startswith("#"):
            _k, _v = _ln.split("=", 1)
            if _v.strip().strip('"'):
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

ENV =os.environ.get("PROBE_ENV", "local")
UA = os.environ.get("SEC_USER_AGENT", "Personal Stock Research (set SEC_USER_AGENT)")
TIMEOUT = 30

# 20 tickers across sizes and the special sectors (bank, insurer, REIT, utility)
ANALYST_TICKERS = [
    # large caps
    "AAPL", "MSFT", "NVDA", "JPM", "XOM", "JNJ", "O", "NEE", "PGR", "WFC",
    # mid caps
    "TOL", "RRX", "SAIA", "EXP", "WING",
    # small caps
    "CALM", "AAON", "WDFC", "PLMR", "ALG",
]

ISHARES = {
    "IWB": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    "IVV": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
    "IJH": "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund",
    "IJR": "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund",
}

results: list[dict] = []


def record(need: str, source: str, ok: bool | None, detail: str, **extra) -> None:
    status = "skipped" if ok is None else ("ok" if ok else "FAIL")
    row = {"need": need, "source": source, "status": status, "detail": detail[:400], **extra}
    results.append(row)
    print(f"[{status:7}] {need:28} {source:28} {detail[:110]}", flush=True)


def get(url: str, *, headers: dict | None = None, params: dict | None = None, method: str = "GET",
        stream: bool = False) -> requests.Response:
    h = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
    h.update(headers or {})
    return requests.request(method, url, headers=h, params=params, timeout=TIMEOUT, stream=stream)


def probe(need: str, source: str, key_env: str | None = None):
    """Decorator: run fn, catch everything, record result. fn returns (ok, detail, extra)."""
    def wrap(fn):
        if key_env and not os.environ.get(key_env):
            record(need, source, None, f"no {key_env} set")
            return fn
        t0 = time.time()
        try:
            ok, detail, *rest = fn()
            extra = rest[0] if rest else {}
            record(need, source, ok, detail, seconds=round(time.time() - t0, 2), **extra)
        except Exception as exc:  # noqa: BLE001
            record(need, source, False, f"{type(exc).__name__}: {exc}", seconds=round(time.time() - t0, 2))
        return fn
    return wrap


def http_detail(r: requests.Response) -> str:
    return f"HTTP {r.status_code}, {len(r.content):,} bytes"


# ---------------------------------------------------------------- SEC EDGAR
def run_sec() -> None:
    @probe("Ticker→CIK map", "SEC company_tickers.json")
    def _():
        r = get("https://www.sec.gov/files/company_tickers.json")
        n = len(r.json()) if r.ok else 0
        return r.ok and n > 5000, f"{http_detail(r)}, {n:,} tickers"

    @probe("Industry (SIC)", "EDGAR submissions")
    def _():
        r = get("https://data.sec.gov/submissions/CIK0000320193.json")
        j = r.json() if r.ok else {}
        return r.ok and bool(j.get("sic")), f"{http_detail(r)}, AAPL SIC={j.get('sic')} {j.get('sicDescription')}"

    @probe("Fundamentals", "EDGAR companyfacts (per co.)")
    def _():
        r = get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
        j = r.json() if r.ok else {}
        n = len(j.get("facts", {}).get("us-gaap", {}))
        return r.ok and n > 100, f"{http_detail(r)}, {n} us-gaap concepts for AAPL"

    @probe("Fundamentals", "EDGAR frames API")
    def _():
        r = get("https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024.json")
        j = r.json() if r.ok else {}
        n = len(j.get("data", []))
        return r.ok and n > 1000, f"{http_detail(r)}, {n:,} filers report Revenues CY2024"

    @probe("Fundamentals (bulk)", "EDGAR companyfacts.zip")
    def _():
        r = get("https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip", method="HEAD")
        size = int(r.headers.get("Content-Length", 0))
        return r.ok and size > 100e6, f"HTTP {r.status_code}, {size/1e9:.2f} GB, Last-Modified {r.headers.get('Last-Modified')}"

    @probe("Insider trades", "EDGAR Form 4 (current feed)")
    def _():
        r = get("https://www.sec.gov/cgi-bin/browse-edgar",
                params={"action": "getcurrent", "type": "4", "count": "40", "output": "atom"})
        n = r.text.count("<entry>") if r.ok else 0
        return r.ok and n > 0, f"{http_detail(r)}, {n} recent Form 4 entries"

    @probe("Insider/13F/13D index", "EDGAR daily form index")
    def _():
        # most recent weekday's index; walk back up to 6 days for holidays
        for back in range(1, 7):
            d = date.today() - timedelta(days=back)
            q = (d.month - 1) // 3 + 1
            url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"
            r = get(url)
            if r.ok:
                txt = r.text
                c = {k: sum(1 for ln in txt.splitlines() if ln.startswith(k + " ")) for k in ("4", "13F-HR", "SC 13D", "SC 13G")}
                return True, f"{d}: Form4={c['4']}, 13F-HR={c['13F-HR']}, 13D={c['SC 13D']}, 13G={c['SC 13G']}"
        return False, f"no daily index found in last 6 days (last HTTP {r.status_code})"


# ---------------------------------------------------------------- universe
def run_ishares() -> None:
    for etf, url in ISHARES.items():
        @probe("Universe + GICS sector", f"iShares {etf} holdings CSV")
        def _(etf=etf, url=url):
            r = get(url, headers={"User-Agent": "Mozilla/5.0"})
            if not r.ok:
                return False, http_detail(r)
            text = r.content.decode("utf-8-sig", errors="replace").lstrip("\ufeff")
            lines = text.splitlines()
            hdr = next((i for i, ln in enumerate(lines) if ln.replace('"', '').lstrip("\ufeff ").startswith("Ticker,")), None)
            if hdr is None:
                head = " || ".join(lines[:12])[:380]
                return False, f"{http_detail(r)}, ctype={r.headers.get('Content-Type')}, header row not found; head: {head!r}"
            rows = list(csv.DictReader(io.StringIO("\n".join(lines[hdr:]))))
            eq = [x for x in rows if x.get("Asset Class") == "Equity"]
            sectors = sorted({x.get("Sector", "") for x in eq})
            return len(eq) > 300, f"{len(eq)} equities, {len(sectors)} sectors", {"tickers": [x["Ticker"] for x in eq]}


# ---------------------------------------------------------------- prices
def run_prices() -> None:
    try:
        import yfinance as yf
    except ImportError:
        record("Daily prices", "yfinance", False, "yfinance not installed")
        yf = None

    if yf:
        @probe("Daily prices", "yfinance history (3y)")
        def _():
            df = yf.Ticker("AAPL").history(period="3y", auto_adjust=False)
            return len(df) > 700, f"{len(df)} rows, last {df.index[-1].date() if len(df) else None}"

        @probe("Daily prices (batch)", "yfinance download 20 tickers")
        def _():
            df = yf.download(ANALYST_TICKERS, period="5d", progress=False, threads=False)
            got = int(df["Close"].notna().any().sum()) if len(df) else 0
            return got >= 18, f"{got}/20 tickers returned closes"

    @probe("Daily prices", "Tiingo", "TIINGO_API_KEY")
    def _():
        r = get("https://api.tiingo.com/tiingo/daily/AAPL/prices",
                params={"startDate": str(date.today() - timedelta(days=10)), "token": os.environ["TIINGO_API_KEY"]})
        return r.ok and len(r.json()) > 0, http_detail(r)

    @probe("Daily prices", "Stooq (API key since 2026)", "STOOQ_API_KEY")
    def _():
        r = get("https://stooq.com/q/d/l/", params={"s": "aapl.us", "i": "d", "apikey": os.environ["STOOQ_API_KEY"]})
        return r.ok and r.text.startswith("Date"), f"{http_detail(r)}, starts: {r.text[:40]!r}"

    @probe("Daily prices", "Alpha Vantage", "ALPHAVANTAGE_API_KEY")
    def _():
        r = get("https://www.alphavantage.co/query",
                params={"function": "TIME_SERIES_DAILY", "symbol": "AAPL", "apikey": os.environ["ALPHAVANTAGE_API_KEY"]})
        j = r.json()
        return "Time Series (Daily)" in j, f"{http_detail(r)}, keys={list(j)[:3]}"


# ---------------------------------------------------------------- analyst (priority)
ANALYST_FIELDS = ["recommendations", "recommendations_summary", "analyst_price_targets", "upgrades_downgrades",
                  "earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"]


def _populated(v) -> bool:
    if v is None:
        return False
    if hasattr(v, "empty"):
        return not v.empty
    if isinstance(v, dict):
        return any(x is not None for x in v.values())
    return bool(v)


def run_analyst_yfinance() -> dict:
    try:
        import yfinance as yf
    except ImportError:
        record("Analyst", "yfinance", False, "yfinance not installed")
        return {}
    matrix: dict[str, dict[str, bool | str]] = {}
    for tk in ANALYST_TICKERS:
        t = yf.Ticker(tk)
        row: dict[str, bool | str] = {}
        for f in ANALYST_FIELDS:
            try:
                row[f] = _populated(getattr(t, f))
            except Exception as exc:  # noqa: BLE001
                row[f] = f"ERR {type(exc).__name__}"
            time.sleep(0.4)  # be gentle; this is ~160 requests
        # analyst count, for the thin-coverage badge
        try:
            info = t.info
            row["numberOfAnalystOpinions"] = info.get("numberOfAnalystOpinions")
        except Exception as exc:  # noqa: BLE001
            row["numberOfAnalystOpinions"] = f"ERR {type(exc).__name__}"
        matrix[tk] = row
        if len(matrix) == 2 and all(isinstance(v, str) for r in matrix.values() for k, v in r.items() if k in ANALYST_FIELDS):
            print("    yfinance unreachable from here (all errors on first 2 tickers); stopping matrix early", flush=True)
            break
        print(f"    yfinance analyst {tk:5} " + " ".join("Y" if row[f] is True else ("e" if isinstance(row[f], str) else ".") for f in ANALYST_FIELDS), flush=True)
    for f in ANALYST_FIELDS:
        n = sum(1 for r in matrix.values() if r[f] is True)
        errs = sum(1 for r in matrix.values() if isinstance(r[f], str))
        tot = len(matrix)
        record("Analyst", f"yfinance .{f}", n >= 0.8 * tot, f"populated {n}/{tot}" + (f", errors {errs}" if errs else ""), populated=n)
    return matrix


def run_analyst_backups() -> None:
    fk = os.environ.get("FINNHUB_API_KEY")
    for ep, params in [("stock/recommendation", {"symbol": "AAPL"}),
                       ("stock/price-target", {"symbol": "AAPL"}),
                       ("stock/upgrade-downgrade", {"symbol": "AAPL"}),
                       ("stock/eps-estimate", {"symbol": "AAPL", "freq": "quarterly"}),
                       ("calendar/earnings", {"from": str(date.today()), "to": str(date.today() + timedelta(days=14))})]:
        @probe("Analyst / earnings", f"Finnhub {ep}", "FINNHUB_API_KEY")
        def _(ep=ep, params=params):
            r = get(f"https://finnhub.io/api/v1/{ep}", params={**params, "token": fk})
            body = r.text[:120]
            # 403 "You don't have access to this resource." = premium-gated
            gated = r.status_code == 403 or "access" in body.lower()
            data = r.json() if r.ok else None
            n = len(data) if isinstance(data, list) else (len(data.get("earningsCalendar", [])) if isinstance(data, dict) and "earningsCalendar" in data else (1 if data else 0))
            if ep == "stock/recommendation" and isinstance(data, list):
                months = len(data)
                return r.ok and months > 0, f"{http_detail(r)}, {months} monthly periods of history", {"history_months": months}
            return r.ok and n > 0 and not gated, ("PREMIUM-GATED: " if gated else "") + f"{http_detail(r)}, items={n}, {body!r}"

    mk = os.environ.get("FMP_API_KEY")
    for ep, sym in [("grades-consensus", "WDFC"), ("price-target-consensus", "WDFC"), ("grades", "WDFC"),
                    ("analyst-estimates", "WDFC"), ("earnings-calendar", None), ("historical-price-eod/light", "WDFC")]:
        @probe("Analyst / earnings", f"FMP {ep} ({sym or 'all'})", "FMP_API_KEY")
        def _(ep=ep, sym=sym):
            # WDFC (small cap) on purpose: FMP's free tier has restricted some endpoints to large caps
            params = {"apikey": mk}
            if sym:
                params["symbol"] = sym
            if ep == "analyst-estimates":
                params.update(period="annual", limit=2)
            r = get(f"https://financialmodelingprep.com/stable/{ep}", params=params)
            body = r.text[:140]
            gated = r.status_code in (402, 403) or "premium" in body.lower() or "subscription" in body.lower()
            data = r.json() if r.ok and r.text.startswith(("[", "{")) else None
            n = len(data) if isinstance(data, list) else (1 if data else 0)
            return r.ok and n > 0 and not gated, ("PREMIUM-GATED: " if gated else "") + f"{http_detail(r)}, items={n}, {body!r}"

    @probe("Analyst", "Alpha Vantage OVERVIEW (target + ratings)", "ALPHAVANTAGE_API_KEY")
    def _():
        r = get("https://www.alphavantage.co/query",
                params={"function": "OVERVIEW", "symbol": "WDFC", "apikey": os.environ["ALPHAVANTAGE_API_KEY"]})
        j = r.json()
        keys = [k for k in j if k.startswith("Analyst")]
        return bool(keys) and j.get("AnalystTargetPrice") not in (None, "None"), f"{http_detail(r)}, {keys}"


# ---------------------------------------------------------------- congress / politics
def run_politics() -> None:
    @probe("Committee membership", "unitedstates/congress-legislators")
    def _():
        r = get("https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml")
        return r.ok and len(r.content) > 50_000, http_detail(r)

    @probe("Congress trades", "LuxAlgo market-trackers-data")
    def _():
        r = get("https://raw.githubusercontent.com/LuxAlgo/market-trackers-data/main/manifest.json")
        j = r.json() if r.ok else {}
        ds = j.get("datasets", {}).get("congress-trades", {})
        return r.ok and ds.get("rows", 0) > 1000, f"rows={ds.get('rows')}, lastIngested={ds.get('lastIngestedAt')}, generated={j.get('generatedAt')}"

    @probe("Congress trades (raw)", "House Clerk FD zip")
    def _():
        r = get(f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{date.today().year}FD.zip", method="HEAD")
        return r.ok, f"HTTP {r.status_code}, {int(r.headers.get('Content-Length', 0)):,} bytes"

    @probe("Congress trades (raw)", "Senate eFD search")
    def _():
        r = get("https://efdsearch.senate.gov/search/home/", headers={"User-Agent": "Mozilla/5.0"})
        return r.ok and "csrfmiddlewaretoken" in r.text, f"{http_detail(r)}, needs agreement+CSRF session"

    @probe("Congress trades", "Bargo free API")
    def _():
        h = {"X-Api-Key": os.environ["BARGO_API_KEY"]} if os.environ.get("BARGO_API_KEY") else {}
        r = get("https://www.bargo.ai/free-apis/congress/v1/trades", params={"limit": 5}, headers=h)
        return r.ok, f"{http_detail(r)}, {r.text[:120]!r}"

    @probe("Executive branch trades", "OGE 278-T index")
    def _():
        r = get("https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView", headers={"User-Agent": "Mozilla/5.0"})
        pdfs = r.text.count(".pdf") if r.ok else 0
        return r.ok, f"{http_detail(r)}, {pdfs} PDF links on index page (PDF-only, no structured feed)"


# ---------------------------------------------------------------- calendars
def run_calendars() -> None:
    @probe("Macro calendar", "Fed FOMC calendar page")
    def _():
        r = get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers={"User-Agent": "Mozilla/5.0"})
        return r.ok and "FOMC" in r.text, http_detail(r)

    @probe("Macro calendar", "BLS release schedule (ics)")
    def _():
        r = get("https://www.bls.gov/schedule/news_release/bls.ics", headers={"User-Agent": "Mozilla/5.0"})
        return r.ok and "BEGIN:VCALENDAR" in r.text, http_detail(r)

    @probe("Macro calendar", "FRED release dates", "FRED_API_KEY")
    def _():
        r = get("https://api.stlouisfed.org/fred/releases/dates",
                params={"api_key": os.environ["FRED_API_KEY"], "file_type": "json", "include_release_dates_with_no_data": "true",
                        "realtime_start": str(date.today()), "limit": 50})
        n = len(r.json().get("release_dates", [])) if r.ok else 0
        return r.ok and n > 0, f"{http_detail(r)}, {n} upcoming release dates"

    try:
        import yfinance as yf

        @probe("Earnings dates", "yfinance .calendar")
        def _():
            c = yf.Ticker("MSFT").calendar
            ed = c.get("Earnings Date") if isinstance(c, dict) else None
            return bool(ed), f"MSFT next earnings {ed}, ex-div {c.get('Ex-Dividend Date') if isinstance(c, dict) else None}"
    except ImportError:
        pass


# ---------------------------------------------------------------- infra
def run_infra() -> None:
    @probe("Notifications", "Telegram getMe", "TELEGRAM_BOT_TOKEN")
    def _():
        r = get(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getMe")
        return r.ok and r.json().get("ok"), http_detail(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="probe-results")
    ap.add_argument("--skip-analyst-matrix", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    print(f"Probe env={ENV} host={platform.node()} python={sys.version.split()[0]} at {started:%Y-%m-%d %H:%M}Z\n")
    sections = [run_sec, run_ishares, run_prices, run_analyst_backups, run_politics, run_calendars, run_infra]
    for fn in sections:
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    matrix = {} if args.skip_analyst_matrix else run_analyst_yfinance()

    # universe union, if iShares worked
    ish = [r for r in results if r["source"].startswith("iShares") and r["status"] == "ok"]
    universe = sorted({t for r in ish for t in r.pop("tickers", [])})
    for r in results:
        r.pop("tickers", None)

    payload = {"env": ENV, "host": platform.node(), "started": started.isoformat(),
               "results": results, "analyst_matrix": matrix,
               "universe_count": len(universe) if len(ish) == 4 else None}
    (out / f"probe-{ENV}.json").write_text(json.dumps(payload, indent=1, default=str))

    md = [f"# Source probe: `{ENV}` ({started:%Y-%m-%d %H:%M} UTC)\n",
          "| Need | Source | Status | Detail |", "|---|---|---|---|"]
    md += [f"| {r['need']} | {r['source']} | {r['status']} | {r['detail'].replace('|', '/')} |" for r in results]
    if payload["universe_count"]:
        md.append(f"\nUniverse (IWB ∪ IVV ∪ IJH ∪ IJR, raw tickers before cleanup): **{payload['universe_count']}**")
    (out / f"probe-{ENV}.md").write_text("\n".join(md) + "\n")

    ok = sum(r["status"] == "ok" for r in results)
    fail = sum(r["status"] == "FAIL" for r in results)
    skip = sum(r["status"] == "skipped" for r in results)
    print(f"\n{ok} ok, {fail} failed, {skip} skipped -> {out}/probe-{ENV}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
