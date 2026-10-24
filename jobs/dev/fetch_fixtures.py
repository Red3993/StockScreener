#!/usr/bin/env python3
"""Dev only: test Finnhub company-news from the runner."""
import json, os, time
from datetime import date, timedelta
from pathlib import Path
import requests
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
k = os.environ.get("FINNHUB_API_KEY"); res = {"has_key": bool(k)}
for t in ["AAPL", "JPM", "WDFC", "BRK.B"]:
    r = requests.get("https://finnhub.io/api/v1/company-news", params={"symbol": t, "from": str(date.today() - timedelta(days=14)), "to": str(date.today()), "token": k}, timeout=30)
    j = r.json() if r.ok else r.text[:300]
    res[t] = {"status": r.status_code, "n": len(j) if isinstance(j, list) else None, "sample": j[:2] if isinstance(j, list) else j}
    time.sleep(1.1)
(OUT / "news.json").write_text(json.dumps(res, indent=1, default=str))
