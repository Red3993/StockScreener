"""Signal Lab: event studies of traditional trading signals, alone, paired, and filtered.

QUESTION ANSWERED
  "When signal X fires (optionally together with signal Y, and only when filter Z holds), what happened to the stock
   over the next 1 week / 2 weeks / 1 month / 3 months / 6 months, compared with the market?"

SIGNALS (an event on the day it first fires; repeats on the same stock within 20 trading days are ignored)
  Moving averages  golden cross (50-day crosses above 200-day), death cross, price crosses above / below the 200-day,
                   price crosses above the 50-day
  MACD (12/26/9)   bullish signal-line cross, bullish cross below the zero line, bearish cross, MACD crosses above zero
  RSI (14)         falls below 30, recovers back above 30, rises above 70, bullish RSI divergence
  Breakouts        new 52-week high, new 52-week low, Bollinger squeeze breakout (close above the upper band after the
                   bands were at a 6-month narrow), close below the lower band
  Price action     5-day drop of 10%+ (capitulation), 1-day jump of 8%+
  Volume (runner)  volume 3x its 50-day average on an up day, 1-day jump of 5%+ on 2x volume (breakaway gap proxy)
  Smart money      insider open-market buy, insider cluster (3+ insiders within 14 days), CEO/CFO buy,
                   Congress purchase, committee-relevant Congress purchase (all on the public filing date)
  PAIRS            two different signals on the same stock within 10 trading days (event = the day the second fires)
  TRIPLES          three different signals on the same stock within 10 trading days (event = the day the last one fires)

FILTERS (must hold on the event day; all point-in-time)
  price vs 200-day, 50-day vs 200-day (trend), RSI < 40 / > 60, position in the 52-week range, S&P 500 above / below its
  200-day (market regime), recent volume surge (runner), the app's category scores >= 7 or Overall >= 6 / <= 4 from the
  quarter-end score history, large (S&P 500) vs small cap (not in the S&P 1500), insider / Congress buying in the last 90 days.

MEASUREMENT
  Entry at the close of the day AFTER the signal (no look-ahead). Return over 5/10/21/63/126 trading days minus
  (a) the median stock over the same days (a fair coin flip: half of all stocks beat it) and (b) SPY. Single returns are capped at -90% / +300%.
  Significance uses one average per calendar month (signals cluster in time, so stock-level t-stats would overstate it).
  Walk-forward: the first 60% of the signal history is "training", the rest "test"; a combination only counts as robust
  if it works in both. Tested: every combination of up to THREE variables (Tom, 2026-11-01): every signal, signal + 1 filter,
  signal + 2 filters, every pair, pair + 1 filter, every triple of signals (~16,500 combinations x 5 horizons), so some winners
  are luck: the luck check compares training and test ranks.
  A combination's second field `p` is "" (single), one signal (pair) or "b+c" (triple; the first field is the third signal).
  `table` holds a compact row for every combination with enough events, so the app can look up any 1-3 variable test.

CAVEATS
  ~3 years of prices (signals needing a 200-day average start ~10 months in); insider data ~1 year; today's index members
  only (survivorship bias); closes only (no intraday highs/lows); trading costs not deducted from single-event returns.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.lab.strategy_lab import download_volume, load, log  # noqa: E402
from jobs.lib import rally as RALLY  # noqa: E402

H = [5, 10, 21, 63, 126]
H_LBL = {5: "1 week", 10: "2 weeks", 21: "1 month", 63: "3 months", 126: "6 months"}
COOLDOWN, PAIR_WIN, TRAIN_FRAC = 20, 10, 0.6
MIN_N, MIN_SIDE, MIN_MONTHS = 40, 15, 6
LEAD_N, LEAD_T = 80, 1.5                         # leaderboard: at least 80 events and month-clustered t >= 1.5
CAP = {5: 0.15, 10: 0.20, 21: 0.30, 63: 0.50, 126: 0.75}   # per-event excess capped here for ranking, so a few lottery-ticket
                                                            # winners can't carry a signal (the raw average is shown too)

SIG_INFO = {
    "golden": ("Golden cross", "50-day average crosses above the 200-day", "trend"),
    "death": ("Death cross", "50-day average crosses below the 200-day", "trend"),
    "x200up": ("Crosses above 200-day", "Price closes back above its 200-day average", "trend"),
    "x200dn": ("Crosses below 200-day", "Price closes below its 200-day average", "trend"),
    "x50up": ("Crosses above 50-day", "Price closes back above its 50-day average", "trend"),
    "macd_bull": ("MACD bullish cross", "MACD line crosses above its signal line", "momentum"),
    "macd_bull_low": ("MACD bullish cross below zero", "Bullish MACD cross while MACD is still negative (early turn)", "momentum"),
    "macd_bear": ("MACD bearish cross", "MACD line crosses below its signal line", "momentum"),
    "macd_zero": ("MACD crosses above zero", "MACD line turns positive", "momentum"),
    "rsi_lt30": ("RSI falls below 30", "RSI(14) drops into oversold", "rsi"),
    "rsi_exit30": ("RSI recovers above 30", "RSI(14) climbs back out of oversold", "rsi"),
    "rsi_gt70": ("RSI rises above 70", "RSI(14) enters overbought", "rsi"),
    "rsi_div": ("Bullish RSI divergence", "Lower price low with a higher RSI low", "rsi"),
    "hi52": ("New 52-week high", "Close at a 52-week high", "breakout"),
    "lo52": ("New 52-week low", "Close at a 52-week low", "breakout"),
    "bb_break": ("Squeeze breakout", "Close above the upper Bollinger band after a 6-month-narrow squeeze", "breakout"),
    "bb_low": ("Below lower Bollinger band", "Close below the lower Bollinger band (20, 2)", "breakout"),
    "drop10": ("Drops 10%+ in a week", "5-day return of -10% or worse", "price"),
    "jump8": ("Jumps 8%+ in a day", "1-day gain of 8% or more", "price"),
    "vol_spike": ("Volume spike, up day", "Volume 3x its 50-day average on an up day", "volume"),
    "breakaway": ("Breakaway day", "Up 5%+ on twice normal volume", "volume"),
    "ins_buy": ("Insider buy", "Open-market purchase by an officer or director (filing date)", "smart"),
    "ins_cluster": ("Insider cluster buy", "3+ insiders buying within 14 days", "smart"),
    "ins_ceo": ("CEO/CFO buy", "Open-market purchase by the CEO or CFO", "smart"),
    "cong_buy": ("Congress buy", "Purchase disclosed by a member of Congress", "smart"),
    "cong_rel": ("Congress buy, committee-relevant", "Purchase in a sector the member's committee oversees", "smart"),
}
COND_INFO = {
    "above200": "Above its 200-day", "below200": "Below its 200-day", "uptrend": "50-day above 200-day", "downtrend": "50-day below 200-day",
    "rsi_lt40": "RSI under 40", "rsi_gt60": "RSI over 60", "near_low": "Bottom 20% of 52-week range", "near_high": "Top 20% of 52-week range",
    "mkt_up": "S&P 500 above its 200-day", "mkt_down": "S&P 500 below its 200-day", "vol_surge": "Volume running 1.5x normal",
    "value7": "Value score 7+", "profit7": "Profitability score 7+", "health7": "Financial health score 7+", "growth7": "Growth score 7+",
    "mgmt7": "Management score 7+", "overall6": "Overall score 6+", "junk": "Overall score 4 or less",
    "large": "S&P 500 company", "small": "Small cap (outside the S&P 1500)", "ins90": "Insider buying in last 90 days", "cong90": "Congress buying in last 90 days",
}


def cooldown(E: np.ndarray, k: int = COOLDOWN) -> np.ndarray:
    """Keep an event only if the same signal didn't fire on that stock in the previous k days."""
    out = E.copy()
    last = np.full(E.shape[1], -10**9)
    for t in range(E.shape[0]):
        row = E[t]
        if not row.any():
            continue
        idx = np.nonzero(row)[0]
        keep = t - last[idx] > k
        out[t, idx[~keep]] = False
        last[idx[keep]] = t
    return out


