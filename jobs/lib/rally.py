"""Trend and Rally Setup scores (1-10) plus a walk-forward backtest of both.

Neither score is part of the Overall score. They answer timing questions, while the seven categories answer
quality/value questions.

TREND (1-10)
  Same formula as the Momentum score in momentum.py (35% trend vs 50/200-day averages and their slope,
  35% relative strength 12-1 and 3-month, 30% buying pressure), computed for every stock on every date so it
  can be backtested, then ranked across the universe: 10 = top 10% that day.

RALLY SETUP (1-10): "is this beaten-down or coiled stock likely to turn up?"
  Each signal is scored 0-100 on an absolute scale (events like an insider cluster are either there or not,
  so ranking them against the universe would be meaningless). Signals go into three pillars:

  Catalyst 45%  why a rally could happen
     estimate revisions (EPS estimate change over 90 days)              live only, no free history
     earnings-style gap that is holding (post-earnings drift proxy)     price + volume
     insider buying (cluster of 3+ insiders in 14 days > 2 insiders > 1; CEO/CFO bonus)
     buybacks (share count down over a year)                            live only
  Flow 25%      who is buying
     quiet accumulation (net up-volume while price is flat)             price + volume
     short-squeeze setup (short interest >= ~10% of float, price above its 20-day)   live only
  Setup 30%     how stretched to the downside / coiled it is
     70% max(lagging its correlation cluster, near the 52-week low, bullish RSI divergence)
         (these three all measure "beaten down", so the strongest one counts, not their sum)
     30% volatility squeeze (Bollinger width at a 6-month low)

  Within Catalyst and Flow the pillar is the strongest signal plus a quarter of the second strongest:
  one strong reason is enough, a second adds a little.
  Composite = weighted pillars -> percentile across the universe -> decile.
  Gate: without at least one catalyst signal >= 50 the score is capped at 6, so "merely cheap" can't be a 9 or 10.

  Peer groups: for each stock, the 15 stocks whose daily returns correlated most with it over the past year
  (the correlation-clustermap idea, tailored per stock; hard Ward clusters were tried and put low-correlation
  names like AAPL into grab-bag clusters). Refit every 4 weeks. Divergence = the stock's 40-day return minus
  its peers' mean, divided by the typical size of that gap (z-score), discounted when the stock isn't tightly
  correlated with its peers (full credit at correlation 0.6+).
  Ties in the composite are broken by position in the 52-week range (lower ranks higher).

BACKTEST
  Every 5 trading days from the first date with a year of history to the last date with a full forward
  window: score every stock using only data available that day, then measure its return over the next
  20 and 60 trading days minus the equal-weighted average stock's return that day (so a rising market doesn't
  flatter results). "Hit" = share of stocks in a bucket that beat the median stock (50% = coin flip).
  Signals without history (estimate revisions, buybacks, short interest) are left out of the backtest, and
  insider data only counts from the date the insider cache starts. The universe is today's index members,
  which flatters results slightly (survivorship bias). Significance (t-stat) uses non-overlapping periods.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

W_CAT, W_FLOW, W_SETUP = 0.45, 0.25, 0.30
GATE_CAP, FIRE = 6, 50.0
DIV_DAYS, PEERS = 40, 15
LABELS = {
    "revisions": "EPS estimate revisions", "drift": "Gap up that is holding", "insider": "Insider buying", "buyback": "Buybacks",
    "accumulation": "Quiet accumulation", "squeeze": "Short-squeeze setup",
    "peer_lag": "Lagging its peer group", "near_low": "Near 52-week low", "rsi_div": "Bullish RSI divergence", "vol_squeeze": "Volatility squeeze",
}
PILLAR_OF = {"revisions": "catalyst", "drift": "catalyst", "insider": "catalyst", "buyback": "catalyst",
             "accumulation": "flow", "squeeze": "flow",
             "peer_lag": "setup", "near_low": "setup", "rsi_div": "setup", "vol_squeeze": "setup"}


def _clip01(x):
    return np.clip(x, 0, 1)


def _decile(p: float) -> int:
    return 1 + min(9, int(p * 10))


def _soft_max(vals: list[float | None]) -> float:
    v = sorted((x for x in vals if x is not None and not math.isnan(x)), reverse=True)
    if not v:
        return 0.0
    return min(100.0, v[0] + (0.25 * v[1] if len(v) > 1 else 0.0))


def _rsi(c: pd.DataFrame, w: int = 14) -> pd.DataFrame:
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = up.ewm(alpha=1 / w, adjust=False).mean(), dn.ewm(alpha=1 / w, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, np.nan))


def _nanwavg(parts: list[tuple[np.ndarray, float]]) -> np.ndarray:
    num = np.zeros_like(parts[0][0], dtype=float)
    den = np.zeros_like(parts[0][0], dtype=float)
    for x, w in parts:
        ok = ~np.isnan(x)
        num += np.where(ok, x, 0) * w
        den += ok * w
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


class Frames:
    """Every price/volume signal for every stock and date, computed once."""

    def __init__(self, C: pd.DataFrame, V: pd.DataFrame | None) -> None:
        C = C.ffill(limit=5)
        self.C = C
        self.cnt = C.notna().cumsum()
        n = len(C)
        r = C.pct_change(fill_method=None)
        self.r = r
        hasv = V is not None and len(V) > 0
        V = V.reindex(index=C.index, columns=C.columns) if hasv else pd.DataFrame(np.nan, index=C.index, columns=C.columns)
        self.V = V

        # setup signals
        sma20, sd20 = C.rolling(20).mean(), C.rolling(20).std()
        bbw = sd20 / sma20
        sq_p = bbw.rolling(126, min_periods=100).rank(pct=True)
        self.vol_squeeze = _clip01((0.35 - sq_p) / 0.35) * 100
        hi, lo = C.rolling(252, min_periods=200).max(), C.rolling(252, min_periods=200).min()
        self.pos = (C - lo) / (hi - lo).replace(0, np.nan)
        falling = C <= C.rolling(10).min() * 1.005                       # still printing 10-day lows: half credit
        self.near_low = _clip01((0.4 - self.pos) / 0.4) * 100 * np.where(falling, 0.5, 1.0)
        rsi = _rsi(C)
        self.rsi = rsi
        L1, L2 = C.rolling(20).min().shift(20), C.rolling(20).min()
        R1, R2 = rsi.rolling(20).min().shift(20), rsi.rolling(20).min()
        cond = (L2 < L1) & (R2 > R1 + 3) & (R1 < 35)
        self.rsi_div = pd.DataFrame(np.where(cond, 80 + 20 * (rsi > R2 + 8), 0.0), index=C.index, columns=C.columns)

        # flow
        sgn = np.sign(r).fillna(0)
        vsum20 = V.rolling(20).sum()
        self.net20 = (sgn * V).rolling(20).sum() / vsum20.replace(0, np.nan)
        r20 = C / C.shift(20) - 1
        self.accumulation = _clip01((self.net20 - 0.05) / 0.35) * 100 * np.where(r20 < 0.05, 1.0, 0.6)
        self.sma20 = sma20

        # earnings-style gap that is holding (post-earnings drift proxy)
        std20p = r.rolling(20).std().shift(1)
        vavg50p = V.rolling(50).mean().shift(1)
        ev = (r >= 0.05) & (r >= 3 * std20p) & (V >= 2 * vavg50p)
        idx = pd.DataFrame(np.repeat(np.arange(n)[:, None], C.shape[1], axis=1), index=C.index, columns=C.columns)
        evpos = idx.where(ev).ffill(limit=60)
        self.ev_ret = r.where(ev).ffill(limit=60)
        self.ev_age = idx - evpos
        evclose = C.where(ev).ffill(limit=60)
        strength = _clip01(self.ev_ret / 0.15) * 100 * (1 - 0.5 * self.ev_age / 60)
        self.drift = strength.where(C >= 0.97 * evclose, 0).fillna(0).where(V.notna() | C.isna())

        # trend (mirrors momentum.py)
        s50, s200 = C.rolling(50).mean(), C.rolling(200).mean()
        rk = lambda f: f.rank(axis=1, pct=True).to_numpy() * 100   # noqa: E731
        trend = _nanwavg([(rk(C / s50 - 1), 1), (rk(C / s200 - 1), 1), (rk(s50 / s200 - 1), 1), (rk(s200 / s200.shift(20) - 1), 1)])
        rel = _nanwavg([(rk(C.shift(21) / C.shift(252) - 1), 1), (rk(C / C.shift(63) - 1), 1)])
        up_share = V.where(r > 0, 0).rolling(20).sum() / vsum20.replace(0, np.nan)
        flow = _nanwavg([(_clip01((up_share.to_numpy() - 0.3) / 0.4) * 100, 1), (_clip01(self.net20.to_numpy() + 0.5) * 100, 1)])
        tr = _nanwavg([(trend, .35), (rel, .35), (flow, .30)])
        tr[np.isnan(s200.to_numpy())] = np.nan
        self.trend = pd.DataFrame(tr, index=C.index, columns=C.columns)


def fit_peers(r: pd.DataFrame, t: int) -> dict:
    """Each stock's peer group = the PEERS stocks whose daily returns correlated most with it over the year ending at
    row t (the idea behind a correlation clustermap, but tailored per stock so no one lands in a grab-bag cluster)."""
    from scipy import sparse
    win = r.iloc[max(0, t - 251):t + 1]
    cols = [c for c in win.columns if win[c].notna().sum() >= 200]
    if len(cols) < PEERS + 1:
        return {"cols": [], "W": None, "members": {}}
    Z = ((win[cols] - win[cols].mean()) / win[cols].std().replace(0, np.nan)).fillna(0).to_numpy()
    Cm = (Z.T @ Z) / len(Z)
    np.fill_diagonal(Cm, -np.inf)
    nb = np.argpartition(-Cm, PEERS, axis=1)[:, :PEERS]
    rows_ = nb.ravel()
    cols_ = np.repeat(np.arange(len(cols)), PEERS)
    W = sparse.csr_matrix((np.ones(rows_.size), (rows_, cols_)), shape=(len(cols), len(cols)))   # column j sums peers of j
    members = {}
    for j, c in enumerate(cols):
        o = nb[j][np.argsort(-Cm[j, nb[j]])]
        members[c] = [cols[k] for k in o]
    return {"cols": cols, "W": W, "members": members}


def peer_lag(r: pd.DataFrame, t: int, P: dict) -> pd.DataFrame:
    """Per stock at row t: 40-day return gap vs its peers' mean, its z-score, correlation with the peers, score 0-100."""
    cols = [c for c in P["cols"] if c in r.columns]
    if not cols or len(cols) != len(P["cols"]):
        cols = P["cols"]
    if not cols:
        return pd.DataFrame(columns=["gap", "z", "corr", "score"])
    win = r.iloc[max(0, t - 251):t + 1].reindex(columns=cols)
    X = win.to_numpy()
    ok = ~np.isnan(X)
    X0 = np.where(ok, X, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        M = (P["W"].T @ X0.T).T / (P["W"].T @ ok.T.astype(float)).T
        resid = X - M
        valid = ~np.isnan(resid)
        sd = np.nanstd(resid, axis=0) * math.sqrt(DIV_DAYS)
        gap = np.nansum(resid[-DIV_DAYS:], axis=0)
        z = gap / sd
        xm, mm = np.where(valid, X, np.nan), np.where(valid, M, np.nan)
        xc, mc = xm - np.nanmean(xm, axis=0), mm - np.nanmean(mm, axis=0)
        corr = np.nansum(xc * mc, axis=0) / np.sqrt(np.nansum(xc ** 2, axis=0) * np.nansum(mc ** 2, axis=0))
    enough = valid[-DIV_DAYS:].sum(axis=0) >= DIV_DAYS * 0.8
    score = np.where(enough, _clip01((-z - 0.25) / 2) * 100 * _clip01(corr / 0.6), np.nan)
    return pd.DataFrame({"gap": gap, "z": z, "corr": corr, "score": score}, index=cols)


def insider_score(events: list[dict], d: date, since: date | None) -> tuple[float | None, str | None]:
    """events: open-market buys {date, filed, owner, ceo}. Uses only filings public by d."""
    if since is None or d < since:
        return None, None
    lo = d - timedelta(days=90)
    ev = [e for e in events if lo < e["date"] <= d and (e.get("filed") or e["date"]) <= d]
    if not ev:
        return 0.0, None
    ev.sort(key=lambda e: e["date"])
    cluster_end = None
    for i, e in enumerate(ev):
        names = {x["owner"] for x in ev[i:] if x["date"] <= e["date"] + timedelta(days=14)}
        if len(names) >= 3:
            cluster_end = max(x["date"] for x in ev[i:] if x["date"] <= e["date"] + timedelta(days=14))
    owners = {e["owner"] for e in ev}
    exec_buy = any(e.get("ceo") for e in ev)
    if cluster_end:
        s, txt = (100.0 if (d - cluster_end).days <= 60 else 75.0), f"Cluster buy: 3+ insiders within 14 days (ended {cluster_end.isoformat()})"
    elif len(owners) >= 2:
        s, txt = 65.0, f"{len(owners)} different insiders bought in 90 days"
    else:
        s, txt = 40.0, "1 insider bought in 90 days"
    if exec_buy:
        s, txt = min(100.0, s + 15), txt + " (incl. CEO/CFO)"
    return s, txt


def _combine(sig: dict[str, float | None]) -> tuple[float, dict, bool]:
    cat = _soft_max([sig.get(k) for k in ("revisions", "drift", "insider", "buyback")])
    flow = _soft_max([sig.get(k) for k in ("accumulation", "squeeze")])
    beaten = max([x for x in (sig.get("peer_lag"), sig.get("near_low"), sig.get("rsi_div")) if x is not None and not math.isnan(x)] or [0.0])
    vs = sig.get("vol_squeeze")
    setup = 0.7 * beaten + 0.3 * (0.0 if vs is None or math.isnan(vs) else vs)
    fires = any((sig.get(k) or 0) >= FIRE for k in ("revisions", "drift", "insider", "buyback"))
    return W_CAT * cat + W_FLOW * flow + W_SETUP * setup, {"catalyst": round(cat, 1), "flow": round(flow, 1), "setup": round(setup, 1)}, fires


def _val(f: pd.DataFrame, t: int, c: str) -> float | None:
    v = f.iat[t, f.columns.get_loc(c)]
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def score_date(F: Frames, t: int, peers: dict, insiders: dict[str, list], since: date | None,
               extras: dict[str, dict] | None = None) -> dict[str, dict]:
    """All stocks at row t. extras (live only): revisions/buyback/short interest inputs."""
    C = F.C
    d = C.index[t].date()
    cnt, now = F.cnt.iloc[t], C.iloc[t]
    alive = [c for c in C.columns if not pd.isna(now[c]) and cnt[c] >= 252]
    if not alive:
        return {}
    pl = peer_lag(F.r, t, peers)
    rows = {}
    for c in alive:
        sig: dict[str, float | None] = {
            "drift": _val(F.drift, t, c), "accumulation": _val(F.accumulation, t, c),
            "peer_lag": None if c not in pl.index or pd.isna(pl.at[c, "score"]) else float(pl.at[c, "score"]),
            "near_low": _val(F.near_low, t, c), "rsi_div": _val(F.rsi_div, t, c), "vol_squeeze": _val(F.vol_squeeze, t, c),
        }
        ins, ins_txt = insider_score(insiders.get(c, []), d, since)
        sig["insider"] = ins
        x = (extras or {}).get(c) if extras is not None else None
        if x is not None:
            x = {k: (None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v) for k, v in x.items()}
        if x is not None:
            rev, net = x.get("eps_rev_90d"), x.get("eps_rev_net_30d")
            if rev is not None:
                sig["revisions"] = float(_clip01((rev - 0.01) / 0.14) * 100)
            elif net is not None:
                sig["revisions"] = float(_clip01((net - 0.2) / 0.6) * 100)
            sc = x.get("share_change_1y")
            if sc is not None:
                sig["buyback"] = float(_clip01((-sc - 0.02) / 0.06) * 100)
            si = x.get("short_pct_float")
            if si is not None:
                above = (_val(F.C, t, c) or 0) > (_val(F.sma20, t, c) or float("inf"))
                sig["squeeze"] = float(_clip01((si - 0.10) / 0.20) * 100 * (1 if above else 0.4))
        sig = {k: (None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v) for k, v in sig.items()}
        comp, pillars, fires = _combine(sig)
        rows[c] = {"sig": sig, "comp": comp, "pillars": pillars, "fires": fires, "ins_txt": ins_txt,
                   "pl": pl.loc[c].to_dict() if c in pl.index else {}, "trend": _val(F.trend, t, c)}
    tb = {c: 0.001 * (1 - min(1.0, max(0.0, _val(F.pos, t, c) if _val(F.pos, t, c) is not None else 0.5))) for c in rows}
    comps = pd.Series({c: v["comp"] + tb[c] for c, v in rows.items()}).rank(pct=True)
    trs = pd.Series({c: v["trend"] for c, v in rows.items() if v["trend"] is not None}).rank(pct=True)
    for c, v in rows.items():
        dec = _decile(float(comps[c]))
        v["pct"] = float(comps[c])
        v["rally"] = dec if v["fires"] else min(dec, GATE_CAP)
        v["gated"] = (not v["fires"]) and dec > GATE_CAP
        v["trend_dec"] = _decile(float(trs[c])) if c in trs.index else None
    return rows


def _drivers(F: Frames, t: int, c: str, v: dict, x: dict | None, members: list[str]) -> list[dict]:
    sig, out = v["sig"], []

    def add(k, txt):
        s = sig.get(k)
        if s is not None and s >= FIRE:
            out.append({"k": k, "s": round(s), "pillar": PILLAR_OF[k], "txt": txt})
    x = x or {}
    if sig.get("revisions") is not None:
        add("revisions", f"EPS estimates {x['eps_rev_90d'] * 100:+.1f}% over 90 days" if x.get("eps_rev_90d") is not None
            else f"Net {x.get('eps_rev_net_30d', 0) * 100:+.0f}% of analysts revised EPS up in 30 days")
    er, ea = _val(F.ev_ret, t, c), _val(F.ev_age, t, c)
    add("drift", f"Jumped {er * 100:+.0f}% on heavy volume {int(ea)} trading days ago and is holding the gain" if er is not None and ea is not None else "")
    add("insider", v.get("ins_txt") or "")
    if x.get("share_change_1y") is not None:
        add("buyback", f"Share count {x['share_change_1y'] * 100:+.1f}% over a year (buybacks)")
    n20 = _val(F.net20, t, c)
    add("accumulation", f"{(n20 or 0) * 100:.0f}% of the last 20 days' volume net to the upside while price stayed flat")
    if x.get("short_pct_float") is not None:
        add("squeeze", f"Short interest {x['short_pct_float'] * 100:.0f}% of float with price above its 20-day average")
    pl = v["pl"]
    if pl.get("gap") is not None and not math.isnan(pl["gap"]):
        add("peer_lag", f"Trailing its peer group by {-pl['gap'] * 100:.0f} pts over 40 days ({pl['z']:.1f}σ)" + (f": {', '.join(members[:5])}" if members else ""))
    pos = _val(F.pos, t, c)
    add("near_low", f"In the bottom {max(1, (pos or 0) * 100):.0f}% of its 52-week range")
    add("rsi_div", "Bullish RSI divergence: lower price low, higher RSI low")
    add("vol_squeeze", "Volatility squeeze: daily ranges at a 6-month low, often before a big move")
    return sorted(out, key=lambda d: (-{"catalyst": 3, "flow": 2, "setup": 1}[d["pillar"]], -d["s"]))


def compute(C: pd.DataFrame, V: pd.DataFrame | None, sectors: dict[str, str], insiders: dict[str, list],
            insider_since: date | None, extras: dict[str, dict], step: int = 5, refit: int = 4, log=print) -> dict:
    fin = lambda v: None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v  # noqa: E731
    extras = {c: {k: fin(v) for k, v in (x or {}).items()} for c, x in (extras or {}).items()}
    F = Frames(C, V)
    n = len(F.C)
    out: dict = {"live": {}, "hist": {}, "bt_dates": [], "backtest": None}
    if n < 280:
        log("rally: not enough history")
        return out
    # ---------------- backtest
    start = 260
    bt_idx = list(range(start, n - 1, step))
    acc = {s: {h: {k: [] for k in range(1, 11)} for h in (20, 60)} for s in ("rally", "trend")}
    per = {s: {h: [] for h in (20, 60)} for s in ("rally", "trend")}   # per-date (date, top-bucket excess, bottom excess)
    hist = {c: [[], []] for c in F.C.columns}
    peers, last_fit = {}, None
    for j, t in enumerate(bt_idx):
        if last_fit is None or j - last_fit >= refit:
            peers, last_fit = fit_peers(F.r, t), j
        rows = score_date(F, t, peers, insiders, insider_since)
        out["bt_dates"].append(F.C.index[t].date().isoformat())
        for c in F.C.columns:
            v = rows.get(c)
            hist[c][0].append(v["rally"] if v else None)
            hist[c][1].append(v["trend_dec"] if v else None)
        for h in (20, 60):
            if t + h > n - 1:
                continue
            fwd = F.C.iloc[t + h] / F.C.iloc[t] - 1
            fwd = fwd[[c for c in rows if not pd.isna(fwd.get(c))]]
            if len(fwd) < 50:
                continue
            ex = fwd - fwd.mean()          # vs the equal-weighted average stock that day (median would make every bucket look positive)
            above_med = fwd > fwd.median()
            for s, key in (("rally", "rally"), ("trend", "trend_dec")):
                top, bot = [], []
                for c, e in ex.items():
                    b = rows[c][key]
                    if b is None:
                        continue
                    acc[s][h][b].append((float(e), bool(above_med[c])))
                    (top if b >= 9 else bot if b <= 3 else []).append(float(e))
                if top and bot:
                    per[s][h].append((F.C.index[t].date().isoformat(), j, float(np.mean(top)), float(np.mean(bot))))
        if j % 20 == 0:
            log(f"rally backtest {j}/{len(bt_idx)}")
    res = {"step_days": step, "dates": [out["bt_dates"][0], out["bt_dates"][-1]] if out["bt_dates"] else None,
           "insider_since": insider_since.isoformat() if insider_since else None, "scores": {}}
    for s in ("rally", "trend"):
        res["scores"][s] = {}
        for h in (20, 60):
            buckets = []
            for k in range(1, 11):
                a = np.array([x[0] for x in acc[s][h][k]])
                hm = np.array([x[1] for x in acc[s][h][k]])
                buckets.append({"b": k, "n": int(a.size), "avg": round(float(a.mean()), 5) if a.size else None,
                                "med": round(float(np.median(a)), 5) if a.size else None,
                                "hit": round(float(hm.mean()), 4) if a.size else None})   # share that beat the median stock (50% = coin flip)
            P = per[s][h]
            stride = max(1, h // step)
            non = [p for p in P if (p[1] - P[0][1]) % stride == 0] if P else []
            tops = np.array([p[2] for p in non])
            spr = np.array([p[2] - p[3] for p in non])
            tstat = lambda a: round(float(a.mean() / (a.std(ddof=1) / math.sqrt(a.size))), 2) if a.size > 2 and a.std(ddof=1) > 0 else None  # noqa: E731
            curve, cum = [], 1.0
            for p in non:
                cum *= 1 + p[2]
                curve.append([p[0], round(cum - 1, 4)])
            res["scores"][s][str(h)] = {
                "buckets": buckets,
                "top": {"periods": int(tops.size), "avg": round(float(tops.mean()), 5) if tops.size else None,
                        "beat_pct": round(float((tops > 0).mean()), 3) if tops.size else None, "t": tstat(tops)},
                "spread": {"avg": round(float(spr.mean()), 5) if spr.size else None, "t": tstat(spr)},
                "curve": curve,
            }
    out["backtest"] = res
    out["hist"] = hist

    # ---------------- live (last date, with live-only signals)
    t = n - 1
    peers = fit_peers(F.r, t)
    rows = score_date(F, t, peers, insiders, insider_since, extras)
    num = lambda x, d: None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), d)  # noqa: E731
    for c, v in rows.items():
        members = peers["members"].get(c, [])
        out["live"][c] = {
            "rally": v["rally"], "trend": v["trend_dec"], "gated": v["gated"], "composite": round(v["comp"], 1), "pct": round(v["pct"], 3),
            "pillars": v["pillars"], "catalyst_fires": v["fires"],
            "signals": {k: (None if s is None else round(s)) for k, s in v["sig"].items()},
            "drivers": _drivers(F, t, c, v, extras.get(c), members),
            "peer_group": {"members": members[:8], "size": len(members), "gap_40d": num(v["pl"].get("gap"), 4),
                           "z": num(v["pl"].get("z"), 2), "corr": num(v["pl"].get("corr"), 2)},
        }
    log(f"rally: live {len(out['live'])} stocks, {sum(1 for v in out['live'].values() if v['rally'] == 10)} tens")
    return out
