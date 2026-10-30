#!/usr/bin/env python3
"""Closing prices right after the market close -> prices.json (a few minutes for ~3,000 stocks).

The full nightly build (scores, insiders, news) takes 1-3 hours; this lets the app show today's closes first.
Reads the ticker list and Yahoo symbols from the last published app-data, downloads the last 10 sessions,
and writes every close dated after app-data's last price date:

    {"generated": ..., "base": "<app-data last date>", "dates": [...], "px": {"AAPL": [c1, c2, ...]}}

    python jobs/app/prices_quick.py --app-data app-data --out prices.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--out", default="prices.json")
    a = ap.parse_args()
    root = Path(a.app_data)
    idx = json.loads((root / "index.json").read_text())
    base = idx["dates"][-1]
    ymap = {}
    for f in glob.glob(str(root / "detail" / "*.json")):
        for t, v in json.loads(Path(f).read_text()).items():
            ymap[t] = v.get("yahoo") or t
    for r in idx["rows"]:
        ymap.setdefault(r["t"], r["t"].replace(".", "-"))
    for b in ("SPY", "QQQ", "IWM"):    # benchmarks for the picks scoreboard
        ymap[b] = b
    ys = sorted(set(ymap.values()))
    frames = []
    for i in range(0, len(ys), 200):
        part = ys[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download(part, period="10d", auto_adjust=True, progress=False, threads=True, group_by="column")
                break
            except Exception as e:  # noqa: BLE001
                print(f"retry {attempt}: {e}", flush=True)
                time.sleep(15 * (attempt + 1))
        else:
            continue
        if not df.empty:
            frames.append(df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": part[0]}))
        time.sleep(1.5)
    if not frames:
        print("no prices downloaded")
        return 1
    close = pd.concat(frames, axis=1)
    close = close.loc[:, ~close.columns.duplicated()].dropna(how="all")
    close.index = [d.date().isoformat() for d in close.index]
    new = [d for d in close.index if d > base]
    # drop a session that is mostly missing (bar not final yet)
    while new and close.loc[new[-1]].isna().mean() > 0.3:
        print(f"dropping incomplete session {new[-1]} ({close.loc[new[-1]].isna().mean():.0%} missing)")
        new = new[:-1]
    px = {}
    for t, y in ymap.items():
        if y in close.columns and new:
            vals = [None if pd.isna(close.at[d, y]) else round(float(close.at[d, y]), 4) for d in new]
            if any(v is not None for v in vals):
                px[t] = vals
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base": base, "dates": new, "px": px}
    Path(a.out).write_text(json.dumps(out, separators=(",", ":")))
    print(f"base {base}; new sessions {new}; {len(px)} of {len(ymap)} tickers priced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
