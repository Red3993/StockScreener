"""Momentum & sentiment model.

Two separate readings, because "how strong is the move" and "how stretched is it" are different questions:

  Momentum score (0-100)  = 35% Trend + 35% Relative strength + 30% Buying pressure
     Trend:             price vs 50-day and 200-day averages, 50 vs 200, slope of the 200-day (each ranked vs universe)
     Relative strength: 12-month return excluding the latest month (classic 12-1), plus 3-month return, ranked vs universe
     Buying pressure:   up-volume share over 20 days, On-Balance Volume slope (20 days), volume vs its 6-month norm

  Stretch (0-100)         = how extended price is: RSI-14, distance above the 200-day measured against the stock's
                            own 3-year history (z-score), and how close it sits to the 52-week high

  Sentiment context (not in the score, shown alongside): options put/call ratio (volume and open interest,
  nearest two expiries), short interest % of float and its month-over-month change, analyst estimate revisions.

  Regime (what the reader actually wants to know):
     Falling knife     weak momentum, downtrend intact, still making new lows, no sign of stabilizing
     Basing / turning  weak momentum but stabilizing: RSI recovering from oversold, price reclaiming the 50-day,
                       MACD bull cross, higher low, or fear (high put/call) at depressed prices
     Healthy uptrend   strong momentum without being stretched
     Overextended      strong momentum and stretched: RSI > ~75 and/or > 2 standard deviations above the 200-day,
                       often with complacent options positioning (low put/call)
     Neutral           everything else
"""
from __future__ import annotations

import math
from bisect import bisect_left

import numpy as np
import pandas as pd


def _pct_rank(sorted_vals: list[float], v: float | None) -> float | None:
    if v is None or not sorted_vals or (isinstance(v, float) and math.isnan(v)):
        return None
    return bisect_left(sorted_vals, v) / len(sorted_vals) * 100


def _rsi(c: pd.Series, w: int = 14) -> pd.Series:
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = up.ewm(alpha=1 / w, adjust=False).mean(), dn.ewm(alpha=1 / w, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, np.nan))


def universe_stats(closes: pd.DataFrame) -> dict[str, list[float]]:
    """Sorted cross-sectional distributions used to rank each stock vs the universe."""
    c = closes.ffill()
    last = c.iloc[-1]
    out = {}

    def col(series):
        return sorted(float(x) for x in series.dropna() if np.isfinite(x))
    if len(c) > 260:
        out["r12_1"] = col(c.iloc[-22] / c.iloc[-253] - 1)
    out["r3m"] = col(last / c.iloc[-64] - 1) if len(c) > 64 else []
    s50, s200 = c.rolling(50).mean().iloc[-1], c.rolling(200).mean()
    out["vs50"] = col(last / s50 - 1)
    out["vs200"] = col(last / s200.iloc[-1] - 1)
    out["gap"] = col(s50 / s200.iloc[-1] - 1)
    out["slope200"] = col(s200.iloc[-1] / s200.iloc[-21] - 1) if len(c) > 221 else []
    return out