def recent(E: np.ndarray, w: int) -> np.ndarray:
    """True where the signal fired within the last w days (including today)."""
    c = np.cumsum(E, axis=0, dtype=np.int32)
    prev = np.vstack([np.zeros((w + 1, E.shape[1]), np.int32), c[:-w - 1]])
    return (c - prev) > 0


def sig_parts(s: str, q: str | None) -> list[str]:
    """All signals in a combination: [s] (single), [s, q] (pair) or [s, b, c] when q is "b+c" (triple)."""
    return [s] + (q.split("+") if q else [])


def combo_events(S: dict, R: dict, s: str, q: str | None) -> np.ndarray:
    """Event mask for one signal, a pair or a triple: every signal fired within PAIR_WIN days; the event is the day the
    last one fires (each signal in turn fires today while the others fired recently), then the 20-day cooldown."""
    parts = sig_parts(s, q)
    if len(parts) == 1:
        return S[s]
    m = np.zeros_like(S[s])
    for i, a in enumerate(parts):
        x = S[a]
        for j, b in enumerate(parts):
            if j != i:
                x = x & R[b]
        m |= x
    return cooldown(m)


def build(app_data: Path, volume: pd.DataFrame | None, prog: Path | None):
    idx, dates, det = load(app_data)
    tick = sorted(t for t, v in det.items() if len(v.get("closes") or []) >= len(dates))
    T, N = len(dates), len(tick)
    C = pd.DataFrame({t: np.array(det[t]["closes"][-T:], dtype=float) for t in tick}, index=dates).ffill(limit=5)
    V = volume.reindex(index=dates, columns=tick) if volume is not None else None
    c = C.to_numpy()
    prev = lambda a: np.vstack([np.full((1, a.shape[1]), np.nan), a[:-1]])   # noqa: E731
    s50, s200 = C.rolling(50).mean().to_numpy(), C.rolling(200).mean().to_numpy()
    ema = lambda span: C.ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()   # noqa: E731
    macd = ema(12) - ema(26)
    sig = pd.DataFrame(macd).ewm(span=9, adjust=False, min_periods=9).mean().to_numpy()
    rsi = RALLY._rsi(C).to_numpy()
    hi252 = C.rolling(252, min_periods=200).max().to_numpy()
    lo252 = C.rolling(252, min_periods=200).min().to_numpy()
    pos = (c - lo252) / np.where(hi252 - lo252 > 0, hi252 - lo252, np.nan)
    m20, sd20 = C.rolling(20).mean().to_numpy(), C.rolling(20).std().to_numpy()
    bw = pd.DataFrame(sd20 / m20)
    bw_p = bw.rolling(126, min_periods=100).rank(pct=True).to_numpy()
    r1 = c / prev(c) - 1
    r5 = c / np.vstack([np.full((5, N), np.nan), c[:-5]]) - 1
    gt = lambda a, b: np.nan_to_num(a, nan=-np.inf) > np.nan_to_num(b, nan=np.inf)   # noqa: E731  (False when either is NaN)
    le = lambda a, b: np.nan_to_num(a, nan=np.inf) <= np.nan_to_num(b, nan=-np.inf)   # noqa: E731
    up = lambda a, b: gt(a, b) & le(prev(a), prev(b))   # noqa: E731
    S = {
        "golden": up(s50, s200), "death": up(s200, s50), "x200up": up(c, s200), "x200dn": up(s200, c), "x50up": up(c, s50),
        "macd_bull": up(macd, sig), "macd_bull_low": up(macd, sig) & (np.nan_to_num(macd) < 0), "macd_bear": up(sig, macd),
        "macd_zero": up(macd, np.zeros_like(macd)),
        "rsi_lt30": up(np.full_like(rsi, 30.0), rsi), "rsi_exit30": gt(rsi, np.full_like(rsi, 30.0)) & le(prev(rsi), np.full_like(rsi, 30.0)),
        "rsi_gt70": up(rsi, np.full_like(rsi, 70.0)),
        "hi52": le(hi252, c) & np.isfinite(hi252), "lo52": le(c, lo252) & np.isfinite(lo252),
        "bb_break": gt(c, m20 + 2 * sd20) & (np.nan_to_num(prev(bw_p), nan=1) < 0.2),
        "bb_low": gt(m20 - 2 * sd20, c),
        "drop10": np.nan_to_num(r5, nan=0) <= -0.10, "jump8": np.nan_to_num(r1, nan=0) >= 0.08,
    }
    F = RALLY.Frames(C, None)
    S["rsi_div"] = (F.rsi_div.to_numpy() > 0) & ~(prev(F.rsi_div.to_numpy()) > 0)
    if V is not None:
        v = V.to_numpy(dtype=float)
        va50 = V.rolling(50, min_periods=40).mean().shift(1).to_numpy()
        S["vol_spike"] = gt(v, 3 * va50) & (np.nan_to_num(r1) > 0)
        S["breakaway"] = (np.nan_to_num(r1) >= 0.05) & gt(v, 2 * va50)
    # smart money (filing dates)
    di = {d.date(): i for i, d in enumerate(dates)}
    dlist = [d.date() for d in dates]

    def day_ix(s: str | None) -> int | None:
        if not s:
            return None
        try:
            d = date.fromisoformat(str(s)[:10])
        except ValueError:
            return None
        k = int(np.searchsorted(np.array(dlist, dtype="datetime64[D]"), np.datetime64(d)))   # first trading day on/after filing
        return k if k < T else None
    for k in ("ins_buy", "ins_cluster", "ins_ceo", "cong_buy", "cong_rel"):
        S[k] = np.zeros((T, N), bool)
    ins_since = None
    for j, t in enumerate(tick):
        buys = [x for x in (det[t].get("insiders") or {}).get("trades", []) if x.get("code") == "P"]
        fl = []
        for x in buys:
            i = day_ix(x.get("filed") or x.get("date"))
            if i is None:
                continue
            S["ins_buy"][i, j] = True
            if x.get("ceo") or x.get("cfo"):
                S["ins_ceo"][i, j] = True
            fl.append((i, x.get("owner")))
            fd = str(x.get("filed") or x.get("date"))[:10]
            ins_since = fd if ins_since is None or fd < ins_since else ins_since
        fl.sort()
        for a, (i, _) in enumerate(fl):
            owners = {o for (i2, o) in fl if i - 10 <= i2 <= i}
            if len(owners) >= 3:
                S["ins_cluster"][i, j] = True
        for x in det[t].get("congress") or []:
            if "Purchase" not in (x.get("type") or ""):
                continue
            i = day_ix(x.get("filed"))
            if i is None:
                continue
            S["cong_buy"][i, j] = True
            if x.get("relevant"):
                S["cong_rel"][i, j] = True
    S = {k: cooldown(v) for k, v in S.items()}
    log(f"signals: {len(S)} built; events " + ", ".join(f"{k} {int(v.sum())}" for k, v in S.items()), prog, stage="signals")

    # filters
    spy = np.array(idx["bench"]["SPY"][-T:], dtype=float)
    spy200 = pd.Series(spy).rolling(200).mean().to_numpy()
    mk_up = np.nan_to_num(spy) > np.nan_to_num(spy200, nan=np.inf)
    mk_dn = np.isfinite(spy200) & ~mk_up
    hd = pd.to_datetime(idx["hist_dates"])
    qi = np.searchsorted(hd.values, dates.values, side="right") - 1          # quarter-end score in force each day
    SH = np.full((len(hd), N, 7), np.nan)
    for j, t in enumerate(tick):
        for q, row in enumerate(det[t].get("score_history") or []):
            if q < len(hd) and row:
                SH[q, j, :len(row)] = [np.nan if x is None else x for x in row]
    sc = lambda k: np.where(qi[:, None] >= 0, SH[np.clip(qi, 0, None), :, k], np.nan)   # noqa: E731  k: 0 overall, 1 value … 6 mgmt
    ix = {r["t"]: r.get("ix") or [] for r in idx["rows"]}
    large = np.array(["IVV" in ix.get(t, []) for t in tick])
    small = np.array([not ({"IVV", "IJH"} & set(ix.get(t, []))) and "IJR" not in ix.get(t, []) for t in tick])
    K = {
        "above200": gt(c, s200), "below200": gt(s200, c), "uptrend": gt(s50, s200), "downtrend": gt(s200, s50),
        "rsi_lt40": le(rsi, np.full_like(rsi, 40.0)) & np.isfinite(rsi), "rsi_gt60": gt(rsi, np.full_like(rsi, 60.0)),
        "near_low": np.nan_to_num(pos, nan=1) < 0.2, "near_high": np.nan_to_num(pos, nan=0) > 0.8,
        "mkt_up": np.repeat(mk_up[:, None], N, 1), "mkt_down": np.repeat(mk_dn[:, None], N, 1),
        "value7": np.nan_to_num(sc(1)) >= 7, "profit7": np.nan_to_num(sc(2)) >= 7, "health7": np.nan_to_num(sc(3)) >= 7,
        "growth7": np.nan_to_num(sc(4)) >= 7, "mgmt7": np.nan_to_num(sc(6)) >= 7, "overall6": np.nan_to_num(sc(0)) >= 6,
        "junk": np.nan_to_num(sc(0), nan=10) <= 4,
        "large": np.repeat(large[None, :], T, 0), "small": np.repeat(small[None, :], T, 0),
        "ins90": recent(S["ins_buy"], 63), "cong90": recent(S["cong_buy"], 63),
    }
    if V is not None:
        K["vol_surge"] = gt(V.rolling(20).mean().to_numpy(), 1.5 * V.rolling(100, min_periods=80).mean().to_numpy())

    # forward excess returns, entry at the next close
    alive = np.isfinite(c) & (np.nan_to_num(c) >= 3)
    FWD, EXA, EXS = {}, {}, {}
    for h in H:
        e = np.vstack([c[1:], np.full((1, N), np.nan)])
        x = np.vstack([c[1 + h:], np.full((1 + h, N), np.nan)])
        f = np.clip(x / e - 1, -0.9, 3.0)
        f[~alive] = np.nan
        avg = np.nanmedian(np.where(np.isfinite(f), f, np.nan), axis=1)   # the median stock: a fair coin-flip benchmark
        sp_e = np.concatenate([spy[1:], [np.nan]])
        sp_x = np.concatenate([spy[1 + h:], np.full(1 + h, np.nan)])
        FWD[h] = f.astype(np.float32)
        EXA[h] = (f - avg[:, None]).astype(np.float32)
        EXS[h] = (f - (sp_x / sp_e - 1)[:, None]).astype(np.float32)
    meta = {"tick": tick, "dates": [d.date().isoformat() for d in dates], "ins_since": ins_since, "volume": V is not None,
            "large": large, "c": c}
    return S, K, FWD, EXA, EXS, meta


