#!/usr/bin/env python3
"""Dev only: pull a handful of real raw files so parsing logic can be developed
and unit-tested offline. Runs on the runner; output goes to the `fixtures` branch,
never to main. Not part of the production pipeline."""
from __future__ import annotations
import json, os, time, pickle
from pathlib import Path
import requests

UA = os.environ["SEC_USER_AGENT"]
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
S = requests.Session(); S.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

def sec(url):
    time.sleep(0.15)
    r = S.get(url, timeout=60); r.raise_for_status(); return r

tick = sec("https://www.sec.gov/files/company_tickers.json").json()
(OUT / "company_tickers.json").write_text(json.dumps(tick))
cik = {v["ticker"]: v["cik_str"] for v in tick.values()}

r = requests.get("https://www.ishares.com/us/products/239726/x/latest-holdings.csv", headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
(OUT / "IVV.csv").write_bytes(r.content)

TICKERS = ["AAPL", "MSFT", "JPM", "PGR", "O", "NEE", "GOOGL", "WMT", "NVDA", "COST", "BRK-B", "XOM"]
for t in TICKERS:
    c = cik[t]
    (OUT / f"companyfacts_{t}.json").write_bytes(sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c:010d}.json").content)
    sub = sec(f"https://data.sec.gov/submissions/CIK{c:010d}.json").json()
    (OUT / f"submissions_{t}.json").write_text(json.dumps(sub))
    print(t, "ok", flush=True)

# a few Form 4 raw XMLs (AAPL + JPM), including any with 10b5-1 info
for t in ["AAPL", "JPM", "NVDA"]:
    sub = json.loads((OUT / f"submissions_{t}.json").read_text())["filings"]["recent"]
    n = 0
    for form, accn, doc in zip(sub["form"], sub["accessionNumber"], sub["primaryDocument"]):
        if form != "4": continue
        raw = doc.split("/")[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik[t]}/{accn.replace('-', '')}/{raw}"
        try:
            (OUT / f"form4_{t}_{n}.xml").write_bytes(sec(url).content); n += 1
        except Exception as e:
            print("form4 fail", url, e)
        if n >= 6: break

# yfinance shapes
import yfinance as yf
yd = {}
for t in ["AAPL", "JPM", "WDFC"]:
    tk = yf.Ticker(t); d = {}
    for f in ["recommendations", "recommendations_summary", "analyst_price_targets", "upgrades_downgrades",
              "earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "calendar", "info"]:
        try: d[f] = getattr(tk, f)
        except Exception as e: d[f] = f"ERR {e}"
        time.sleep(0.3)
    yd[t] = d
pickle.dump(yd, open(OUT / "yfinance.pkl", "wb"))
px = yf.download(["AAPL", "JPM", "O"], period="3y", progress=False, auto_adjust=True, threads=False)
px.to_pickle(OUT / "prices.pkl")
print("done")
