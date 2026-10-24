#!/usr/bin/env python3
"""Prototype build (top-N demo).

Full universe (~1,650 stocks): fundamentals + current valuation, so every metric is ranked
against real sector peers. Top N by S&P 500 weight: full detail (prices + MAs, analyst data,
Form 4 insider trades, own-history valuation, share-count history, calendar).

Output: one JSON file (default demo-data/demo.json). Nothing goes to a database yet.

    SEC_USER_AGENT="..." FRED_API_KEY=... python jobs/demo/build.py --top 100
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.lib.form4 import cluster_buys, parse_form4  # noqa: E402
from jobs.lib.metrics import Fundamentals, metric_set_for  # noqa: E402
from jobs.lib.scoring import own_history_pct, score_rows  # noqa: E402
from jobs.lib.xbrl import CompanyFacts  # noqa: E402

try:
    import orjson
    loads = orjson.loads
except ImportError:  # pragma: no cover
    loads = json.loads

TODAY = date.today()
CFG = yaml.safe_load((ROOT / "config/scoring.yaml").read_text())
OVR = yaml.safe_load((ROOT / "config/overrides.yaml").read_text())
UA = os.environ.get("SEC_USER_AGENT", "")
ISHARES = {"IWB": 239707, "IVV": 239726, "IJH": 239763, "IJR": 239774}
HEALTH: list[dict] = []
T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def health(kind: str, detail: str) -> None:
    HEALTH.append({"kind": kind, "detail": detail[:300]})
    log(f"HEALTH {kind}: {detail[:200]}")


def clean(x):
    """JSON-safe: NaN/inf -> None, numpy -> python, round floats."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        try:
            x = x.item()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return float(f"{x:.6g}")
    if isinstance(x, (date, datetime)):
        return x.isoformat()[:10]
    return x


# ------------------------------------------------------------------ SEC client
class SEC:
    def __init__(self, rps: float = 8.0) -> None:
        if "@" not in UA:
            raise SystemExit("SEC_USER_AGENT must include a contact email")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        self.gap = 1.0 / rps
        self.last = 0.0
        self.calls = 0

    def get(self, url: str, stream: bool = False) -> requests.Response:
        for attempt in range(5):
            wait = self.gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            try:
                r = self.s.get(url, timeout=120, stream=stream)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                if attempt == 4:
                    raise
                continue
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r


# ------------------------------------------------------------------ universe
def load_universe() -> dict[str, dict]:
    uni: dict[str, dict] = {}
    for etf, pid in ISHARES.items():
        url = f"https://www.ishares.com/us/products/{pid}/x/latest-holdings.csv"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        r.raise_for_status()
        lines = r.content.decode("utf-8-sig", errors="replace").splitlines()
        hdr = next(i for i, ln in enumerate(lines) if ln.replace('"', "").startswith("Ticker,"))
        rows = list(csv.DictReader(io.StringIO("\n".join(lines[hdr:]))))
        n = 0
        for x in rows:
            if x.get("Asset Class") != "Equity" or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", x.get("Ticker", "")):
                continue
            t = x["Ticker"]
            u = uni.setdefault(t, {"ticker": t, "name": x["Name"].title(), "sector": x["Sector"], "etfs": [], "ivv_weight": 0.0})
            u["etfs"].append(etf)
            if etf == "IVV":
                u["ivv_weight"] = float(x["Weight (%)"].replace(",", "") or 0)
            n += 1
        log(f"universe {etf}: {n} equities")
    log(f"universe union: {len(uni)} tickers")
    return uni


def map_ciks(uni: dict, sec: SEC) -> None:
    tick = sec.get("https://www.sec.gov/files/company_tickers.json").json()
    m = {v["ticker"].upper(): (int(v["cik_str"]), v["title"]) for v in tick.values()}
    aliases = OVR.get("ticker_aliases") or {}
    missing = []
    for t, u in uni.items():
        cands = [aliases.get(t), t, t.replace(".", "-"), (t[:-1] + "-" + t[-1]) if len(t) > 2 else None]
        hit = next((c for c in cands if c and c in m), None)
        if hit:
            u["sec_ticker"] = hit
            u["cik"] = m[hit][0]
        else:
            missing.append(t)
    if missing:
        health("cik_unmapped", f"{len(missing)} tickers without SEC CIK: {', '.join(missing[:40])}")


