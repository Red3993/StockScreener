"""Track records: who has bought stocks that went on to beat the market?

For each disclosed open-market purchase we measure what a follower could have earned: buy at the first close AFTER the
filing date (the day the public could know), then the return over the next 21 / 63 / 126 trading days minus the S&P 500
(SPY) over the same days. Only stocks in this app's universe can be priced.

Each trade's excess return is capped at +100% / -90% before averaging. People are ranked on a shrunk average of their 63-day excess returns: avg * n / (n + K), so someone with two lucky trades
doesn't outrank someone with a steady record over twenty. Hit rate = share of buys that beat the S&P 500 over 63 days.

  Congress: House + Senate periodic transaction reports, purchases of stock or options (3 years of history).
  Insiders: SEC Form 4 open-market purchases (code P) by officers and directors (1 year of history, so few have
            enough buys yet to rank).

Over time: for each ranked person we also build a "follow portfolio": every disclosed buy is held for 63 trading days from
the first close after disclosure, equal-weighted across whatever is open that day, and its daily return minus SPY's is
compounded into a cumulative excess-return curve. `t63` is a t-statistic on the 3-month excess returns (is the average
distinguishable from zero?), and `years` splits the record by filing year, so a single lucky year stands out.

Small samples and overlapping trades mean these are leads, not proof; past results don't predict future ones.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

H = (21, 63, 126)
K_SHRINK = 4


def _entry_ix(idx: pd.DatetimeIndex, d: str | None) -> int | None:
    if not d:
        return None
    try:
        ts = pd.Timestamp(d[:10])
    except ValueError:
        return None
    k = int(idx.searchsorted(ts, side="right"))   # first session strictly after the filing date
    return k if k < len(idx) else None


def _fwd(px: np.ndarray, bench: np.ndarray, k: int, h: int) -> float | None:
    if k + h >= len(px):
        return None
    p0, p1, b0, b1 = px[k], px[k + h], bench[k], bench[k + h]
    if not all(np.isfinite([p0, p1, b0, b1])) or p0 <= 0 or b0 <= 0:
        return None
    return float((p1 / p0 - 1) - (b1 / b0 - 1))


def _summarize(events: list[dict], min_n: int) -> dict | None:
    r63 = [e["x63"] for e in events if e.get("x63") is not None]
    if len(r63) < min_n:
        return None
    avg = float(np.mean(np.clip(r63, -0.9, 1.0)))   # one +500% biotech pop shouldn't define a track record
    rc = np.clip(r63, -0.9, 1.0)
    sd = float(np.std(rc, ddof=1)) if len(rc) > 2 else None
    t63 = round(avg / (sd / math.sqrt(len(rc))), 2) if sd and sd > 0 else None
    years: dict[str, list] = {}
    for e in events:
        if e.get("x63") is not None:
            years.setdefault(e["filed"][:4], []).append(max(-0.9, min(1.0, e["x63"])))
    out = {"n": len(events), "n63": len(r63), "avg63": round(avg, 4), "med63": round(float(np.median(r63)), 4), "t63": t63,
           "years": {y: [len(v), round(float(np.mean(v)), 4)] for y, v in sorted(years.items())},
           "hit63": round(float(np.mean([x > 0 for x in r63])), 3), "score": round(avg * len(r63) / (len(r63) + K_SHRINK), 4)}
    for h in (21, 126):
        xs = [e[f"x{h}"] for e in events if e.get(f"x{h}") is not None]
        out[f"avg{h}"] = round(float(np.mean(xs)), 4) if xs else None
        out[f"n{h}"] = len(xs)
    best = sorted((e for e in events if e.get("x63") is not None), key=lambda e: -e["x63"])
    out["best"] = [{"t": e["t"], "filed": e["filed"], "x63": round(e["x63"], 4)} for e in best[:3]]
    out["recent"] = [{"t": e["t"], "date": e["date"], "filed": e["filed"]} for e in sorted(events, key=lambda e: e["filed"], reverse=True)[:5]]
    return out


def _curve(events: list[dict], priced, idx: pd.DatetimeIndex, B: np.ndarray, hold: int = 63, max_pts: int = 80) -> list | None:
    """Cumulative excess return of a follow portfolio: each buy held `hold` sessions from the first close after disclosure."""
    n = len(idx)
    contrib = np.zeros(n)
    count = np.zeros(n)
    rb = np.full(n, np.nan)
    rb[1:] = B[1:] / B[:-1] - 1
    first = None
    for e in events:
        px = priced(e["t"])
        k = _entry_ix(idx, e["filed"])
        if px is None or k is None:
            continue
        end = min(n - 1, k + hold)
        if end <= k:
            continue
        seg = px[k:end + 1]
        r = seg[1:] / seg[:-1] - 1
        x = np.clip(r - rb[k + 1:end + 1], -0.5, 0.5)
        ok = np.isfinite(x)
        contrib[k + 1:end + 1][ok] += x[ok]
        count[k + 1:end + 1][ok] += 1
        first = k if first is None else min(first, k)
    if first is None or first >= n - 2:
        return None
    daily = np.where(count[first:] > 0, contrib[first:] / np.maximum(count[first:], 1), 0.0)
    cum = np.cumprod(1 + daily) - 1
    step = max(1, math.ceil(len(cum) / max_pts))
    ix = list(range(0, len(cum), step))
    if ix[-1] != len(cum) - 1:
        ix.append(len(cum) - 1)
    return [[idx[first + i].date().isoformat(), round(float(cum[i]), 4)] for i in ix]


def compute(uclose: pd.DataFrame, bench: pd.Series | None, ytick: dict[str, str], congress: dict[str, list[dict]],
            insiders: dict[str, list[dict]], names: dict[str, str], today: date) -> dict:
    idx = uclose.index
    if bench is None or bench.dropna().size < 200:
        rr = uclose.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).clip(-0.5, 1.0)
        bench = rr.median(axis=1).fillna(0).add(1).cumprod()   # fallback: median app stock
        bname = "median stock in the app"
    else:
        bench = bench.reindex(idx).ffill()
        bname = "S&P 500 (SPY)"
    B = bench.to_numpy(dtype=float)
    C = uclose.ffill(limit=5)

    def priced(t):
        yt = ytick.get(t)
        return C[yt].to_numpy(dtype=float) if yt in C.columns else None

    def measure(t, filed):
        px = priced(t)
        k = _entry_ix(idx, filed)
        if px is None or k is None or not np.isfinite(px[k]):
            return None
        return {h: _fwd(px, B, k, h) for h in H}

    # ---- congress
    people: dict[str, dict] = {}
    seen = set()
    n_all = n_priced = 0
    for t, trades in congress.items():
        for x in trades:
            if "Purchase" not in (x.get("type") or "") or not x.get("filed"):
                continue
            key = (x.get("name"), t, x.get("filed"))
            if key in seen:
                continue
            seen.add(key)
            n_all += 1
            m = measure(t, x["filed"])
            if not m:
                continue
            n_priced += 1
            p = people.setdefault(x.get("name") or "?", {"name": x.get("name"), "party": x.get("party"), "state": x.get("state"),
                                                         "chamber": x.get("chamber"), "events": []})
            p["events"].append({"t": t, "date": x.get("date"), "filed": x["filed"], "x21": m[21], "x63": m[63], "x126": m[126],
                                "relevant": bool(x.get("relevant")), "opt": x.get("asset_type") == "OP"})
    cong_rows = []
    for p in people.values():
        s = _summarize(p["events"], 3)
        if s:
            s.update({k: p[k] for k in ("name", "party", "state", "chamber")})
            s["_events"] = p["events"]
            s["relevant_share"] = round(float(np.mean([e["relevant"] for e in p["events"]])), 2)
            cong_rows.append(s)
    cong_rows.sort(key=lambda r: -r["score"])

    # ---- insiders (person + company)
    ins_people: dict[tuple, dict] = {}
    m_all = m_priced = 0
    for t, trades in insiders.items():
        for x in trades:
            if x.get("code") != "P" or not x.get("date"):
                continue
            filed = str(x.get("filed") or x["date"])[:10]
            m_all += 1
            m = measure(t, filed)
            if not m:
                continue
            m_priced += 1
            key = (x.get("owner"), t)
            p = ins_people.setdefault(key, {"name": x.get("owner"), "t": t, "company": names.get(t), "role": ", ".join((x.get("roles") or [])[:2]),
                                            "events": [], "value": 0.0})
            p["value"] += x.get("value") or 0
            p["events"].append({"t": t, "date": x["date"], "filed": filed, "x21": m[21], "x63": m[63], "x126": m[126]})
    # one event per person per filing date (a Form 4 often lists several lots)
    ins_rows = []
    for p in ins_people.values():
        dd = {}
        for e in p["events"]:
            dd.setdefault(e["filed"], e)
        p["events"] = list(dd.values())
        s = _summarize(p["events"], 2)
        if s:
            s.update({"name": p["name"], "t": p["t"], "company": p["company"], "role": p["role"], "value": round(p["value"])})
            s["_events"] = p["events"]
            ins_rows.append(s)
    ins_rows.sort(key=lambda r: -r["score"])

    # ---- latest buys by people with a strong record (actionable)
    top_c = {r["name"] for r in cong_rows[:25] if r["score"] > 0 and r["hit63"] >= 0.5}
    cut = (pd.Timestamp(today) - pd.Timedelta(days=60)).date().isoformat()
    recent = []
    for t, trades in congress.items():
        for x in trades:
            if x.get("name") in top_c and "Purchase" in (x.get("type") or "") and (x.get("filed") or "") >= cut:
                recent.append({"who": x["name"], "kind": "Congress", "t": t, "date": x.get("date"), "filed": x.get("filed"),
                               "amount": [x.get("amount_low"), x.get("amount_high")], "opt": x.get("asset_type") == "OP"})
    top_i = {(r["name"], r["t"]) for r in ins_rows[:25] if r["score"] > 0}
    for t, trades in insiders.items():
        for x in trades:
            if (x.get("owner"), t) in top_i and x.get("code") == "P" and str(x.get("filed") or x.get("date") or "") >= cut:
                recent.append({"who": x["owner"], "kind": "Insider", "t": t, "date": x.get("date"), "filed": str(x.get("filed") or x["date"])[:10],
                               "value": x.get("value")})
    dd = {}
    for r in recent:
        dd.setdefault((r["who"], r["t"], r["filed"]), r)
    recent = sorted(dd.values(), key=lambda r: r["filed"] or "", reverse=True)[:40]

    def clean(v):
        return None if isinstance(v, float) and not math.isfinite(v) else v

    keep_c = cong_rows[:60] + (cong_rows[-10:] if len(cong_rows) > 20 else [])
    for r in keep_c:     # insiders only buy their own company's stock, so no follow-portfolio curve for them
        r["curve"] = _curve(r["_events"], priced, idx, B)
    for r in cong_rows + ins_rows:
        r.pop("_events", None)
    return {"asof": today.isoformat(), "benchmark": bname, "horizons": list(H), "shrink_k": K_SHRINK,
            "congress": [{k: clean(v) for k, v in r.items()} for r in cong_rows[:60]],
            "congress_bottom": [{k: clean(v) for k, v in r.items()} for r in cong_rows[-10:][::-1]] if len(cong_rows) > 20 else [],
            "insiders": [{k: clean(v) for k, v in r.items()} for r in ins_rows[:60]],
            "recent_by_top": recent,
            "coverage": {"congress_buys": n_all, "congress_priced": n_priced, "congress_ranked": len(cong_rows),
                         "insider_buys": m_all, "insider_priced": m_priced, "insiders_ranked": len(ins_rows)}}
