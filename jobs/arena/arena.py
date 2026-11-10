#!/usr/bin/env python3
"""Arena engine: six AI traders with paper money, a Floor Manager who approves trades, and plain code that keeps the books.

The AI parts (pitches and rulings) are written by the weekly "Arena round" scheduled task from the prompts in
config/arena.yaml. This script does everything that must be exact and repeatable:

  init     start a fresh desk: every trader gets start_cash
  prepare  write each trader's briefing (their portfolio + a style-specific shortlist from the app's data) and the
           Floor Manager's briefing (house rules + every trader's book)
  apply    take the pitches and rulings, enforce the hard limits in code, and queue approved orders
  mark     nightly: fill queued orders at the latest close (never at a price the trader could have seen when pitching),
           apply stop-losses, charge short borrow fees, record each trader's equity and the S&P 500 (SPY) for comparison

State is one JSON document (the app stores it in the artifact database as app/arena). Orders are target weights
(share of the trader's equity, negative = short); the engine turns them into shares at the fill price.

    python jobs/arena/arena.py init --state arena.json
    python jobs/arena/arena.py prepare --app-data app-data --state arena.json --out packets [--research research.json] [--brief brief.json]
    python jobs/arena/arena.py apply --app-data app-data --state arena.json --pitches pitches.json --rulings rulings.json --log round.json
    python jobs/arena/arena.py mark --app-data app-data --state arena.json [--prices prices.json]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "config/arena.yaml").read_text())
CATS = ["Valuation", "Profitability", "Financial Health", "Growth", "Dividends", "Management", "Analyst"]
FRONTIER_SECTORS = {"Information Technology", "Health Care", "Communication Services"}
FRONTIER_WORDS = re.compile(r"semiconductor|software|internet|biotech|pharma|medical|diagnostic|life science|computer|"
                            r"electronic|communication equipment|aerospace|scientific|technical|information tech|data|cyber|"
                            r"solar|instrument|health information|robot", re.I)
NOT_FRONTIER = re.compile(r"telecom services|entertainment|advertising|publishing|broadcasting|pharmaceutical retail|"
                          r"health care plans|medical care facilities|medical distribution|drug manufacturers - general", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(p) -> dict:
    return json.loads(Path(p).read_text())


def save(p, obj) -> None:
    Path(p).write_text(json.dumps(obj, separators=(",", ":"), default=str))


# ------------------------------------------------------------------ market data
class Market:
    def __init__(self, app_data: str, prices: str | None = None) -> None:
        idx = load(Path(app_data) / "index.json")
        self.idx = idx
        self.rows = {r["t"]: r for r in idx["rows"]}
        self.date = idx["dates"][-1]
        self.px = {t: (r.get("d") or {}).get("px") for t, r in self.rows.items()}
        spy = [v for v in (idx.get("bench") or {}).get("SPY", []) if v is not None]
        self.spy = spy[-1] if spy else None
        if prices and Path(prices).exists():
            q = load(prices)
            if q.get("dates") and q["dates"][-1] > self.date:
                self.date = q["dates"][-1]
                for t, arr in (q.get("px") or {}).items():
                    last = next((v for v in reversed(arr) if v is not None), None)
                    if last is not None:
                        if t == "SPY":
                            self.spy = last
                        else:
                            self.px[t] = last

    def price(self, t: str) -> float | None:
        v = self.px.get(t)
        return float(v) if v else None


# ------------------------------------------------------------------ state
def personas() -> list:
    return [{k: t.get(k) for k in ("id", "name", "color", "style", "blurb", "strategy")} for t in CFG["traders"]] + [{"id": "manager", "name": CFG["manager"]["name"], "color": CFG["manager"].get("color"), "blurb": CFG["manager"].get("blurb")}]


def new_account() -> dict:
    return {"cash": float(CFG["start_cash"]), "positions": {}, "hist": [], "peak": float(CFG["start_cash"]),
            "probation": False, "trades": 0, "approved": 0, "pitched": 0, "realized": 0.0, "closed": []}


def ensure_traders(st: dict, today: str | None = None) -> dict:
    """Add traders that were added to config/arena.yaml after the desk started (they start with fresh cash)."""
    for t in CFG["traders"]:
        if t["id"] not in st["traders"]:
            st["traders"][t["id"]] = {**new_account(), "joined": today}
    st["personas"] = personas()
    return st


def init_state() -> dict:
    return {"created": now(), "personas": personas(), "start_cash": CFG["start_cash"], "start": None, "round": 0, "spy0": None, "pending": [], "rounds": [],
            "traders": {t["id"]: {"cash": float(CFG["start_cash"]), "positions": {}, "hist": [], "peak": float(CFG["start_cash"]),
                                  "probation": False, "trades": 0, "approved": 0, "pitched": 0, "realized": 0.0, "closed": []}
                        for t in CFG["traders"]}}


def equity(tr: dict, mk: Market) -> float:
    eq = tr["cash"]
    for t, p in tr["positions"].items():
        px = mk.price(t) or p.get("last") or p["entry"]
        eq += p["shares"] * px
    return eq


def exposures(tr: dict, mk: Market) -> dict:
    eq = equity(tr, mk) or 1.0
    out = {}
    for t, p in tr["positions"].items():
        px = mk.price(t) or p.get("last") or p["entry"]
        out[t] = p["shares"] * px / eq
    return out


# ------------------------------------------------------------------ shortlists (one per trading style)
def fnum(v, nd=1, pct=False):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    return f"{v * 100:+.{nd}f}%" if pct else f"{v:.{nd}f}"


def line(r: dict) -> str:
    d, mo, ry, a, i, cg, e = (r.get(k) or {} for k in ("d", "mo", "ry", "a", "i", "cg", "e"))
    cs = r.get("cs") or [None] * 7
    sc = " ".join(f"{c[:4]} {v if v is not None else '-'}" for c, v in zip(CATS, cs))
    mc = r.get("mc")
    bits = [f"{r['t']} | {r['n']} | {r['sec']}{' / ' + r['ind'] if r.get('ind') else ''} | mcap {mc / 1e9:.1f}B" if mc else f"{r['t']} | {r['n']} | {r['sec']}",
            f"price {d.get('px')} | 1d {fnum(d.get('c1'), 1, True)} 1m {fnum(d.get('r21'), 1, True)} 3m {fnum(d.get('r63'), 1, True)} 1y {fnum(d.get('r252'), 1, True)}"
            f" | {fnum(d.get('fh'), 0, True)} from 52w high, {fnum(d.get('fl'), 0, True)} above low | RSI {fnum(d.get('rsi'), 0)} | vs200d {fnum(d.get('v200'), 0, True)}",
            f"scores: overall {r.get('ov')} ({sc}) | cheap-vs-own-history {fnum(r.get('own'), 0)}/100 | Trend {ry.get('t')}/10 Rally {ry.get('r')}/10"
            f"{' (drivers: ' + ', '.join(ry.get('k') or []) + ')' if ry.get('k') else ''} | momentum regime {mo.get('rg')}",
            f"analysts {a.get('n')} rating {a.get('rm')} up/down 90d {a.get('ug')}/{a.get('dg')} target {a.get('tm')}"
            f"{' (gap ' + fnum(a['tm'] / d['px'] - 1, 0, True) + ')' if a.get('tm') and d.get('px') else ''} | short interest {fnum(mo.get('si'), 1, True).lstrip('+')} of float"
            f" | insider buys 90d {i.get('b90', 0)}{' (cluster)' if i.get('cl') else ''}{' (CEO/CFO)' if i.get('ceo') else ''}, disc. sales {i.get('ds90', 0)}"
            f" | congress buys 90d {cg.get('b90', 0)} | earnings {e.get('earn') or 'n/a'}"]
    return "\n  ".join(bits)


LAB_FACTORS = {   # live versions of the Strategy Lab factors, from index.json rows (higher = better)
    "value": lambda r: (r.get("cs") or [None] * 7)[0], "profitability": lambda r: (r.get("cs") or [None] * 7)[1],
    "health": lambda r: (r.get("cs") or [None] * 7)[2], "growth": lambda r: (r.get("cs") or [None] * 7)[3],
    "dividends": lambda r: (r.get("cs") or [None] * 7)[4] or 0, "management": lambda r: (r.get("cs") or [None] * 7)[5],
    "trend": lambda r: (r.get("ry") or {}).get("t"), "rally": lambda r: (r.get("ry") or {}).get("r"),
    "reversal": lambda r: None if (r.get("d") or {}).get("r21") is None else -r["d"]["r21"],
    "near_low": lambda r: None if (r.get("d") or {}).get("fl") is None else -r["d"]["fl"],
    "oversold": lambda r: None if (r.get("d") or {}).get("rsi") is None else -r["d"]["rsi"],
    "congress": lambda r: (r.get("cg") or {}).get("b90") or 0, "insider": lambda r: (r.get("i") or {}).get("b90") or 0,
}


def trader_min_mcap(tid: str) -> float:
    """Market-cap floor: the trader's own `min_mcap` in arena.yaml, else $300M for YOLO, else the house floor."""
    own = next((t.get("min_mcap") for t in CFG["traders"] if t["id"] == tid), None)
    return own if own else (CFG["limits"]["yolo_min_mcap"] if tid == "yolo" else CFG["limits"]["min_mcap"])