# ------------------------------------------------------------------ prices (yfinance)
def download_prices(tickers: list[str], period: str, chunk: int = 150) -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf
    closes, splits = [], []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, auto_adjust=True, actions=True, progress=False,
                                 threads=True, group_by="column")
                break
            except Exception as e:  # noqa: BLE001
                log(f"yf.download retry {attempt}: {e}")
                time.sleep(20 * (attempt + 1))
        else:
            health("prices", f"chunk {i} failed")
            continue
        if df.empty:
            continue
        c = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": part[0]})
        closes.append(c)
        if isinstance(df.columns, pd.MultiIndex) and "Stock Splits" in df.columns.get_level_values(0):
            splits.append(df["Stock Splits"])
        time.sleep(2)
    close = pd.concat(closes, axis=1) if closes else pd.DataFrame()
    close = close.loc[:, ~close.columns.duplicated()]
    close = close.dropna(how="all")
    spl = pd.concat(splits, axis=1) if splits else pd.DataFrame()
    spl = spl.loc[:, ~spl.columns.duplicated()] if not spl.empty else spl
    return close, spl


def split_list(spl: pd.DataFrame, t: str) -> list[tuple[date, float]]:
    if spl.empty or t not in spl.columns:
        return []
    s = spl[t].fillna(0)
    return [(d.date(), float(v)) for d, v in s[s > 0].items()]


def split_factor(splits: list[tuple[date, float]], after: date) -> float:
    f = 1.0
    for d, r in splits:
        if d > after:
            f *= r
    return f


# ------------------------------------------------------------------ yfinance analyst
def yahoo_detail(t: str) -> dict:
    import yfinance as yf
    tk = yf.Ticker(t)
    out: dict = {}

    def grab(name, fn):
        for attempt in range(3):
            try:
                out[name] = fn()
                return
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "Rate" in msg or "Too Many" in msg:
                    time.sleep(30 * (attempt + 1))
                    continue
                out[name] = None
                out.setdefault("_errors", []).append(f"{name}: {type(e).__name__}")
                return
        out[name] = None
        out.setdefault("_errors", []).append(f"{name}: rate limited")

    grab("info", lambda: tk.info)
    grab("news", lambda: (tk.get_news(count=12) if hasattr(tk, "get_news") else tk.news))
    for name in ["recommendations_summary", "analyst_price_targets", "upgrades_downgrades", "earnings_estimate",
                 "eps_trend", "eps_revisions", "calendar"]:
        grab(name, lambda n=name: getattr(tk, n))
        time.sleep(0.25)
    return out


def analyst_block(y: dict, price: float | None) -> tuple[dict, dict]:
    """Returns (display block, metric values for scoring)."""
    info = y.get("info") or {}
    n = info.get("numberOfAnalystOpinions")
    block: dict = {"n_analysts": n, "rating_mean": info.get("recommendationMean"),
                   "rating_key": info.get("recommendationKey"), "errors": y.get("_errors", [])}
    metrics: dict = {}
    rs = y.get("recommendations_summary")
    if rs is not None and hasattr(rs, "empty") and not rs.empty:
        row = rs.iloc[0]
        block["ratings"] = {k: int(row.get(k, 0) or 0) for k in ["strongBuy", "buy", "hold", "sell", "strongSell"]}
        block["ratings_history"] = [{"period": r["period"], **{k: int(r.get(k, 0) or 0) for k in ["strongBuy", "buy", "hold", "sell", "strongSell"]}}
                                    for _, r in rs.iterrows()]
    pt = y.get("analyst_price_targets") or {}
    if isinstance(pt, dict) and pt.get("mean"):
        block["targets"] = {k: pt.get(k) for k in ["low", "mean", "median", "high"]}
        if price:
            metrics["target_upside"] = pt["mean"] / price - 1
    if block["rating_mean"] is not None:
        metrics["rating_mean"] = float(block["rating_mean"])
    et = y.get("eps_trend")
    if et is not None and hasattr(et, "empty") and not et.empty:
        block["eps_trend"] = {str(idx): {c: et.loc[idx, c] for c in ["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"] if c in et.columns}
                              for idx in et.index}
        if "0y" in et.index:
            cur, ago = et.loc["0y", "current"], et.loc["0y", "90daysAgo"]
            if cur is not None and ago and not pd.isna(cur) and not pd.isna(ago) and ago != 0:
                metrics["eps_rev_90d"] = cur / ago - 1 if ago > 0 else None
    er = y.get("eps_revisions")
    if er is not None and hasattr(er, "empty") and not er.empty:
        block["eps_revisions"] = {str(idx): {c: er.loc[idx, c] for c in er.columns if c != "currency"} for idx in er.index}
        if "0y" in er.index and n:
            up, dn = er.loc["0y"].get("upLast30days", 0) or 0, er.loc["0y"].get("downLast30days", 0) or 0
            metrics["eps_revisions_net_30d"] = (up - dn) / n
    ee = y.get("earnings_estimate")
    if ee is not None and hasattr(ee, "empty") and not ee.empty:
        block["earnings_estimate"] = {str(idx): {c: ee.loc[idx, c] for c in ["avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth"] if c in ee.columns}
                                      for idx in ee.index}
    ud = y.get("upgrades_downgrades")
    if ud is not None and hasattr(ud, "empty") and not ud.empty:
        ud = ud.copy()
        ud.index = pd.to_datetime(ud.index)
        recent = ud[ud.index >= pd.Timestamp(TODAY - timedelta(days=365))]
        block["actions"] = [{"date": d.date().isoformat(), "firm": r.get("Firm"), "action": r.get("Action"),
                             "to": r.get("ToGrade"), "from": r.get("FromGrade"),
                             "pt": r.get("currentPriceTarget") or None, "pt_prior": r.get("priorPriceTarget") or None,
                             "pt_action": r.get("priceTargetAction")}
                            for d, r in recent.sort_index(ascending=False).iterrows()][:60]
        last90 = ud[ud.index >= pd.Timestamp(TODAY - timedelta(days=90))]
        ups, downs = int((last90["Action"] == "up").sum()), int((last90["Action"] == "down").sum())
        block["upgrades_90d"], block["downgrades_90d"] = ups, downs
        if n:
            metrics["net_upgrades_90d"] = (ups - downs) / n
    block["thin_coverage"] = bool(n is not None and n < CFG["thin_coverage_analysts"])
    return block, metrics