def momentum_block(close: pd.Series, volume: pd.Series | None, U: dict, options: dict | None, short: dict | None,
                   eps_rev_90d: float | None, insider_buys_90d: int) -> dict:
    c = close.dropna()
    if len(c) < 210:
        return {"score": None, "regime": "Not enough history", "reasons": []}
    last = float(c.iloc[-1])
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    rsi = _rsi(c)
    vs50, vs200 = last / s50.iloc[-1] - 1, last / s200.iloc[-1] - 1
    gap = s50.iloc[-1] / s200.iloc[-1] - 1
    slope = s200.iloc[-1] / s200.iloc[-21] - 1
    r12_1 = c.iloc[-22] / c.iloc[-253] - 1 if len(c) > 253 else None
    r3m = last / c.iloc[-64] - 1
    trend = np.nanmean([_pct_rank(U["vs50"], vs50), _pct_rank(U["vs200"], vs200), _pct_rank(U["gap"], gap),
                        _pct_rank(U.get("slope200", []), slope) if U.get("slope200") else np.nan])
    rel = np.nanmean([x for x in [(_pct_rank(U.get("r12_1", []), r12_1) if r12_1 is not None else None),
                                  _pct_rank(U["r3m"], r3m)] if x is not None] or [np.nan])
    flow, up_share, obv_slope, rel_vol = np.nan, None, None, None
    if volume is not None and volume.dropna().size > 130:
        v = volume.reindex(c.index).fillna(0)
        chg = c.diff()
        last20 = slice(-20, None)
        tot = v.iloc[last20].sum()
        up_share = float(v.iloc[last20][chg.iloc[last20] > 0].sum() / tot) if tot else None
        obv = (np.sign(chg).fillna(0) * v).cumsum()
        avgv = v.iloc[-20:].mean()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-21]) / (avgv * 20)) if avgv else None   # -1..1: share of 20d volume net-up
        rel_vol = float(v.iloc[-20:].mean() / v.iloc[-126:].mean()) if v.iloc[-126:].mean() else None
        parts = []
        if up_share is not None:
            parts.append(max(0, min(100, (up_share - 0.3) / 0.4 * 100)))
        if obv_slope is not None:
            parts.append(max(0, min(100, (obv_slope + 0.5) * 100)))
        flow = np.mean(parts) if parts else np.nan
    w = [(trend, .35), (rel, .35), (flow, .30)]
    w = [(x, k) for x, k in w if not (x is None or np.isnan(x))]
    score = sum(x * k for x, k in w) / sum(k for _, k in w) if w else None

    # stretch
    dist = c / s200 - 1
    hist = dist.dropna().iloc[-756:]
    z = float((vs200 - hist.mean()) / hist.std()) if hist.std() else 0.0
    hi52 = c.iloc[-252:].max()
    near_high = last / hi52
    r_now = float(rsi.iloc[-1])
    stretch = float(np.clip(0.4 * np.clip((r_now - 30) / 50 * 100, 0, 100) + 0.4 * np.clip((z + 1) / 3.5 * 100, 0, 100)
                            + 0.2 * np.clip((near_high - 0.8) / 0.2 * 100, 0, 100), 0, 100))

    # sentiment context
    pc_vol = pc_oi = None
    if options and options.get("call_vol"):
        pc_vol = options["put_vol"] / options["call_vol"] if options["call_vol"] else None
        pc_oi = options["put_oi"] / options["call_oi"] if options.get("call_oi") else None
    si = (short or {}).get("shortPercentOfFloat")
    si_chg = None
    if short and short.get("sharesShort") and short.get("sharesShortPriorMonth"):
        si_chg = short["sharesShort"] / short["sharesShortPriorMonth"] - 1

    # regime
    low10 = c.iloc[-10:].min() <= c.iloc[-252:].min() * 1.001
    rsi10_min = float(rsi.iloc[-10:].min())
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    macd_cross_up = bool(((macd > sig) & (macd.shift() <= sig.shift())).iloc[-10:].any())
    reclaimed50 = last > s50.iloc[-1] and bool((c.iloc[-15:-1] < s50.iloc[-15:-1]).any())
    lows = c.iloc[-40:]
    higher_low = lows.iloc[-20:].min() > lows.iloc[:20].min()
    stabilizing = [x for x, ok in [("RSI recovering from oversold", rsi10_min < 35 and r_now > rsi10_min + 8),
                                   ("Price reclaimed the 50-day average", reclaimed50),
                                   ("MACD bullish cross in the last 2 weeks", macd_cross_up),
                                   ("Higher low over the last month", higher_low),
                                   ("Insider buying in the last 90 days", insider_buys_90d > 0),
                                   ("Heavy put buying (fear) near the lows", pc_vol is not None and pc_vol > 1.2)] if ok]
    downtrend = last < s50.iloc[-1] < s200.iloc[-1]
    reasons = []
    if score is not None and score < 35 and downtrend and (low10 or not stabilizing):
        regime = "Falling knife"
        reasons = (["New 52-week low in the last 2 weeks"] if low10 else []) + ["Price below a falling 50- and 200-day average"]
    elif score is not None and score < 45 and len(stabilizing) >= 2:
        regime, reasons = "Basing / turning", stabilizing
    elif score is not None and score >= 60 and stretch >= 75:
        regime = "Overextended"
        reasons = [f"RSI {r_now:.0f}", f"{vs200 * 100:.0f}% above the 200-day ({z:+.1f}σ vs its own 3-year range)"]
        if pc_vol is not None and pc_vol < 0.5:
            reasons.append("Low put/call ratio: options traders are complacent")
    elif score is not None and score >= 60:
        regime, reasons = "Healthy uptrend", ["Above rising 50- and 200-day averages"] if last > s50.iloc[-1] > s200.iloc[-1] else []
    else:
        regime, reasons = "Neutral", stabilizing[:2]
    return {"score": None if score is None else round(float(score), 1), "stretch": round(stretch, 1), "regime": regime, "reasons": reasons,
            "components": {"trend": None if np.isnan(trend) else round(float(trend), 1), "relative_strength": None if np.isnan(rel) else round(float(rel), 1),
                           "buying_pressure": None if np.isnan(flow) else round(float(flow), 1)},
            "inputs": {"r12_1": r12_1, "r3m": r3m, "vs50": vs50, "vs200": vs200, "vs200_z": z, "slope200": slope, "rsi": r_now,
                       "up_volume_share_20d": up_share, "obv_slope_20d": obv_slope, "rel_volume": rel_vol,
                       "put_call_volume": pc_vol, "put_call_oi": pc_oi, "short_pct_float": si, "short_change_1m": si_chg,
                       "eps_rev_90d": eps_rev_90d}}
