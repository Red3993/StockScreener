#!/usr/bin/env python3
"""Market news + snapshot for the Calendar tab's market brief -> market.json (runs with the quick closing-prices job).

    {"generated": ..., "asof": "YYYY-MM-DD",
     "snapshot": [{"k": "SPY", "name": "S&P 500", "kind": "index|rate|fx|commodity|crypto|vol", "last", "d1", "d5", "m1"}],
     "sectors":  [{"k": "XLK", "name": "Technology", "d1", "d5", "m1"}],
     "news":     [{"title", "src", "url", "summary", "ts", "cat"}]}

Returns are fractions; for the 10-year yield (^TNX, quoted in percent) d1/d5/m1 are changes in percentage points.
News: Finnhub's free market-news feed (general + merger categories), newest first, last few days, deduplicated.

    FINNHUB_API_KEY=... python jobs/app/market_news.py --out market.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

SNAP = [("SPY", "S&P 500", "index"), ("QQQ", "Nasdaq 100", "index"), ("IWM", "Russell 2000", "index"), ("DIA", "Dow", "index"),
        ("^VIX", "VIX (fear gauge)", "vol"), ("^TNX", "10-year Treasury yield", "rate"), ("DX-Y.NYB", "US dollar index", "fx"),
        ("CL=F", "Oil (WTI)", "commodity"), ("GC=F", "Gold", "commodity"), ("BTC-USD", "Bitcoin", "crypto")]
WIRES = {"businesswire", "globenewswire", "prnewswire", "accesswire", "newsfile"}   # company press releases, often not English
SECTORS = [("XLK", "Technology"), ("XLC", "Communication Services"), ("XLY", "Consumer Discretionary"), ("XLP", "Consumer Staples"),
           ("XLF", "Financials"), ("XLV", "Health Care"), ("XLI", "Industrials"), ("XLE", "Energy"), ("XLB", "Materials"),
           ("XLU", "Utilities"), ("XLRE", "Real Estate")]


def snapshot() -> tuple[list, list, str | None]:
    import yfinance as yf
    syms = [s for s, _, _ in SNAP] + [s for s, _ in SECTORS]
    df = None
    for attempt in range(3):
        try:
            df = yf.download(syms, period="3mo", auto_adjust=True, progress=False, threads=True, group_by="column")
            break
        except Exception as e:  # noqa: BLE001
            print(f"snapshot retry {attempt}: {e}", flush=True)
            time.sleep(15 * (attempt + 1))
    if df is None or df.empty:
        return [], [], None
    c = df["Close"].dropna(how="all")
    asof = c.index[-1].date().isoformat() if len(c) else None

    def stats(s: str, diff: bool = False) -> dict:
        x = c[s].dropna() if s in c.columns else pd.Series(dtype=float)
        if len(x) < 2:
            return {"last": None, "d1": None, "d5": None, "m1": None}
        last = float(x.iloc[-1])

        def ch(n):
            if len(x) <= n:
                return None
            prev = float(x.iloc[-1 - n])
            return round(last - prev, 4) if diff else round(last / prev - 1, 5)
        return {"last": round(last, 4), "d1": ch(1), "d5": ch(5), "m1": ch(21), "date": x.index[-1].date().isoformat()}
    snap = [{"k": s, "name": n, "kind": k, **stats(s, diff=(s == "^TNX"))} for s, n, k in SNAP]
    sect = [{"k": s, "name": n, **stats(s)} for s, n in SECTORS]
    return snap, sect, asof


def news(days: int) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        print("FINNHUB_API_KEY not set: no market news")
        return []
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    out, seen = [], set()
    for cat in ("general", "merger"):
        try:
            r = requests.get("https://finnhub.io/api/v1/news", params={"category": cat, "token": key}, timeout=30)
            items = r.json() if r.ok else []
        except Exception as e:  # noqa: BLE001
            print(f"news {cat}: {e}")
            items = []
        time.sleep(1.1)
        for it in items if isinstance(items, list) else []:
            title = re.sub(r"\s+", " ", (it.get("headline") or "")).strip()
            norm = re.sub(r"[^a-z0-9 ]", "", title.lower())[:90]
            ts = it.get("datetime") or 0
            if not title or norm in seen or ts < cut or (it.get("source") or "").lower() in WIRES:
                continue
            seen.add(norm)
            out.append({"title": title, "src": it.get("source"), "url": it.get("url"),
                        "summary": re.sub(r"\s+", " ", (it.get("summary") or "")).strip()[:320], "ts": ts, "cat": cat})
    out.sort(key=lambda x: -x["ts"])
    return out[:80]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="market.json")
    ap.add_argument("--days", type=int, default=0, help="news lookback; default 4 on Mondays, else 2")
    a = ap.parse_args()
    days = a.days or (4 if datetime.now(timezone.utc).weekday() == 0 else 2)
    snap, sect, asof = snapshot()
    nw = news(days)
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "asof": asof, "snapshot": snap, "sectors": sect, "news": nw}
    Path(a.out).write_text(json.dumps(out, separators=(",", ":")))
    print(f"market.json: asof {asof}, {len(snap)} snapshot, {len(sect)} sectors, {len(nw)} headlines")
    return 0 if snap or nw else 1


if __name__ == "__main__":
    sys.exit(main())
