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
    return [{k: t.get(k) for k in ("id", "name", "color", "style", "blurb")} for t in CFG["traders"]] + [{"id": "manager", "name": CFG["manager"]["name"], "blurb": CFG["manager"].get("blurb")}]


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
            f"analysts {a.get('n')} rating {a.get('rm')} up/down 90d {a.get('ug')}/{a.get('dg')} target {a.get('tm')} | short interest {fnum(mo.get('si'), 1, True).lstrip('+')} of float"
            f" | insider buys 90d {i.get('b90', 0)}{' (cluster)' if i.get('cl') else ''}{' (CEO/CFO)' if i.get('ceo') else ''}, disc. sales {i.get('ds90', 0)}"
            f" | congress buys 90d {cg.get('b90', 0)} | earnings {e.get('earn') or 'n/a'}"]
    return "\n  ".join(bits)


def shortlist(mk: Market, tid: str, research: dict | None) -> tuple[list[str], list[str]]:
    lim = CFG["limits"]
    minc = lim["yolo_min_mcap"] if tid == "yolo" else lim["min_mcap"]
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
        hot = {}
        tr = mk.idx.get("track") or {}
        for x in tr.get("recent_by_top", []):
            hot[x["t"]] = hot.get(x["t"], 0) + 3
        for x in ((research or {}).get("consensus") or {}).get("most_bought", []):
            if x.get("t"):
                hot[x["t"]] = hot.get(x["t"], 0) + len(x.get("funds", []))
        sold = {}
        for x in ((research or {}).get("consensus") or {}).get("most_sold", []):
            if x.get("t"):
                sold[x["t"]] = sold.get(x["t"], 0) + len(x.get("funds", []))
        long = sorted(R, key=lambda r: -(hot.get(r["t"], 0) * 3 + (4 if g(r, "i", "cl") else 0) + g(r, "i", "b90") + 2 * g(r, "cg", "b90")))
        short = sorted(R, key=lambda r: -(sold.get(r["t"], 0) * 3 + g(r, "i", "ds90") - g(r, "i", "b90")))
    elif tid == "yolo":
        def soon(r):
            ed = g(r, "e", "earn", None)
            return 1 if ed and mk.date < ed <= f"{mk.date[:8]}{min(28, int(mk.date[8:]) + 10):02d}" else 0
        long = sorted(R, key=lambda r: -((g(r, "mo", "si") or 0) * 20 + (g(r, "ry", "r") or 0) + abs(g(r, "d", "r21") or 0) * 15 + 3 * soon(r)))
        short = sorted(R, key=lambda r: -((g(r, "mo", "st") or 0) / 8 + (g(r, "d", "r63") or 0) * 8 - cs(r, 1)))
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
    brief = load(a.brief) if a.brief and Path(a.brief).exists() else None
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lim = CFG["limits"]
    rules = (f"House rules (enforced by code after the Floor Manager rules): long positions up to {lim['max_long']:.0%} of your equity, shorts up to "
             f"{lim['max_short']:.0%}, total shorts up to {lim['max_short_total']:.0%}, longs + shorts up to {lim['max_gross']:.0%}, one sector up to "
             f"{lim['max_sector']:.0%}; limits halve on probation. Minimum market cap ${lim['min_mcap'] / 1e9:.0f}B (YOLO ${lim['yolo_min_mcap'] / 1e6:.0f}M). "
             f"Orders fill at the next session's close, after you pitch; trading costs {CFG['cost_bps'] / 100:.2f}%; shorts pay {CFG['borrow_rate']:.0%}/yr borrow. "
             f"Stops (automatic): longs close {CFG['stops']['long']:.0%} below entry, shorts cover {CFG['stops']['short']:+.0%} above entry.")
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
        board.append(f"{t['name']} ({t['id']}): equity ${eq:,.0f} ({eq / CFG['start_cash'] - 1:+.1%} since start), cash ${tr['cash']:,.0f}, "
                     f"peak ${tr['peak']:,.0f}{', ON PROBATION' if tr['probation'] else ''}. Book: {book}." + (f" Unfilled orders: {pend}." if pend else ""))
        lg, sh = shortlist(mk, t["id"], research)
        held = [k for k in tr["positions"] if k in mk.rows and k not in lg and k not in sh]
        txt = f"""{t['prompt'].strip()}

Goal: maximize your return over the coming weeks within the rules. It is {mk.date} (data as of that close). Round {st['round'] + 1}.
{rules}

YOUR BOOK
{board[-1]}

{mkt}

LONG IDEAS that fit your style (from the app's data; you may also pick any stock below):
""" + "\n".join("- " + line(mk.rows[k]) for k in lg) + "\n\nSHORT IDEAS that fit your style:\n" + "\n".join("- " + line(mk.rows[k]) for k in sh) + (
            "\n\nYOUR CURRENT HOLDINGS:\n" + "\n".join("- " + line(mk.rows[k]) for k in held) if held else "") + """

Use only facts from the data above. Reply with JSON only:
{"view": "1-2 sentences on how you see the market this week",
 "trades": [{"ticker": "XYZ", "action": "buy|sell|short|cover", "weight": target share of your equity after the trade (0.10 = 10%; for shorts give a positive number; 0 to close),
             "thesis": "1-2 sentences in your own voice", "evidence": ["2-4 specific facts from the data"]}]}
Propose 0-6 trades. "sell" and "cover" close or reduce a position; "buy" and "short" open or add to one."""
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


def cmd_apply(a) -> int:
    mk = Market(a.app_data, a.prices)
    st = load(a.state)
    pitches, rulings = load(a.pitches), load(a.rulings)
    lim = CFG["limits"]
    ids = {t["id"] for t in CFG["traders"]}
    st["round"] += 1
    rid = st["round"]
    st["personas"] = personas()
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
        for x in (p.get("trades") or [])[:8]:
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
            elif r.get("decision") in ("approve", "resize"):
                w = abs(float(r.get("weight") if r.get("weight") is not None else x.get("weight") or 0))
                minc = lim["yolo_min_mcap"] if tid == "yolo" else lim["min_mcap"]
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
    summary = {"round": rid, "asof": mk.date, "at": now(), "manager_note": rulings.get("note"),
               "views": {k: (v.get("view") or "") for k, v in pitches.items() if k in ids},
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
            if (p["shares"] > 0 and move <= CFG["stops"]["long"]) or (p["shares"] < 0 and move >= CFG["stops"]["short"]):
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
    ap.add_argument("cmd", choices=["init", "prepare", "apply", "mark"])
    ap.add_argument("--state", required=True)
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--prices")
    ap.add_argument("--research")
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
    return {"prepare": cmd_prepare, "apply": cmd_apply, "mark": cmd_mark}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