def news_block(y: dict) -> list[dict]:
    """Yahoo news items -> {title, publisher, date, url, summary}. Handles old and new yfinance shapes."""
    out = []
    for it in (y.get("news") or [])[:12]:
        if not isinstance(it, dict):
            continue
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        title = c.get("title")
        if not title:
            continue
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("link"))
        pub = (c.get("provider") or {}).get("displayName") or c.get("publisher")
        d = c.get("pubDate") or c.get("displayTime")
        if not d and c.get("providerPublishTime"):
            d = datetime.fromtimestamp(c["providerPublishTime"], timezone.utc).isoformat()
        out.append({"title": title, "publisher": pub, "date": (d or "")[:10], "url": url,
                    "summary": (c.get("summary") or c.get("description") or "")[:500]})
    return out


def finnhub_news(t: str, name: str, days: int = 14, keep: int = 12) -> list[dict]:
    """Company news from Finnhub's free tier. Feeds include loosely related stories, so prefer items
    whose headline or summary mentions the ticker or the company's first name word."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    sym = t.replace("-", ".")
    try:
        r = requests.get("https://finnhub.io/api/v1/company-news", timeout=30,
                         params={"symbol": sym, "from": str(TODAY - timedelta(days=days)), "to": str(TODAY), "token": key})
        time.sleep(1.05)   # free tier: 60 calls/min
        items = r.json() if r.ok else []
    except Exception as e:  # noqa: BLE001
        health("news", f"{t}: {e}")
        return []
    if not isinstance(items, list):
        return []
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", name) if len(w) > 2 and w.lower() not in {"inc", "corp", "the", "class", "company", "holdings", "group"}]
    pat = re.compile(r"\b(" + "|".join(map(re.escape, [t.split("-")[0]] + words[:1])) + r")\b", re.I)
    seen, rel, other = set(), [], []
    for it in sorted(items, key=lambda x: -(x.get("datetime") or 0)):
        h = (it.get("headline") or "").strip()
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        row = {"title": h, "publisher": it.get("source"), "url": it.get("url"),
               "date": datetime.fromtimestamp(it.get("datetime") or 0, timezone.utc).date().isoformat(),
               "summary": (it.get("summary") or "")[:500]}
        (rel if pat.search(h + " " + (it.get("summary") or "")) else other).append(row)
    return (rel + other)[:keep]


ITEMS_8K = {"1.01": "Material agreement", "1.02": "Agreement terminated", "1.05": "Cybersecurity incident",
            "2.01": "Acquisition or disposal completed", "2.02": "Earnings results", "2.03": "New debt obligation",
            "2.05": "Restructuring / exit costs", "2.06": "Impairment", "3.01": "Listing notice", "3.02": "Unregistered share sale",
            "4.01": "Auditor change", "4.02": "Prior financials unreliable", "5.01": "Change in control",
            "5.02": "Executive or director change", "5.03": "Charter or bylaw change", "5.07": "Shareholder vote results",
            "7.01": "Reg FD disclosure", "8.01": "Other events"}


def filings_block(cik: int, sub: dict, days: int = 120) -> list[dict]:
    rec = sub.get("filings", {}).get("recent", {})
    cutoff = (TODAY - timedelta(days=days)).isoformat()
    out = []
    for form, accn, filed, doc, items in zip(rec.get("form", []), rec.get("accessionNumber", []), rec.get("filingDate", []),
                                            rec.get("primaryDocument", []), rec.get("items", [""] * len(rec.get("form", [])))):
        if filed < cutoff or form not in ("8-K", "10-K", "10-Q", "SC 13D", "SC 13D/A", "DEF 14A"):
            continue
        labels = [ITEMS_8K.get(i.strip(), "") for i in (items or "").split(",") if i.strip() and i.strip() != "9.01"]
        out.append({"form": form, "date": filed, "items": [l for l in labels if l],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"})
    return out[:15]


PROFILE_KEYS = ["longBusinessSummary", "industry", "website", "fullTimeEmployees", "beta", "fiftyTwoWeekHigh",
                "fiftyTwoWeekLow", "trailingPE", "forwardPE", "dividendYield", "averageVolume", "city", "state",
                "country", "regularMarketPrice", "regularMarketTime", "previousClose"]


def profile_block(y: dict) -> dict:
    info = y.get("info") or {}
    p = {k: info.get(k) for k in PROFILE_KEYS}
    if p.get("longBusinessSummary"):
        p["longBusinessSummary"] = p["longBusinessSummary"][:1500]
    return p


def calendar_block(y: dict) -> dict:
    c = y.get("calendar") or {}
    if not isinstance(c, dict):
        return {}
    ed = c.get("Earnings Date") or []
    return {"earnings": [d.isoformat() for d in ed if hasattr(d, "isoformat")],
            "ex_dividend": c.get("Ex-Dividend Date").isoformat() if hasattr(c.get("Ex-Dividend Date"), "isoformat") else None,
            "dividend_pay": c.get("Dividend Date").isoformat() if hasattr(c.get("Dividend Date"), "isoformat") else None,
            "eps_estimate": c.get("Earnings Average")}


# ------------------------------------------------------------------ insiders
def insider_block(sec: SEC, cik: int, sub: dict, days: int) -> dict:
    rec = sub.get("filings", {}).get("recent", {})
    cutoff = (TODAY - timedelta(days=days)).isoformat()
    trades, n_filings, errors = [], 0, 0
    for form, accn, filed, doc in zip(rec.get("form", []), rec.get("accessionNumber", []), rec.get("filingDate", []),
                                     rec.get("primaryDocument", [])):
        if form != "4" or filed < cutoff:
            continue
        raw = doc.split("/")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{raw}"
        try:
            trades += parse_form4(sec.get(url).content, accn, filed)
            n_filings += 1
        except Exception:  # noqa: BLE001
            errors += 1
    trades.sort(key=lambda t: (t["date"] or "", t["filed"]), reverse=True)
    buys = [t for t in trades if t["code"] == "P"]
    disc_sales = [t for t in trades if t["code"] == "S" and not t["plan_10b5_1"]]
    plan_sales = [t for t in trades if t["code"] == "S" and t["plan_10b5_1"]]
    return {"trades": trades[:300], "n_form4": n_filings, "parse_errors": errors,
            "clusters": cluster_buys(trades),
            "summary": {"buy_value": round(sum(t["value"] or 0 for t in buys), 2), "buys": len(buys),
                        "ceo_cfo_buys": sum(1 for t in buys if t["ceo"] or t["cfo"]),
                        "disc_sale_value": round(sum(t["value"] or 0 for t in disc_sales), 2), "disc_sales": len(disc_sales),
                        "plan_sale_value": round(sum(t["value"] or 0 for t in plan_sales), 2), "plan_sales": len(plan_sales)}}


# ------------------------------------------------------------------ technicals
def technicals(close: pd.Series) -> dict:
    s = close.dropna()
    if len(s) < 210:
        return {"signals": [], "state": None}
    sma50, sma200 = s.rolling(50).mean(), s.rolling(200).mean()
    sig = []
    above200 = (s > sma200)
    gold = (sma50 > sma200)
    for i in range(201, len(s)):
        d = s.index[i].date().isoformat()
        if pd.isna(sma200.iloc[i - 1]):
            continue
        if above200.iloc[i] != above200.iloc[i - 1]:
            sig.append({"date": d, "type": "cross_above_200" if above200.iloc[i] else "cross_below_200", "price": s.iloc[i]})
        if gold.iloc[i] != gold.iloc[i - 1]:
            sig.append({"date": d, "type": "golden_cross" if gold.iloc[i] else "death_cross", "price": s.iloc[i]})
    return {"signals": sig, "state": {"sma50": sma50.iloc[-1], "sma200": sma200.iloc[-1],
                                      "above_200": bool(above200.iloc[-1]), "golden": bool(gold.iloc[-1])}}


# ------------------------------------------------------------------ own-history valuation
def own_history(cf: CompanyFacts, sub: dict, close: pd.Series, splits: list, mcap_now: float, shares_adj_now: float | None,
                current: dict) -> tuple[dict, dict, dict]:
    """Daily valuation ratios over the last 3 years using only what had been *filed* by each day."""
    rec = sub.get("filings", {}).get("recent", {})
    start = TODAY - timedelta(days=3 * 365 + 10)
    kds = sorted({date.fromisoformat(f) for form, f in zip(rec.get("form", []), rec.get("filingDate", []))
                  if form in ("10-K", "10-Q", "10-K/A", "10-Q/A") and f >= (start - timedelta(days=200)).isoformat()})
    if not kds:
        return {}, {k: "no filing history" for k in current}, {}
    snaps = []
    for kd in kds:
        f = Fundamentals(cf, CFG["concepts"], kd, CFG)
        g = f.inputs
        so = cf.shares_outstanding(kd)
        sh_adj = so[0] * split_factor(splits, so[1]) if so else None
        tangible = None if g["equity"] is None else g["equity"] - (g["goodwill"] or 0) - (g["intangibles"] or 0)
        snaps.append((kd, {"net_income": g["net_income"], "fcf": g["fcf"], "revenue": g["revenue"], "ebitda": g["ebitda"],
                           "equity": g["equity"], "tangible": tangible, "ffo": g["ffo"],
                           "netdebt": None if g["debt"] is None or g["cash_total"] is None else g["debt"] - g["cash_total"],
                           "shares_adj": sh_adj}))
    s = close.dropna()
    s = s[s.index >= pd.Timestamp(start)]
    if s.empty:
        return {}, {k: "no price history" for k in current}, {}
    price_now = s.iloc[-1]
    series: dict[str, list[float]] = {k: [] for k in current}
    monthly: dict[str, list] = {"dates": [], "pe": []}
    j = -1
    for d, px in s.items():
        dd = d.date()
        while j + 1 < len(snaps) and snaps[j + 1][0] <= dd:
            j += 1
        if j < 0:
            continue
        g = snaps[j][1]
        share_ratio = (g["shares_adj"] / shares_adj_now) if g["shares_adj"] and shares_adj_now else 1.0
        mcap = mcap_now * (px / price_now) * share_ratio
        vals = {"pe": (mcap, g["net_income"]), "pfcf": (mcap, g["fcf"]), "ps": (mcap, g["revenue"]),
                "ev_ebitda": (None if g["netdebt"] is None else mcap + g["netdebt"], g["ebitda"]),
                "pb": (mcap, g["equity"]), "ptbv": (mcap, g["tangible"]), "pffo": (mcap, g["ffo"])}
        for k in series:
            num, den = vals.get(k, (None, None))
            series[k].append(num / den if num is not None and den and den > 0 else None)
        if dd.day <= 7 and (not monthly["dates"] or monthly["dates"][-1][:7] != dd.isoformat()[:7]):
            monthly["dates"].append(dd.isoformat())
            monthly["pe"].append(series["pe"][-1])
    own, own_na = {}, {}
    min_days = CFG["valuation_blend"]["own_history_min_days"] * 252 // 365
    for k, vals in series.items():
        valid = [v for v in vals if v is not None and v > 0]
        if current.get(k) is None:
            continue
        if len(valid) < min_days:
            own_na[k] = f"only {len(valid)} trading days of history (need {min_days})"
            continue
        own[k] = own_history_pct(valid, current[k])
    return own, own_na, monthly


# ------------------------------------------------------------------ macro calendar
def macro_calendar() -> list[dict]:
    ev: list[dict] = []
    try:
        html = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=60).text
        months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                              "September", "October", "November", "December"], 1)}
        for ym in re.finditer(r"(\d{4}) FOMC Meetings", html):
            year = int(ym.group(1))
            if year < TODAY.year:
                continue
            nxt = html.find("FOMC Meetings", ym.end())
            chunk = html[ym.end(): nxt if nxt > 0 else len(html)]
            for mm in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>.*?fomc-meeting__date[^>]*>\s*([0-9\-\*]+)', chunk, re.S):
                mon = mm.group(1).split("/")[-1]
                day = mm.group(2).replace("*", "").split("-")[-1]
                if mon in months and day.isdigit():
                    d = date(year, months[mon], int(day))
                    ev.append({"date": d.isoformat(), "type": "fomc", "label": "FOMC rate decision" + (" + projections" if "*" in mm.group(2) else "")})
        if not ev:
            health("macro", "FOMC page parsed but no meetings found (layout change?)")
    except Exception as e:  # noqa: BLE001
        health("macro", f"FOMC: {e}")
    key = os.environ.get("FRED_API_KEY")
    if key:
        for rid, label in [(10, "CPI"), (50, "Jobs report"), (54, "PCE (Personal Income & Outlays)"), (53, "GDP")]:
            try:
                r = requests.get("https://api.stlouisfed.org/fred/release/dates",
                                 params={"release_id": rid, "api_key": key, "file_type": "json",
                                         "include_release_dates_with_no_data": "true", "realtime_start": TODAY.isoformat(),
                                         "realtime_end": "9999-12-31", "sort_order": "asc", "limit": 12}, timeout=60)
                r.raise_for_status()
                for x in r.json().get("release_dates", []):
                    if x["date"] >= TODAY.isoformat():
                        ev.append({"date": x["date"], "type": "macro", "label": label})
            except Exception as e:  # noqa: BLE001
                health("macro", f"FRED {label}: {e}")
    else:
        health("macro", "FRED_API_KEY not set: CPI/jobs/PCE/GDP dates missing")
    ev = [e for e in ev if e["date"] <= (TODAY + timedelta(days=200)).isoformat()]
    return sorted({(e["date"], e["label"]): e for e in ev}.values(), key=lambda e: e["date"])


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--insider-days", type=int, default=365)
    ap.add_argument("--out", default="demo-data/demo.json")
    ap.add_argument("--zip", default="/tmp/companyfacts.zip")
    args = ap.parse_args()
    sec = SEC()

    uni = load_universe()
    map_ciks(uni, sec)
    mapped = {t: u for t, u in uni.items() if "cik" in u}
    # top N by S&P 500 weight, one share class per company
    top, seen = [], set()
    for t, u in sorted(mapped.items(), key=lambda kv: -kv[1]["ivv_weight"]):
        if u["cik"] in seen or u["ivv_weight"] <= 0:
            continue
        seen.add(u["cik"])
        top.append(t)
        if len(top) >= args.top:
            break
    top_set = set(top)
    log(f"top {len(top)}: {', '.join(top[:15])} ...")

    # submissions: SIC for everyone, filing lists for the top N
    subs: dict[str, dict] = {}
    for i, (t, u) in enumerate(mapped.items()):
        try:
            sub = sec.get(f"https://data.sec.gov/submissions/CIK{u['cik']:010d}.json").json()
            u["sic"] = int(sub.get("sic") or 0) or None
            u["sic_desc"] = sub.get("sicDescription")
            if t in top_set:
                subs[t] = sub
        except Exception as e:  # noqa: BLE001
            u["sic"] = None
            health("submissions", f"{t}: {e}")
        if i % 300 == 0:
            log(f"submissions {i}/{len(mapped)}")

    # prices
    ytick = {t: u.get("sec_ticker", t) for t, u in mapped.items()}
    log("prices: universe 1y")
    uclose, usplit = download_prices(sorted(set(ytick.values())), "1y")
    log(f"prices: universe got {uclose.shape[1]} tickers")
    log("prices: top 3y")
    tclose, tsplit = download_prices([ytick[t] for t in top], "3y", chunk=50)
    log(f"prices: top got {tclose.shape[1]} tickers, {len(tclose)} days")

    # yahoo analyst + calendar (top N)
    ydata: dict[str, dict] = {}
    for i, t in enumerate(top):
        ydata[t] = yahoo_detail(ytick[t])
        if i % 20 == 0:
            log(f"yahoo detail {i}/{len(top)}")

    # insiders (top N)
    insiders: dict[str, dict] = {}
    for i, t in enumerate(top):
        try:
            insiders[t] = insider_block(sec, mapped[t]["cik"], subs.get(t, {}), args.insider_days)
        except Exception as e:  # noqa: BLE001
            health("insiders", f"{t}: {e}")
            insiders[t] = {"trades": [], "clusters": [], "summary": {}, "error": str(e)}
        if i % 10 == 0:
            log(f"insiders {i}/{len(top)} (SEC calls so far {sec.calls})")

    # companyfacts bulk
    zp = Path(args.zip)
    if not zp.exists():
        log("downloading companyfacts.zip")
        with sec.get("https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip", stream=True) as r:
            with open(zp, "wb") as fh:
                for chunk in r.iter_content(1 << 22):
                    fh.write(chunk)
    log(f"companyfacts.zip {zp.stat().st_size / 1e9:.2f} GB")
    zf = zipfile.ZipFile(zp)
    names = set(zf.namelist())

    def read_cf(cik: int):
        n = f"CIK{cik:010d}.json"
        return loads(zf.read(n)) if n in names else None

    rows, detail, validation = [], {}, []
    preds = OVR.get("predecessor_ciks") or {}
    for i, (t, u) in enumerate(mapped.items()):
        try:
            raw = read_cf(u["cik"])
            if raw is None:
                health("xbrl_missing", f"{t}: no companyfacts")
                continue
            cf = CompanyFacts(raw, [p for c in preds.get(t, []) if (p := read_cf(c))])
            if cf.n_concepts() < 60:
                health("xbrl_thin", f"{t} (CIK {u['cik']}): only {cf.n_concepts()} concepts; add a predecessor CIK?")
            f = Fundamentals(cf, CFG["concepts"], TODAY, CFG)
            yt = ytick[t]
            src = tclose if (t in top_set and yt in tclose.columns) else uclose
            px_series = src[yt].dropna() if yt in src.columns else pd.Series(dtype=float)
            price = float(px_series.iloc[-1]) if len(px_series) else None
            splits = split_list(tsplit if t in top_set else usplit, yt)
            so = cf.shares_outstanding(TODAY)
            shares_adj = so[0] * split_factor(splits, so[1]) if so else None
            mcap, mcap_src = (price * shares_adj, f"XBRL shares ({so[2]}) × price") if price and shares_adj else (None, None)
            yinfo = (ydata.get(t) or {}).get("info") or {}
            ymcap = yinfo.get("marketCap")
            if t in top_set and ymcap:
                if t in (OVR.get("market_cap_from_yahoo") or []) or mcap is None or abs(mcap / ymcap - 1) > 0.25:
                    if mcap:
                        health("mcap", f"{t}: XBRL mcap {mcap / 1e9:.0f}B vs Yahoo {ymcap / 1e9:.0f}B -> using Yahoo")
                    mcap, mcap_src = float(ymcap), "Yahoo"
            extra = {}
            ab = None
            if t in top_set:
                ab, am = analyst_block(ydata.get(t, {}), price)
                extra.update(am)
                ins = insiders.get(t, {}).get("summary") or {}
                if mcap and ins:
                    extra["insider_net_buy_pct"] = (ins.get("buy_value", 0) - ins.get("disc_sale_value", 0)) / mcap
            m = f.compute(mcap, extra)
            sector = u["sector"]
            mset = metric_set_for(u.get("sic"), CFG["sic_routing"])
            row = {"ticker": t, "sector": sector, "set": mset, "metrics": m, "na": dict(f.na),
                   "pays_dividend": f.pays_dividend, "has_analyst": bool(ab and ab.get("n_analysts"))}
            if t in top_set:
                cur = {k: m.get(k) for k in CFG["valuation_metrics_with_own_history"]}
                own, own_na, monthly = own_history(cf, subs.get(t, {}), px_series, splits, mcap, shares_adj, cur) if mcap else ({}, {}, {})
                row["own_pct"], row["own_na"] = own, own_na
                g = f.inputs
                sc = cf.annual_shares_chain(TODAY)
                rt, ru = CFG["concepts"]["revenue"]["tags"], CFG["concepts"]["revenue"]["unit"]
                nt = CFG["concepts"]["net_income"]["tags"]
                qrev, qni = cf.quarters(rt, ru, TODAY), cf.quarters(nt, "USD", TODAY)
                qe = sorted(qrev)[-12:]
                detail[t] = {
                    "price": price, "mcap": mcap, "mcap_source": mcap_src, "latest_period": f.latest_period,
                    "shares_source": f.shares_source, "analyst": ab, "calendar": calendar_block(ydata.get(t, {})),
                    "insiders": insiders.get(t), "tech": technicals(px_series), "pe_history": monthly,
                    "shares_history": [[d.isoformat(), v] for d, v in list(sc.items())[-11:]],
                    "quarterly": [{"end": e.isoformat(), "revenue": qrev.get(e), "net_income": qni.get(e)} for e in qe],
                    "ttm": {k: g.get(k) for k in ["revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex",
                                                   "fcf", "ebitda", "ffo", "dividends_paid", "debt", "cash_total", "equity",
                                                   "total_assets"]},
                    "yahoo_errors": (ydata.get(t) or {}).get("_errors", []),
                    "news": news_block(ydata.get(t, {})) or finnhub_news(t, u["name"]),
                    "filings": filings_block(u["cik"], subs.get(t, {})), "profile": profile_block(ydata.get(t, {})),
                }
                yr, yn = yinfo.get("totalRevenue"), yinfo.get("netIncomeToCommon")
                validation.append({"ticker": t, "rev_xbrl": g.get("revenue"), "rev_yahoo": yr,
                                   "ni_xbrl": g.get("net_income"), "ni_yahoo": yn,
                                   "rev_diff": (g["revenue"] / yr - 1) if g.get("revenue") and yr else None,
                                   "ni_diff": (g["net_income"] / yn - 1) if g.get("net_income") and yn else None})
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            health("fundamentals", f"{t}: {type(e).__name__}: {e}")
            traceback.print_exc()
        if i % 200 == 0:
            log(f"fundamentals {i}/{len(mapped)}")

    log("scoring")
    scores = score_rows(rows, CFG, score_only=top_set)
    rowmap = {r["ticker"]: r for r in rows}

    # common trading calendar for the top-N price arrays
    tdates = [d.date().isoformat() for d in tclose.index]
    # Yahoo's daily bar for the latest session can lag; use regularMarketPrice for that session.
    extra_close: dict[str, float] = {}
    if tdates:
        last = tdates[-1]
        rmt = {}
        for t in top:
            info = (ydata.get(t) or {}).get("info") or {}
            ts, px = info.get("regularMarketTime"), info.get("regularMarketPrice")
            if ts and px:
                d = datetime.fromtimestamp(int(ts), timezone(timedelta(hours=-4))).date().isoformat()
                rmt[t] = (d, float(px))
        newer = sorted({d for d, _ in rmt.values() if d > last})
        if newer:
            nd = newer[-1]
            n_have = sum(1 for d, _ in rmt.values() if d == nd)
            if n_have >= 0.8 * len(top):
                tdates.append(nd)
                extra_close = {t: px for t, (d, px) in rmt.items() if d == nd}
                log(f"appended latest session {nd} from regularMarketPrice for {len(extra_close)} stocks")
    stocks = []
    for t in top:
        if t not in detail:
            continue
        u, d, sc = mapped[t], detail[t], scores.get(t, {})
        yt = ytick[t]
        closes = [None if pd.isna(v) else round(float(v), 2) for v in tclose[yt]] if yt in tclose.columns else []
        if len(tdates) > len(tclose.index):
            closes.append(round(extra_close[t], 2) if t in extra_close else None)
        prev = next((c for c in reversed(closes[:-1]) if c is not None), None) if closes else None
        stocks.append({"ticker": t, "yahoo": yt, "name": u["name"], "sector": u["sector"], "sic": u.get("sic"),
                       "sic_desc": u.get("sic_desc"), "cik": u["cik"], "metric_set": rowmap[t]["set"],
                       "ivv_weight": u["ivv_weight"], "chg_1d": (closes[-1] / prev - 1) if closes and prev and closes[-1] else None,
                       "overall": sc.get("overall"), "excluded": sc.get("excluded"), "categories": sc.get("categories"),
                       "closes": closes, **d})
    vv = [v for v in validation if v["rev_diff"] is not None]
    summary = {
        "rev_within_2pct": sum(abs(v["rev_diff"]) <= 0.02 for v in vv), "rev_within_5pct": sum(abs(v["rev_diff"]) <= 0.05 for v in vv),
        "rev_compared": len(vv),
        "ni_within_5pct": sum(abs(v["ni_diff"]) <= 0.05 for v in validation if v["ni_diff"] is not None),
        "ni_compared": sum(v["ni_diff"] is not None for v in validation),
        "worst": sorted(vv, key=lambda v: -abs(v["rev_diff"]))[:12],
    }
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "asof": TODAY.isoformat(),
           "universe_count": len(uni), "universe_mapped": len(mapped), "fundamentals_rows": len(rows),
           "top_n": len(stocks), "dates": tdates, "stocks": stocks, "macro": macro_calendar(),
           "validation": summary, "health": HEALTH, "runtime_s": round(time.time() - T0),
           "sec_calls": sec.calls,
           "sector_counts": pd.Series([r["sector"] for r in rows]).value_counts().to_dict()}
    p = ROOT / args.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean(out), separators=(",", ":")))
    (p.parent / "validation.json").write_text(json.dumps(clean(validation), indent=1))
    (p.parent / "health.json").write_text(json.dumps(clean(HEALTH), indent=1))
    log(f"wrote {p} ({p.stat().st_size / 1e6:.1f} MB); {len(stocks)} stocks; health events {len(HEALTH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
