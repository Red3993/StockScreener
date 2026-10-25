#!/usr/bin/env python3
"""Morning digest of the day's important news from a published app-data folder.

Used by the weekday "Publish StockScreener data" scheduled task to build Tom's phone notification.

    python jobs/app/daily_digest.py /home/claude/pub/app-data --watch AAPL,MSFT

Prints JSON: {"push": "<one line, under 200 chars>", "digest": "<markdown>"}.
"Recent" = filed or dated within --days days (default 3, so a Monday run covers Friday).
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, timedelta
from pathlib import Path


def pct(x):
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


def money(v):
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--watch", default="", help="comma-separated watchlist tickers")
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    root = Path(a.folder)
    idx = json.loads((root / "index.json").read_text())
    rows = {r["t"]: r for r in idx["rows"]}
    asof = idx["dates"][-1]
    since = (date.fromisoformat(asof) - timedelta(days=a.days)).isoformat()
    week = (date.fromisoformat(asof) + timedelta(days=7)).isoformat()
    watch = [t.strip().upper() for t in a.watch.split(",") if t.strip() and t.strip().upper() in rows]
    shards: dict[int, dict] = {}

    def detail(t):
        n = rows[t]["sh"]
        if n not in shards:
            shards[n] = json.loads((root / "detail" / f"{n:02d}.json").read_text())
        return shards[n].get(t) or {}

    out, push_bits = [f"**StockScreener, prices through {asof}**"], []
    spx = [r["d"]["c1"] for r in rows.values() if "IVV" in (r.get("ix") or []) and (r.get("d") or {}).get("c1") is not None]
    if spx:
        up = sum(1 for x in spx if x > 0)
        out.append(f"S&P 500 stocks: median {pct(statistics.median(spx))}, {up} up / {len(spx) - up} down.")

    # watchlist
    if watch:
        out.append("\n**Your watchlist**")
        for t in watch:
            r, s = rows[t], detail(t)
            bits = [f"{t} {pct((r.get('d') or {}).get('c1'))}"]
            news = [n for n in (s.get("news") or []) if (n.get("date") or "") >= since][:2]
            ins = [x for x in ((s.get("insiders") or {}).get("trades") or []) if x.get("code") == "P" and (x.get("filed") or "") >= since]
            cg = [x for x in (s.get("congress") or []) if (x.get("filed") or "") >= since and (x.get("relevant") or x.get("tracked"))]
            acts = [x for x in ((s.get("analyst") or {}).get("actions") or []) if (x.get("date") or "") >= since and x.get("action") in ("up", "down", "init")]
            earn = next((e for e in ((s.get("calendar") or {}).get("earnings") or []) if asof <= e <= week), None)
            if ins:
                bits.append(f"insider buy {money(sum(x.get('value') or 0 for x in ins))}")
            if cg:
                bits.append(f"{cg[0]['name']} {cg[0]['type']}")
            for x in acts[:2]:
                bits.append(f"{x['firm']} {'upgrade' if x['action'] == 'up' else 'downgrade' if x['action'] == 'down' else 'initiates'} to {x.get('to')}")
            if earn:
                bits.append(f"earnings {earn}")
            out.append("- " + "; ".join(bits) + "".join(f"\n  - {n['date']}: {n['title']}" for n in news))
            if len(bits) > 1 or abs((r.get("d") or {}).get("c1") or 0) >= 0.04:
                push_bits.append(bits[0] if len(bits) == 1 else f"{t}: {bits[1]}")

    # universe highlights
    movers = sorted((r for r in rows.values() if "IVV" in (r.get("ix") or []) and (r.get("d") or {}).get("c1") is not None), key=lambda r: r["d"]["c1"])
    if movers:
        out.append("\n**Biggest S&P 500 moves**: " + ", ".join(f"{r['t']} {pct(r['d']['c1'])}" for r in movers[-3:][::-1] + movers[:3]))
    buys = []
    for t, r in rows.items():
        if not ((r.get("i") or {}).get("b30")):
            continue
        for x in (detail(t).get("insiders") or {}).get("trades") or []:
            if x.get("code") == "P" and (x.get("filed") or "") >= since and (x.get("value") or 0) >= 100_000:
                buys.append((x["value"], t, x))
    buys.sort(key=lambda z: -z[0])
    if buys:
        out.append("\n**Notable insider buys (filed recently, $100K+)**")
        for v, t, x in buys[:6]:
            out.append(f"- {t}: {x.get('owner')} ({', '.join(x.get('roles') or [])[:40]}) bought {money(v)} on {x.get('date')}")
        push_bits.append(f"insider buy {buys[0][1]} {money(buys[0][0])}")
    cgs = []
    for t, r in rows.items():
        if not ((r.get("cg") or {}).get("b90")):
            continue
        for x in detail(t).get("congress") or []:
            if (x.get("filed") or "") >= since and (x.get("relevant") or x.get("tracked")) and "Purchase" in (x.get("type") or ""):
                cgs.append((t, x))
    if cgs:
        out.append("\n**Committee-relevant congressional buys (filed recently)**")
        for t, x in cgs[:6]:
            why = (x.get("committees") or ["tracked trader"])[0]
            out.append(f"- {t}: {x['name']} ({x.get('party', '')[:1]}-{x.get('state', '')}), ${x.get('amount_low') or 0:,}+ on {x['date']} ({why})")
        push_bits.append(f"{cgs[0][1]['name'].split()[-1]} bought {cgs[0][0]}")
    earn = sorted((r["e"]["earn"], r["t"]) for r in rows.values() if r.get("top") and (r.get("e") or {}).get("earn") and asof < r["e"]["earn"] <= week)
    if earn:
        out.append("\n**Top-100 earnings this week**: " + ", ".join(f"{t} ({d[5:]})" for d, t in earn[:12]))
    health = idx.get("health") or []
    if len(health) > 25:
        out.append(f"\nData note: {len(health)} data-health warnings in this build.")
    push = f"StockScreener updated ({asof}, {idx.get('count')} stocks)"
    if push_bits:
        push += ": " + "; ".join(push_bits)
    print(json.dumps({"push": push[:197] + ("..." if len(push) > 197 else ""), "digest": "\n".join(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