def trader_stop_long(tid: str) -> float:
    """Automatic stop for longs: the trader's own `stop_long` in arena.yaml, else the house stop."""
    own = next((t.get("stop_long") for t in CFG["traders"] if t["id"] == tid), None)
    return own if own is not None else CFG["stops"]["long"]


LONG_ONLY = {t["id"] for t in CFG["traders"] if t.get("long_only")}   # code-enforced: these traders never short
SIGNALS: dict | None = None    # signals.json from the Signal Lab (set by cmd_prepare)
SMART = ("ins_buy", "ins_cluster", "ins_ceo", "cong_buy", "cong_rel")
SCORE_F = ("value7", "profit7", "health7", "growth7", "mgmt7", "overall6", "junk")


def signal_book(strat: dict) -> tuple[list[dict], dict[str, list]]:
    """The validated signal combinations this trader trades, and which stocks fired them in the last 10 trading days."""
    S = SIGNALS or {}
    h = str(strat.get("horizon", 63))
    lead = (S.get("leaders") or {}).get(h) or {}
    pool = lead.get("tech" if strat.get("pick") == "tech" else "best") or []
    if strat.get("pick") == "insider":
        pool = [k for k in pool if any(x.startswith("ins_") for x in [k[0]] + (k[1] or "").split("+")) or "ins90" in k[2]]
    combos = []
    for k in pool[: strat.get("combos", 8)]:
        key = "|".join([k[0], k[1] or "", ",".join(k[2])])
        x = ((S.get("results") or {}).get(key) or {}).get(h) or {}
        nm = " + ".join(S["signals"][x]["name"] for x in [k[0]] + [y for y in (k[1] or "").split("+") if y]) + "".join(f" [{S['filters'][f]}]" for f in k[2])
        combos.append({"key": key, "name": nm, "n": x.get("n"), "avg": x.get("avgc"), "hit": x.get("hit"), "t": x.get("t"), "tr": x.get("tr"), "te": x.get("te")})
    fired: dict[str, list] = {}
    for c in combos:
        for d, tk in (S.get("live") or {}).get(c["key"], []):
            fired.setdefault(tk, []).append((d, c))
    return combos, fired


def fund_tier(c: dict | None) -> str:
    """Same verdict as the app's Funds leaderboard (copying each 13F when filed vs the S&P 500)."""
    if not c:
        return "none"
    ex, br = c["ann"] - c["spy_ann"], (c["beat"] / c["periods"] if c.get("periods") else 0)
    return "beats" if c["periods"] >= 6 and ex >= 0.02 and br >= 0.55 else "lags" if ex <= -0.02 else "market"