def evaluate(mask: np.ndarray, EXA, EXS, FWD, month: np.ndarray, cut: int, nmonths: int):
    ti, si = np.nonzero(mask)
    if ti.size < MIN_N:
        return None
    out = {"n": int(ti.size)}
    for h in H:
        ea = EXA[h][ti, si]
        ok = np.isfinite(ea)
        if ok.sum() < MIN_N:
            out[str(h)] = None
            continue
        t_, s_, ea = ti[ok], si[ok], ea[ok]
        es, fr = EXS[h][t_, s_], FWD[h][t_, s_]
        ec = np.clip(ea, -CAP[h], CAP[h])
        m = month[t_]
        cnt = np.bincount(m, minlength=nmonths)
        mm = np.bincount(m, weights=ec, minlength=nmonths)[cnt > 0] / cnt[cnt > 0]
        tr, te = t_ < cut, t_ >= cut
        tst = float(mm.mean() / (mm.std(ddof=1) / math.sqrt(mm.size))) if mm.size >= MIN_MONTHS and mm.std(ddof=1) > 0 else None
        out[str(h)] = {"n": int(ea.size), "avg": float(ea.mean()), "avgc": float(ec.mean()), "med": float(np.median(ea)), "hit": float((ea > 0).mean()),
                       "spy": float(np.nanmean(es)), "spy_hit": float(np.nanmean(es > 0)), "raw": float(fr.mean()), "t": tst, "months": int(mm.size),
                       "tr_n": int(tr.sum()), "tr": float(ec[tr].mean()) if tr.sum() >= MIN_SIDE else None,
                       "te_n": int(te.sum()), "te": float(ec[te].mean()) if te.sum() >= MIN_SIDE else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--download-volume", action="store_true")
    ap.add_argument("--volume", help="pickled DataFrame of daily volumes (dates x tickers), e.g. from --save-volume")
    ap.add_argument("--save-volume", help="save the downloaded volumes here (pickle) for the other lab script")
    ap.add_argument("--out", default="signals.json")
    ap.add_argument("--progress")
    a = ap.parse_args()
    prog = Path(a.progress) if a.progress else None
    T0 = time.time()
    vol = pd.read_pickle(a.volume) if a.volume and Path(a.volume).exists() else None
    if vol is None and a.download_volume:
        _, dates0, det0 = load(Path(a.app_data))
        vol = download_volume(det0, dates0, prog)
        del det0
        if vol is not None and a.save_volume:
            vol.to_pickle(a.save_volume)
    S, K, FWD, EXA, EXS, meta = build(Path(a.app_data), vol, prog)
    dates = meta["dates"]
    T = len(dates)
    month = np.array([int(d[:4]) * 12 + int(d[5:7]) for d in dates])
    month -= month.min()
    nmonths = int(month.max()) + 1
    # training/test cut: 60% of the days on which events can be measured (after the 200-day warm-up)
    warm = 200
    cut = warm + int((T - warm) * TRAIN_FRAC)
    sig_names, cond_names = list(S), list(K)
    combos = []
    for s in sig_names:
        combos.append((s, None, ()))
        for k in cond_names:
            combos.append((s, None, (k,)))
        for k1, k2 in itertools.combinations(cond_names, 2):
            if {k1, k2} in ({"above200", "below200"}, {"uptrend", "downtrend"}, {"mkt_up", "mkt_down"}, {"large", "small"}, {"rsi_lt40", "rsi_gt60"}, {"near_low", "near_high"}, {"overall6", "junk"}):
                continue
            combos.append((s, None, (k1, k2)))
    pairs = list(itertools.combinations(sig_names, 2))
    R = {s: recent(S[s], PAIR_WIN) for s in sig_names}
    for p, q in pairs:
        combos.append((p, q, ()))
        for k in cond_names:
            combos.append((p, q, (k,)))
    NESTED = [{"ins_buy", "ins_cluster"}, {"ins_buy", "ins_ceo"}, {"cong_buy", "cong_rel"}]   # the second always fires with the first
    triples = [t for t in itertools.combinations(sig_names, 3) if not any(n <= set(t) for n in NESTED)]
    for a_, b_, c_ in triples:
        combos.append((a_, b_ + "+" + c_, ()))
    log(f"combos: {len(combos):,} ({len(sig_names)} signals, {len(cond_names)} filters, {len(pairs)} pairs, {len(triples)} triples)", prog, stage="run", done=0, total=len(combos))
    res = []
    pair_cache: dict = {}
    for n_, (s, q, ks) in enumerate(combos):
        if (s, q) not in pair_cache:
            pair_cache.clear()
            pair_cache[(s, q)] = combo_events(S, R, s, q)
        m = pair_cache[(s, q)]
        for k in ks:
            m = m & K[k]
        r = evaluate(m, EXA, EXS, FWD, month, cut, nmonths)
        if r:
            res.append({"s": s, "p": q, "k": list(ks), **r})
        if n_ % 500 == 0:
            log(f"tested {n_:,}/{len(combos):,} combinations ({len(res):,} with enough events), {time.time() - T0:.0f}s", prog, stage="run", done=n_, total=len(combos))
    log(f"tested {len(combos):,}; {len(res):,} had {MIN_N}+ events", prog, stage="summarize")

    # ---- summaries
    def g(r, h, k):
        x = r.get(str(h))
        return None if not x else x.get(k)

    def robust(r, h):
        tr, te = g(r, h, "tr"), g(r, h, "te")
        return None if tr is None or te is None else min(tr, te)
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "horizons": H, "h_labels": H_LBL,
           "signals": {k: {"name": SIG_INFO[k][0], "desc": SIG_INFO[k][1], "group": SIG_INFO[k][2], "events": int(S[k].sum())} for k in sig_names},
           "filters": {k: COND_INFO[k] for k in cond_names},
           "meta": {"dates": [dates[warm], dates[-1]], "cut": dates[cut], "tested": len(combos), "kept": len(res), "volume": meta["volume"],
                    "by_size": {str(n): sum(1 for (s_, q_, k_) in combos if len(sig_parts(s_, q_)) + len(k_) == n) for n in (1, 2, 3)},
                    "triples": len(triples),
                    "insider_since": meta["ins_since"], "stocks": len(meta["tick"]), "cooldown": COOLDOWN, "pair_window": PAIR_WIN, "min_n": MIN_N},
           "baseline": {}, "luck": {}, "leaders": {}, "singles": [], "matrix": {}, "paths": {}, "live": {}}
    for h in H:
        ea = EXA[h][warm:]
        ea = ea[np.isfinite(ea)]
        out["baseline"][str(h)] = {"hit": float((ea > 0).mean()), "med": float(np.median(ea))}
        both = [(r[str(h)]["tr"], r[str(h)]["te"]) for r in res if r.get(str(h)) and r[str(h)]["tr"] is not None and r[str(h)]["te"] is not None]
        if len(both) > 20:
            a_, b_ = np.array(both).T
            out["luck"][str(h)] = {"corr": float(pd.Series(a_).corr(pd.Series(b_), method="spearman")), "n": len(both),
                                   "test_of_top50": float(np.median(b_[np.argsort(-a_)[:50]])), "test_all": float(np.median(b_)),
                                   "share_pos_both": float(np.mean((a_ > 0) & (b_ > 0)))}
    key = lambda r: (r["s"], r["p"], tuple(r["k"]))   # noqa: E731
    keep = {}
    # drop combinations whose filter changes nothing (e.g. "insider cluster" + "insider buying in last 90 days"):
    # same events as the combination without that filter
    byk = {key(r): r for r in res}
    redundant = set()
    for r in res:
        for f in r["k"]:
            par = byk.get((r["s"], r["p"], tuple(x for x in r["k"] if x != f)))
            if par and par["n"] == r["n"]:
                redundant.add(key(r))
        sp = sig_parts(r["s"], r["p"])
        if len(sp) == 3:
            for drop in sp:
                rest = [x for x in sp if x != drop]
                par = byk.get((rest[0], rest[1], ())) or byk.get((rest[1], rest[0], ()))
                if par and par["n"] == r["n"]:
                    redundant.add(key(r))
    res_all, res = res, [r for r in res if key(r) not in redundant]
    for h in H:
        bh = out["baseline"][str(h)]["hit"]
        cand = [r for r in res if robust(r, h) is not None and (g(r, h, "t") or 0) >= LEAD_T and g(r, h, "n") >= LEAD_N and g(r, h, "hit") > bh]
        best = sorted(cand, key=lambda r: -robust(r, h))[:60]
        worst = sorted([r for r in res if robust(r, h) is not None and g(r, h, "n") >= LEAD_N], key=lambda r: max(g(r, h, "tr"), g(r, h, "te")))[:20]
        tech = [r for r in cand if all(SIG_INFO[x][2] != "smart" for x in sig_parts(r["s"], r["p"]))
                and not any(k in ("value7", "profit7", "health7", "growth7", "mgmt7", "overall6", "junk", "ins90", "cong90") for k in r["k"])]
        best_tech = sorted(tech, key=lambda r: -robust(r, h))[:30]
        out["leaders"][str(h)] = {"best": [list(key(r)) for r in best], "worst": [list(key(r)) for r in worst], "tech": [list(key(r)) for r in best_tech]}
        for r in best + worst + best_tech:
            keep[key(r)] = r
    out["singles"] = [list(key(r)) for r in res if r["p"] is None and not r["k"]]
    out["meta"]["redundant_dropped"] = len(redundant)
    for r in res_all:
        if r["p"] is None and len(r["k"]) <= 1:
            keep[key(r)] = r
            if len(r["k"]) == 1:
                out["matrix"].setdefault(r["s"], {})[r["k"][0]] = {str(h): (None if not r.get(str(h)) else [r[str(h)]["avgc"], r[str(h)]["n"], r[str(h)]["t"], r[str(h)]["hit"], r[str(h)]["tr"], r[str(h)]["te"]]) for h in H}
    # event-time paths (average excess vs the average stock, days 0..126) and live firings for the kept combos
    LIVE_DAYS = 10
    cum = {}
    c = meta["c"]
    d1 = c[1:] / c[:-1] - 1
    d1[~np.isfinite(d1) | (np.abs(d1) > 0.9)] = np.nan
    steps = [0, 1, 2, 3, 5, 10, 15, 21, 30, 42, 63, 84, 105, 126]
    alive_c = np.where(np.nan_to_num(c) >= 3, c, np.nan)
    MED = np.full((T, len(steps)), np.nan)        # median stock's return from day e0 to e0+d (the path benchmark)
    for j, d in enumerate(steps):
        if d < T:
            MED[: T - d, j] = np.nanmedian(np.clip(c[d:] / alive_c[: T - d] - 1, -0.9, 3), axis=1)
    want = {tuple(x[:2]) + (tuple(x[2]),) for h in ("21", "63") for x in out["leaders"][h]["best"][:25] + out["leaders"][h]["tech"][:15]}
    RW = {k: recent(S[k], PAIR_WIN) for k in S}
    for kk, r in keep.items():
        s, q, ks = kk
        m = combo_events(S, RW, s, q)
        for k in ks:
            m = m & K[k]
        ti, si = np.nonzero(m)
        if kk in want or (q is None and not ks):
            pts = []
            for j, d in enumerate(steps):
                ok = ti + 1 + d < T
                if ok.sum() < MIN_N:
                    break
                e0, e1 = ti[ok] + 1, ti[ok] + 1 + d
                rr = c[e1, si[ok]] / c[e0, si[ok]] - 1
                ex = np.clip(rr - MED[e0, j], -0.5, 0.5)
                pts.append([d, round(float(np.nanmean(ex)), 4)])
            cum["|".join([s, q or "", ",".join(ks)])] = pts
        recent_ev = [(int(t), meta["tick"][int(j)]) for t, j in zip(ti, si) if t >= T - LIVE_DAYS]
        if recent_ev:
            out["live"]["|".join([s, q or "", ",".join(ks)])] = [[meta["dates"][t], tk] for t, tk in sorted(recent_ev, reverse=True)[:40]]
    out["paths"] = cum
    # compact lookup of every tested combination with enough events (redundant ones included, flagged):
    # key -> [n, then per horizon in H: [avgc, hit, n, t, tr, te] or None, redundant flag]
    rnd = lambda v, d=4: None if v is None or not math.isfinite(float(v)) else round(float(v), d)   # noqa: E731
    out["table"] = {"|".join([r["s"], r["p"] or "", ",".join(r["k"])]): [r["n"]] + [
        None if not r.get(str(h)) else [rnd(r[str(h)]["avgc"]), rnd(r[str(h)]["hit"], 3), r[str(h)]["n"], rnd(r[str(h)]["t"], 2),
                                        rnd(r[str(h)]["tr"]), rnd(r[str(h)]["te"])] for h in H] + [1 if key(r) in redundant else 0]
        for r in res_all}
    out["results"] = {"|".join([k[0], k[1] or "", ",".join(k[2])]): {kk: vv for kk, vv in r.items() if kk not in ("s", "p", "k")} for k, r in keep.items()}
    out["runtime_s"] = round(time.time() - T0)

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (float, np.floating)):
            return None if not math.isfinite(float(o)) else round(float(o), 5)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return o
    Path(a.out).write_text(json.dumps(clean(out), separators=(",", ":"), allow_nan=False))
    log(f"wrote {a.out} ({Path(a.out).stat().st_size / 1e6:.2f} MB) in {out['runtime_s']}s", prog, stage="done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
