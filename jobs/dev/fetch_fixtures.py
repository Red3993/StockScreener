#!/usr/bin/env python3
"""Dev only: dump raw yfinance news shapes for a few tickers to the `fixtures` branch."""
import json, pickle
from pathlib import Path
import yfinance as yf
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
res = {"yfinance_version": yf.__version__}
for t in ["AAPL", "JPM", "WDFC"]:
    tk = yf.Ticker(t)
    for name, fn in [("news", lambda: tk.news), ("get_news", lambda: tk.get_news(count=5))]:
        try:
            v = fn(); res[f"{t}.{name}"] = {"type": type(v).__name__, "len": len(v) if hasattr(v, "__len__") else None, "sample": v[:2] if isinstance(v, list) else str(v)[:2000]}
        except Exception as e:
            res[f"{t}.{name}"] = f"ERR {type(e).__name__}: {e}"
(OUT / "news.json").write_text(json.dumps(res, indent=1, default=str))
print(json.dumps(res, default=str)[:3000])