def copycat_book(mk: Market, research: dict | None, strat: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Model portfolio: the latest 13F holdings of market-beating funds (each fund equal-weighted, positions by their weight in
    the fund), plus recent purchases by members of Congress with a proven or promising record."""
    funds = [f for f in (research or {}).get("filers", []) if f.get("group") in ("fund", "person") and not f.get("error") and not f.get("stale") and f.get("copy") is not False
             and fund_tier(f.get("copycat")) == "beats"]
    w: dict[str, float] = {}
    who: dict[str, list] = {}
    for f in funds:
        top = [x for x in (f.get("top") or []) if x.get("t") in mk.rows][: strat.get("per_fund", 15)]
        tot = sum(x["pct"] for x in top) or 1
        for x in top:
            t_ = {"GOOGL": "GOOG", "BRK-A": "BRK-B", "FOXA": "FOX", "NWSA": "NWS"}.get(x["t"], x["t"])   # one share class per company
            x = {**x, "t": t_ if t_ in mk.rows else x["t"]}
            w[x["t"]] = w.get(x["t"], 0) + x["pct"] / tot / len(funds)
            who.setdefault(x["t"], []).append(f"{f['name']} {x['pct']:.1%}{' (' + x['chg'] + ')' if x.get('chg') in ('new', 'add') else ''}")
    model = sorted(({"t": t, "w": v, "funds": who[t]} for t, v in w.items()), key=lambda x: -x["w"])[: strat.get("positions", 12)]
    s_ = sum(x["w"] for x in model) or 1
    fund_share = strat.get("fund_share", 0.75)
    for x in model:
        x["w"] = round(x["w"] / s_ * fund_share, 4)
    tr = mk.idx.get("track") or {}
    good = {c["name"]: ("Proven edge" if c.get("t63", 0) >= 2 and c.get("n63", 0) >= 8 else "Promising")
            for c in tr.get("congress", []) if (c.get("avg63") or 0) > 0 and (c.get("t63") or 0) >= 1.2}
    buys = [x for x in tr.get("recent_by_top", []) if x.get("kind") == "Congress" and x.get("who") in good and x.get("t") in mk.rows]
    seen, cong = set(), []
    for x in sorted(buys, key=lambda x: x.get("filed") or "", reverse=True):
        if x["t"] in seen:
            continue
        seen.add(x["t"])
        cong.append({**x, "tier": good[x["who"]]})
    fund_info = [{"name": f["name"], "manager": f.get("manager"), "period": f.get("period"), "filed": f.get("filed"),
                  "ex": f["copycat"]["ann"] - f["copycat"]["spy_ann"], "beat": f["copycat"]["beat"], "periods": f["copycat"]["periods"]} for f in funds]
    return model, cong[: strat.get("congress", 8)], fund_info


def strategy_rank(mk: Market, strat: dict) -> list[tuple[str, float]]:
    """Rank the strategy's universe by the average percentile rank of its factors (as in jobs/lab/strategy_lab.py)."""
    R = [r for r in mk.rows.values() if ((r.get("d") or {}).get("px") or 0) >= 3 and (r.get("mc") or 0) >= CFG["limits"]["min_mcap"]
         and (strat.get("universe") != "sp500" or "IVV" in (r.get("ix") or []))]
    ranks = {}
    for f in strat["factors"]:
        vals = sorted((v, r["t"]) for r in R for v in [LAB_FACTORS[f](r)] if v is not None)
        n = len(vals)
        ranks[f] = {t: (k + 1) / n for k, (v, t) in enumerate(vals)} if n > 1 else {}
    out = []
    for r in R:
        xs = [ranks[f][r["t"]] for f in strat["factors"] if r["t"] in ranks[f]]
        if len(xs) >= math.ceil(0.6 * len(strat["factors"])):
            out.append((r["t"], sum(xs) / len(xs)))
    return sorted(out, key=lambda x: -x[1])


def shortlist(mk: Market, tid: str, research: dict | None) -> tuple[list[str], list[str]]:
    lim = CFG["limits"]
    strat = next((t.get("strategy") for t in CFG["traders"] if t["id"] == tid), None)
    if strat and strat.get("type") == "copycat":
        model, cong, _ = copycat_book(mk, research, strat)
        return list(dict.fromkeys([x["t"] for x in model] + [x["t"] for x in cong])), []
    if strat and strat.get("type") == "signals":
        combos, fired = signal_book(strat)
        order = {c["key"]: i for i, c in enumerate(combos)}
        ok = [(tk, min(order[c["key"]] for _, c in v), max(d for d, _ in v)) for tk, v in fired.items()
              if tk in mk.rows and (mk.rows[tk].get("mc") or 0) >= lim["min_mcap"] and (mk.rows[tk].get("d") or {}).get("px")]
        ok.sort(key=lambda x: (x[1], x[2] and -int(x[2].replace("-", ""))))
        return [tk for tk, _, _ in ok[:30]], []
    if strat:
        ranked = strategy_rank(mk, strat)
        return [t for t, _ in ranked[:30]], []
    minc = trader_min_mcap(tid)
    R = [r for r in mk.rows.values() if (r.get("mc") or 0) >= minc and (r.get("d") or {}).get("px")]
    g = lambda r, k, sub=None, dflt=0: ((r.get(k) or {}).get(sub) if sub else r.get(k)) or dflt   # noqa: E731
    cs = lambda r, i: (r.get("cs") or [None] * 7)[i] or 0   # noqa: E731
    if tid == "value":
        long = sorted(R, key=lambda r: -(2 * cs(r, 0) + cs(r, 1) + cs(r, 2) + cs(r, 5) + (g(r, "own") or 0) / 10 - (5 if g(r, "mo", "rg") == "Falling knife" else 0)))
        short = sorted(R, key=lambda r: (cs(r, 0) + cs(r, 1) + cs(r, 3)) - (g(r, "mo", "st") or 0) / 20)
    elif tid == "momentum":
        long = sorted(R, key=lambda r: -((g(r, "ry", "t") or 0) * 2 + (g(r, "mo", "s") or 0) / 10 + (g(r, "d", "r63") or 0) * 10))
        short = sorted(R, key=lambda r: ((g(r, "ry", "t") or 10) * 2 + (g(r, "mo", "s") or 50) / 10 + (g(r, "d", "r63") or 0) * 10))
    elif tid == "contrarian":
        long = sorted(R, key=lambda r: -((g(r, "ry", "r") or 0) * 2 + (3 if g(r, "i", "cl") else 0) + (2 if g(r, "mo", "rg") == "Basing / turning" else 0) - (g(r, "d", "pos") or 0.5) * 3))
        short = sorted(R, key=lambda r: -((g(r, "mo", "st") or 0) / 10 + (g(r, "d", "rsi") or 50) / 10 + (g(r, "d", "v200") or 0) * 5 - cs(r, 0)))
    elif tid == "smart_money":
        # mirror the best, not the average: only members with a promising record, and funds weighted by their copycat record
        hot = {}
        tr = mk.idx.get("track") or {}
        good = {c["name"] for c in tr.get("congress", []) if (c.get("avg63") or 0) > 0 and (c.get("t63") or 0) >= 1.2}
        for x in tr.get("recent_by_top", []):
            if x.get("kind") != "Congress" or x.get("who") in good:
                hot[x["t"]] = hot.get(x["t"], 0) + 3

        # plus what market-beating funds and people (13F copycat verdict "Beats the market") newly bought or added to
        for f in (research or {}).get("filers", []):
            if f.get("group") in ("fund", "person") and not f.get("stale") and f.get("copy") is not False and fund_tier(f.get("copycat")) == "beats":
                for x in f.get("top") or []:
                    if x.get("t") and x.get("chg") in ("new", "add"):
                        hot[x["t"]] = hot.get(x["t"], 0) + 2
        sold = {}
        long = sorted(R, key=lambda r: -(hot.get(r["t"], 0) * 3 + (4 if g(r, "i", "cl") else 0) + g(r, "i", "b90") + 2 * g(r, "cg", "b90")))
        short = sorted(R, key=lambda r: -(sold.get(r["t"], 0) * 3 + g(r, "i", "ds90") - g(r, "i", "b90")))
    elif tid == "yolo":
        def soon(r):
            ed = g(r, "e", "earn", None)
            return 1 if ed and mk.date < ed <= f"{mk.date[:8]}{min(28, int(mk.date[8:]) + 10):02d}" else 0
        long = sorted(R, key=lambda r: -((g(r, "mo", "si") or 0) * 20 + (g(r, "ry", "r") or 0) + abs(g(r, "d", "r21") or 0) * 15 + 3 * soon(r)))
        short = sorted(R, key=lambda r: -((g(r, "mo", "st") or 0) / 8 + (g(r, "d", "r63") or 0) * 8 - cs(r, 1)))
    elif tid == "dividend":
        # Big-name income (Tom, 2026-11-10): $50B+, yield 2.5-8%, affordable payout, a growth streak. Long only, low turnover.
        def mm(r, k):
            return (r.get("m") or {}).get(k)
        D = []
        for r in R:
            y, pe, pf, st_ = mm(r, "div_yield"), mm(r, "payout_earnings"), mm(r, "payout_fcf"), mm(r, "div_growth_streak")
            if y is None or not (0.025 <= y <= 0.08) or (st_ or 0) < 5:
                continue
            if (pe is not None and pe > 0.85) and (pf is None or pf > 0.9):
                continue   # payout not covered by earnings or free cash flow
            if g(r, "mo", "rg") == "Falling knife":
                continue
            D.append(r)
        def dscore(r):
            y, pe, st_ = mm(r, "div_yield"), mm(r, "payout_earnings"), mm(r, "div_growth_streak")
            return (min(y, 0.06) * 100 + min(st_ or 0, 25) / 3 + cs(r, 4) + cs(r, 2) / 2 + cs(r, 1) / 2
                    - (max(0.0, (pe or 0.5) - 0.7) * 10) - (max(0.0, y - 0.06) * 100))
        long, per = [], {}
        for r in sorted(D, key=lambda r: -dscore(r)):   # spread: at most 6 per sector
            if per.get(r["sec"], 0) < 6:
                long.append(r)
                per[r["sec"]] = per.get(r["sec"], 0) + 1
        short = []
    elif tid == "analyst":
        # Wall Street's favourites (Tom, 2026-11-04): Strong Buy consensus, big gap to the mean target, preferably 4+ analysts. Long only.
        def gap(r):
            a, px = r.get("a") or {}, g(r, "d", "px", None)
            return a["tm"] / px - 1 if a.get("tm") and px else None
        A = [r for r in R if (g(r, "a", "n") or 0) >= 3 and g(r, "a", "rm", None) is not None and g(r, "a", "rm") <= 2.0 and (gap(r) or 0) > 0.05]
        def score(r):   # gaps count up to +60%; above +100% they are more often stale targets on a collapsed stock
            gp, n = gap(r), g(r, "a", "n")
            return (min(gp, 0.6) * 10 - max(0.0, gp - 1.0) * 4 + (2.0 - g(r, "a", "rm")) * 6 + (3 if n >= 4 else -3) + min(n, 25) / 5
                    + 1.5 * (g(r, "a", "ug") - g(r, "a", "dg")) - (4 if g(r, "mo", "rg") == "Falling knife" else 0))
        long, per = [], {}
        for r in sorted(A, key=lambda r: -score(r)):   # spread the shortlist: at most 8 per sector, 5 per industry
            k1, k2 = r["sec"], "ind:" + (r.get("ind") or r["sec"])
            if per.get(k1, 0) < 8 and per.get(k2, 0) < 5:
                long.append(r)
                per[k1], per[k2] = per.get(k1, 0) + 1, per.get(k2, 0) + 1
        short = []
    else:   # frontier
        F = [r for r in R if r["sec"] in FRONTIER_SECTORS and (not r.get("ind") or (FRONTIER_WORDS.search(r["ind"]) and not NOT_FRONTIER.search(r["ind"])))]
        long = sorted(F, key=lambda r: -(cs(r, 3) * 2 + (g(r, "ry", "t") or 0) + cs(r, 6) + (g(r, "ry", "r") or 0) / 2))
        short = sorted(F, key=lambda r: (cs(r, 1) + cs(r, 2) + (g(r, "ry", "t") or 5)) - (g(r, "mo", "st") or 0) / 15)
    return [r["t"] for r in long[:30]], [r["t"] for r in short[:12]]


# ------------------------------------------------------------------ commands
def cmd_prepare(a) -> int:
    mk = Market(a.app_data, a.prices)
    st = load(a.state)
    research = load(a.research) if a.research and Path(a.research).exists() else None
    global SIGNALS
    SIGNALS = load(a.signals) if getattr(a, "signals", None) and Path(a.signals).exists() else None
    ensure_traders(st, mk.date)
    brief = load(a.brief) if a.brief and Path(a.brief).exists() else None
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lim = CFG["limits"]
    rules = (f"House rules (enforced by code after the Floor Manager rules): long positions up to {lim['max_long']:.0%} of your equity, shorts up to "
             f"{lim['max_short']:.0%}, total shorts up to {lim['max_short_total']:.0%}, longs + shorts up to {lim['max_gross']:.0%}, one sector up to "
             f"{lim['max_sector']:.0%}; limits halve on probation. Minimum market cap ${lim['min_mcap'] / 1e9:.0f}B (YOLO ${lim['yolo_min_mcap'] / 1e6:.0f}M). "
             f"Orders fill at the next session's close, after you pitch; trading costs {CFG['cost_bps'] / 100:.2f}%; shorts pay {CFG['borrow_rate']:.0%}/yr borrow. "
             f"Stops (automatic): longs close {CFG['stops']['long']:.0%} below entry, shorts cover {CFG['stops']['short']:+.0%} above entry. "
             f"FULLY INVESTED: after this round your longs must add up to at least {lim.get('min_invested', 0):.0%} of your equity (cash at most "
             f"{1 - lim.get('min_invested', 0):.0%}); hold roughly 8-15 positions so you can get there within the position limit. If your book "
             f"falls short, the engine scales all your longs up proportionally, so pick enough names you actually want to own. Exception: if you "
             f"believe a sell-off or bear market is coming, you may choose a defensive cash level (the \"cash\" field) and explain why; "
             f"it lasts until the next round, so set it again each week you still want it.")
    mkt = ""
    if brief:
        mkt = "MARKET BRIEF (" + str(brief.get("asof")) + "): " + (brief.get("summary") or "") + "\n" + "\n".join(
            f"- {x.get('title')}: {x.get('detail')}" for x in (brief.get("moved") or []) + (brief.get("world") or [])) + "\nComing up: " + "; ".join(
            f"{x.get('date')} {x.get('title')}" for x in (brief.get("ahead") or []))
    board = []
    for t in CFG["traders"]:
        tr = st["traders"][t["id"]]
        eq = equity(tr, mk)
        ex = exposures(tr, mk)
        book = "; ".join(f"{k} {v:+.1%} (entry {p['entry']:.2f}, now {mk.price(k) or p.get('last')}, since {p['since']})"
                         for k, v in sorted(ex.items(), key=lambda kv: -abs(kv[1])) for p in [tr["positions"][k]]) or "no positions (all cash)"
        pend = [o for o in st["pending"] if o["trader"] == t["id"]]
        ct = tr.get("cash_target") or 0
        defensive = f", defensive cash target {ct:.0%}" if ct else ""
        board.append(f"{t['name']} ({t['id']}): equity ${eq:,.0f} ({eq / CFG['start_cash'] - 1:+.1%} since start), cash ${tr['cash']:,.0f}, "
                     f"peak ${tr['peak']:,.0f}{', ON PROBATION' if tr['probation'] else ''}{defensive}. Book: {book}." + (f" Unfilled orders: {pend}." if pend else ""))
        lg, sh = shortlist(mk, t["id"], research)
        model = ""
        if (t.get("strategy") or {}).get("type") == "signals":
            combos, fired = signal_book(t["strategy"])
            hd = t["strategy"].get("horizon", 63)
            model = (f"\nYOUR VALIDATED SIGNALS (Signal Lab, holding period {hd} trading days; each worked in both the training and the unseen "
                     f"test period; returns are vs the median stock, per signal, before costs):\n"
                     + "\n".join(f"  S{i + 1}. {c['name']}: {c['n']} past signals, avg {fnum(c['avg'], 1, True)}, beat the market {fnum(c['hit'] * 100 if c['hit'] is not None else None, 0)}% of the time, "
                                  f"t {fnum(c['t'], 1)}, training {fnum(c['tr'], 1, True)} / test {fnum(c['te'], 1, True)}" for i, c in enumerate(combos))
                     + "\nFIRED IN THE LAST 10 TRADING DAYS: "
                     + ("; ".join(f"{tk} ({', '.join(sorted({'S' + str(combos.index(c) + 1) + ' on ' + d for d, c in v}))})" for tk, v in list(fired.items())[:40]) or "none")
                     + "\nYour holdings and when you bought them: " + (", ".join(f"{k} since {p['since']}" for k, p in tr["positions"].items()) or "none") + "\n")
        elif (t.get("strategy") or {}).get("type") == "copycat":
            mdl, cong, fi = copycat_book(mk, research, t["strategy"])
            cs_ = t["strategy"].get("fund_share", 0.75)
            model = (f"\nFUNDS YOU COPY (verdict 'Beats the market': copying each 13F when it was filed beat the S&P 500 by 2%+/yr and in most quarters):\n"
                     + ("\n".join(f"  - {f['name']} ({f['manager']}): {f['ex']:+.1%}/yr vs S&P 500, ahead in {f['beat']}/{f['periods']} quarters; latest 13F for quarter ended {f['period']}, filed {f['filed']}" for f in fi) or "  none qualify right now")
                     + f"\nMODEL PORTFOLIO, fund part ({cs_:.0%} of your money; each fund equal-weighted, positions by their weight in each fund):\n"
                     + ("\n".join(f"  {x['t']}: target {x['w']:.1%}  <- {'; '.join(x['funds'][:4])}" for x in mdl) or "  empty")
                     + f"\nCONGRESS PART (up to {1 - cs_:.0%}, about {(1 - cs_) / max(1, len(cong)):.1%} each, hold ~3 months): recent purchases by members rated Proven edge or Promising:\n"
                     + ("\n".join(f"  {x['t']}: {x['who']} ({x['tier']}), traded {x.get('date')}, disclosed {x.get('filed')}" for x in cong) or "  none in the last 60 days")
                     + "\nYour holdings and when you bought them: " + (", ".join(f"{k} since {p['since']}" for k, p in tr["positions"].items()) or "none") + "\n")
        elif t.get("strategy"):
            ranked = strategy_rank(mk, t["strategy"])
            pos_ = {k: i + 1 for i, (k, _) in enumerate(ranked)}
            cut = max(10, int(len(ranked) * t["strategy"].get("top_frac", 0.1)))
            model = (f"\nMODEL RANKING ({' + '.join(t['strategy']['factors'])}, {t['strategy'].get('universe')} universe, {len(ranked)} stocks ranked; "
                     f"the backtested rule holds the top {cut}). Your current holdings' ranks: "
                     + (", ".join(f"{k} #{pos_.get(k, 'unranked')}" for k in tr["positions"]) or "none") + "\n")
        held = [k for k in tr["positions"] if k in mk.rows and k not in lg and k not in sh]
        txt = f"""{t['prompt'].strip()}

Goal: maximize your return over the coming weeks within the rules. It is {mk.date} (data as of that close). Round {st['round'] + 1}.
{rules}

YOUR BOOK
{board[-1]}
{model}
{mkt}

LONG IDEAS that fit your style (from the app's data; you may also pick any stock below):
""" + "\n".join(("- " + (f"model rank #{n + 1}: " if t.get("strategy") and t["strategy"].get("type") not in ("signals", "copycat") else "") + line(mk.rows[k])) for n, k in enumerate(lg)) + "\n\nSHORT IDEAS that fit your style:\n" + "\n".join("- " + line(mk.rows[k]) for k in sh) + (
            "\n\nYOUR CURRENT HOLDINGS:\n" + "\n".join("- " + line(mk.rows[k]) for k in held) if held else "") + """

Use only facts from the data above. Reply with JSON only:
{"view": "1-2 sentences on how you see the market this week",
 "cash": optional, only if you fear a sell-off or bear market: share of your equity to hold in cash this week (0.4 = 40%; leave it out to stay fully invested),
 "cash_reason": "if you set cash: 1-2 sentences on why, from the data",
 "trades": [{"ticker": "XYZ", "action": "buy|sell|short|cover", "weight": target share of your equity after the trade (0.10 = 10%; for shorts give a positive number; 0 to close),
             "thesis": "1-2 sentences in your own voice", "evidence": ["2-4 specific facts from the data"]}]}
Propose up to 15 trades (enough to stay fully invested). "sell" and "cover" close or reduce a position; "buy" and "short" open or add to one."""
        (out / f"{t['id']}.md").write_text(txt)
    (out / "manager.md").write_text(f"""{CFG['manager']['prompt'].strip()}

It is {mk.date}. Round {st['round'] + 1}.
{rules}

THE DESK
""" + "\n".join("- " + b for b in board) + f"""

{mkt}

You will be given every trader's pitches (JSON). For each trade reply with JSON only:
{{"rulings": [{{"trader": "id", "ticker": "XYZ", "action": "buy|sell|short|cover", "decision": "approve|resize|reject", "weight": approved target weight (positive), "reason": "one sentence"}}],
 "probation": {{"trader_id": true|false for every trader}},
 "note": "2-3 sentences to the desk about risk this week"}}
Closing trades (sell, cover) should normally be approved.""")
    (out / "tickers.json").write_text(json.dumps(sorted(mk.rows)))
    print(f"prepared round {st['round'] + 1} as of {mk.date} for {len(CFG['traders'])} traders -> {out}")
    return 0


def topup(st: dict, mk: "Market", rid: int, cash: dict | None = None) -> list:
    """Keep every book fully invested (Tom, 2026-11-03: "I'm looking for full portfolios").
    After the round's orders, a trader's longs (holdings + queued orders) must add up to at least limits.min_invested of
    equity. If they fall short, every long that isn't being sold is scaled up by the same factor, each capped at the
    position limit and its sector at the sector limit, repeated until the floor is met or no long has room left (a book
    with only a few names can stay below the floor). Extra buys are queued for the next close like any other order.
    A trader who fears a sell-off can choose a defensive cash level in their pitch ("cash": 0.4 = keep up to 40% in cash);
    it's stored as tr["cash_target"] and lowers that trader's floor to 1 - cash (Tom: bots may go to cash if they feel that's better)."""
    lim = CFG["limits"]
    floor = lim.get("min_invested") or 0
    out = []
    if floor <= 0:
        return out
    for t in CFG["traders"]:
        tid = t["id"]
        tr = st["traders"][tid]
        if cash is not None and tid in cash:
            tr["cash_target"] = cash[tid]
        fl = min(floor, 1 - (tr.get("cash_target") or 0))
        f = lim["probation_factor"] if tr["probation"] else 1.0
        cap, scap = lim["max_long"] * f, lim["max_sector"] * f
        ex = exposures(tr, mk)
        frozen = set()
        for o in st["pending"]:
            if o["trader"] == tid:
                ex[o["ticker"]] = o["target"]
                if o["action"] in ("sell", "cover", "short"):
                    frozen.add(o["ticker"])
        longs = {k: v for k, v in ex.items() if v > 0 and k in mk.rows}
        new = dict(longs)
        sec = lambda k: (mk.rows.get(k) or {}).get("sec", "?")
        for _ in range(12):
            need = fl - sum(new.values())
            if need <= 1e-4:
                break
            ssum = {}
            for k, v in ex.items():
                ssum[sec(k)] = ssum.get(sec(k), 0) + abs(new.get(k, v))
            room = {k: min(cap - v, scap - ssum[sec(k)]) for k, v in new.items() if k not in frozen}
            room = {k: r for k, r in room.items() if r > 1e-4}
            base = sum(new[k] for k in room)
            if not room or base <= 0:
                break
            for k in room:
                new[k] += min(room[k], need * new[k] / base)
        for k, v in new.items():
            if v > longs[k] + 0.002:
                o = {"round": rid, "trader": tid, "ticker": k, "target": round(v, 4), "action": "buy", "asof": mk.date, "topup": True}
                st["pending"] = [p for p in st["pending"] if not (p["trader"] == tid and p["ticker"] == k)] + [o]
                out.append(o)
    return out


def cmd_topup(a) -> int:
    mk = Market(a.app_data, a.prices)
    st = load(a.state)
    ensure_traders(st, mk.date)
    tops = topup(st, mk, st["round"])
    save(a.state, st)
    by = {}
    for o in tops:
        by.setdefault(o["trader"], []).append(f"{o['ticker']} {o['target']:.1%}")
    print(f"topped up as of {mk.date}: {len(tops)} orders queued for the next close; " + "; ".join(f"{k}: {', '.join(v)}" for k, v in by.items()))
    return 0


def cmd_apply(a) -> int:
    mk = Market(a.app_data, a.prices)
    st = load(a.state)
    pitches, rulings = load(a.pitches), load(a.rulings)
    lim = CFG["limits"]
    ids = {t["id"] for t in CFG["traders"]}
    st["round"] += 1
    rid = st["round"]
    ensure_traders(st, mk.date)
    if not st.get("start"):
        st["start"], st["spy0"] = mk.date, mk.spy
    for tid, b in (rulings.get("probation") or {}).items():
        if tid in ids:
            st["traders"][tid]["probation"] = bool(b)
    rmap = {(x.get("trader"), (x.get("ticker") or "").upper(), x.get("action")): x for x in rulings.get("rulings", [])}
    queued, log_trades = [], []
    for tid, p in pitches.items():
        if tid not in ids:
            continue
        tr = st["traders"][tid]
        f = lim["probation_factor"] if tr["probation"] else 1.0
        for x in (p.get("trades") or [])[:15]:
            t, act = (x.get("ticker") or "").upper(), x.get("action")
            tr["pitched"] += 1
            r = rmap.get((tid, t, act)) or {"decision": "reject", "reason": "No ruling from the Floor Manager."}
            entry = {"trader": tid, "ticker": t, "action": act, "weight": x.get("weight"), "thesis": x.get("thesis"), "evidence": x.get("evidence"),
                     "decision": r.get("decision"), "approved_weight": r.get("weight"), "reason": r.get("reason")}
            note = None
            if t not in mk.rows:
                entry["decision"], note = "reject", "Not in the app's universe."
            elif act not in ("buy", "sell", "short", "cover"):
                entry["decision"], note = "reject", "Unknown action."
            elif act == "short" and tid in LONG_ONLY:
                entry["decision"], note = "reject", "This trader is long-only."
            elif r.get("decision") in ("approve", "resize"):
                w = abs(float(r.get("weight") if r.get("weight") is not None else x.get("weight") or 0))
                minc = trader_min_mcap(tid)
                if act in ("buy", "short") and (mk.rows[t].get("mc") or 0) < minc:
                    entry["decision"], note = "reject", "Below the market-cap floor."
                else:
                    cap = (lim["max_long"] if act == "buy" else lim["max_short"]) * f
                    if act in ("buy", "short") and w > cap:
                        w, note = cap, f"Capped at {cap:.0%} by house limits."
                    target = 0.0 if act in ("sell", "cover") and w == 0 else (w if act in ("buy", "sell") else -w)
                    cur = exposures(tr, mk).get(t, 0.0)
                    if act == "sell" and cur <= 0 or act == "cover" and cur >= 0:
                        entry["decision"], note = "reject", "Nothing to close."
                    else:
                        if act == "sell":
                            target = min(max(target, 0.0), cur)
                        if act == "cover":
                            target = max(min(target, 0.0), cur)
                        queued.append({"round": rid, "trader": tid, "ticker": t, "target": round(target, 4), "action": act, "asof": mk.date})
                        tr["approved"] += 1
            entry["engine_note"] = note
            log_trades.append(entry)
    # portfolio-level checks on the queued book (sector, gross, total short): scale new orders down if needed
    for tid in ids:
        tr = st["traders"][tid]
        f = lim["probation_factor"] if tr["probation"] else 1.0
        ex = exposures(tr, mk)
        mine = [o for o in queued if o["trader"] == tid]
        for o in mine:
            ex[o["ticker"]] = o["target"]
        gross, shorts = sum(abs(v) for v in ex.values()), -sum(v for v in ex.values() if v < 0)
        scale = min(1.0, (lim["max_gross"] * f) / gross if gross else 1, (lim["max_short_total"] * f) / shorts if shorts else 1)
        sect = {}
        for k, v in ex.items():
            s = (mk.rows.get(k) or {}).get("sec", "?")
            sect[s] = sect.get(s, 0) + abs(v)
        for o in mine:
            s = (mk.rows.get(o["ticker"]) or {}).get("sec", "?")
            sc = scale * min(1.0, (lim["max_sector"] * f) / sect[s] if sect.get(s) else 1)
            if sc < 0.999 and o["action"] in ("buy", "short"):
                o["target"] = round(o["target"] * sc, 4)
                for e in log_trades:
                    if e["trader"] == tid and e["ticker"] == o["ticker"]:
                        e["engine_note"] = (e.get("engine_note") or "") + f" Scaled to {abs(o['target']):.1%} to stay within sector/gross limits."
    st["pending"] = [o for o in st["pending"] if (o["trader"], o["ticker"]) not in {(q["trader"], q["ticker"]) for q in queued}] + queued
    cash = {}
    for tid, p in pitches.items():
        if tid in ids:
            try:
                c = float(p.get("cash") or 0)
            except (TypeError, ValueError):
                c = 0.0
            cash[tid] = round(min(1.0, max(0.0, c)), 3) if c > 1 - (lim.get("min_invested") or 0) + 1e-9 else 0.0
    tops = topup(st, mk, rid, cash)
    for o in tops:
        for e in log_trades:
            if e["trader"] == o["trader"] and e["ticker"] == o["ticker"]:
                e["engine_note"] = (e.get("engine_note") or "") + f" Topped up to {o['target']:.1%} to keep the book fully invested."
    summary = {"round": rid, "asof": mk.date, "at": now(), "manager_note": rulings.get("note"),
               "views": {k: (v.get("view") or "") for k, v in pitches.items() if k in ids},
               "cash": {k: {"target": cash[k], "why": pitches[k].get("cash_reason")} for k in cash if cash[k]},
               "counts": {"pitched": len(log_trades), "approved": sum(1 for e in log_trades if e["decision"] in ("approve", "resize") and not (e.get("engine_note") or "").startswith(("Not in", "Below", "Nothing", "Unknown"))),
                          "rejected": sum(1 for e in log_trades if e["decision"] == "reject")},
               "manual": bool(a.manual)}
    st["rounds"].append(summary)
    st["rounds"] = st["rounds"][-104:]
    save(a.state, st)
    save(a.log, {**summary, "trades": log_trades, "pitches": pitches, "rulings": rulings})
    print(f"round {rid}: {summary['counts']}, {len(queued)} orders queued for the next close")
    return 0


def cmd_mark(a) -> int:
    mk = Market(a.app_data, a.prices)
    st = load(a.state)
    ensure_traders(st, mk.date)
    d = mk.date
    cost = CFG["cost_bps"] / 1e4
    fills = []
    # 1) fill orders queued before this session's close
    keep = []
    for o in st["pending"]:
        if o["asof"] >= d:
            keep.append(o)
            continue
        tr = st["traders"][o["trader"]]
        px = mk.price(o["ticker"])
        if not px:
            keep.append(o)
            continue
        eq = equity(tr, mk)
        pos = tr["positions"].get(o["ticker"])
        cur = pos["shares"] if pos else 0.0
        tgt = o["target"] * eq / px
        delta = tgt - cur
        if abs(delta * px) < 1:
            continue
        tr["cash"] -= delta * px + abs(delta * px) * cost
        tr["trades"] += 1
        if abs(tgt) < 1e-9 or (cur and tgt * cur < 0):     # closed (or flipped): book realized P&L on the old position
            if pos:
                pnl = cur * (px - pos["entry"])
                tr["realized"] += pnl
                tr["closed"] = (tr["closed"] + [{"t": o["ticker"], "side": "long" if cur > 0 else "short", "entry": pos["entry"], "exit": px,
                                                 "since": pos["since"], "until": d, "pnl": round(pnl, 2), "why": o["action"]}])[-60:]
            tr["positions"].pop(o["ticker"], None)
            if abs(tgt) >= 1e-9:
                tr["positions"][o["ticker"]] = {"shares": tgt, "entry": px, "since": d, "last": px}
        elif pos and abs(tgt) > abs(cur):                    # adding: average the entry price
            pos["entry"] = (pos["entry"] * cur + px * delta) / tgt
            pos["shares"], pos["last"] = tgt, px
        elif pos:                                            # trimming
            pnl = -delta * (px - pos["entry"])
            tr["realized"] += pnl
            pos["shares"], pos["last"] = tgt, px
        else:
            tr["positions"][o["ticker"]] = {"shares": tgt, "entry": px, "since": d, "last": px}
        fills.append({**o, "price": px, "shares": round(delta, 4), "date": d})
    st["pending"] = keep
    # 2) stops, borrow, marks
    last_mark = st.get("last_mark")
    days = 1 if not last_mark else max(1, (date.fromisoformat(d) - date.fromisoformat(last_mark)).days)
    for tid, tr in st["traders"].items():
        for t, p in list(tr["positions"].items()):
            px = mk.price(t) or p["last"]
            p["last"] = px
            if p["shares"] < 0 and last_mark != d:
                tr["cash"] -= abs(p["shares"] * px) * CFG["borrow_rate"] * days / 365
            move = px / p["entry"] - 1
            if (p["shares"] > 0 and move <= trader_stop_long(tid)) or (p["shares"] < 0 and move >= CFG["stops"]["short"]):
                pnl = p["shares"] * (px - p["entry"])
                tr["cash"] += p["shares"] * px - abs(p["shares"] * px) * cost
                tr["realized"] += pnl
                tr["trades"] += 1
                tr["closed"] = (tr["closed"] + [{"t": t, "side": "long" if p["shares"] > 0 else "short", "entry": p["entry"], "exit": px,
                                                 "since": p["since"], "until": d, "pnl": round(pnl, 2), "why": "stop"}])[-60:]
                fills.append({"trader": tid, "ticker": t, "action": "stop", "price": px, "shares": -p["shares"], "date": d})
                tr["positions"].pop(t)
        eq = equity(tr, mk)
        tr["peak"] = max(tr["peak"], eq)
        tr["hist"] = [h for h in tr["hist"] if h[0] != d] + [[d, round(eq, 2)]]
    if mk.spy:
        st["spy"] = [h for h in st.get("spy", []) if h[0] != d] + [[d, mk.spy]]
        if not st.get("spy0"):
            st["spy0"] = mk.spy
    st["last_mark"] = d
    st["fills"] = (st.get("fills", []) + fills)[-400:]
    save(a.state, st)
    print(f"marked {d}: {len(fills)} fills, {len(keep)} orders still pending; equities " +
          ", ".join(f"{k} {tr['hist'][-1][1]:,.0f}" for k, tr in st["traders"].items()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "prepare", "apply", "mark", "topup"])
    ap.add_argument("--state", required=True)
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--prices")
    ap.add_argument("--research")
    ap.add_argument("--signals", help="signals.json from the Signal Lab (lab-data branch)")
    ap.add_argument("--brief")
    ap.add_argument("--out", default="arena-packets")
    ap.add_argument("--pitches")
    ap.add_argument("--rulings")
    ap.add_argument("--log", default="round.json")
    ap.add_argument("--manual", action="store_true")
    a = ap.parse_args()
    if a.cmd == "init":
        if Path(a.state).exists() and Path(a.state).stat().st_size:
            print("state exists; not overwriting")
            return 1
        save(a.state, init_state())
        print("initialized")
        return 0
    return {"prepare": cmd_prepare, "apply": cmd_apply, "mark": cmd_mark, "topup": cmd_topup}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
