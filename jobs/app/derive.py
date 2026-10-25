"""Price-derived fields for every stock (server-side, so the screener can filter all ~1,650 stocks
without shipping 3 years of prices for each one). Mirrors the app's earlier client-side logic."""
from __future__ import annotations

import numpy as np
import pandas as pd

FLAG_WINDOW = 60   # trading days scanned for recent events


def _rsi(c: pd.Series, w: int = 14) -> pd.Series:
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = up.ewm(alpha=1 / w, adjust=False).mean(), dn.ewm(alpha=1 / w, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, np.nan))


def derive(close: pd.Series, hi52: float | None = None, lo52: float | None = None) -> dict:
    c = close.dropna()
    if len(c) < 30:
        return {}
    last = float(c.iloc[-1])

    def back(n):
        return float(c.iloc[-1 - n]) if len(c) > n else None

    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    rsi = _rsi(c)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    win = c.iloc[-252:]
    hi = hi52 if hi52 else float(win.max())
    lo = lo52 if lo52 else float(win.min())
    roll_hi, roll_lo = c.rolling(252, min_periods=200).max(), c.rolling(252, min_periods=200).min()
    ev: dict[str, int] = {}
    n = len(c)

    def cross(a, b, i):
        if i < 1 or pd.isna(a.iloc[i]) or pd.isna(b.iloc[i]) or pd.isna(a.iloc[i - 1]) or pd.isna(b.iloc[i - 1]):
            return 0
        return int(np.sign(a.iloc[i] - b.iloc[i]) - np.sign(a.iloc[i - 1] - b.iloc[i - 1]))

    for bk in range(min(FLAG_WINDOW, n - 1)):
        i = n - 1 - bk
        for k, a, b in (("golden", s50, s200), ("px200", c, s200), ("px50", c, s50), ("macd", macd, sig)):
            x = cross(a, b, i)
            if x > 0:
                ev.setdefault(k + "_up", bk)
            elif x < 0:
                ev.setdefault(k + "_down", bk)
        r = rsi.iloc[i]
        if not pd.isna(r):
            if r < 30:
                ev.setdefault("rsi_lt30", bk)
            if r > 70:
                ev.setdefault("rsi_gt70", bk)
        if not pd.isna(roll_hi.iloc[i]) and c.iloc[i] >= roll_hi.iloc[i]:
            ev.setdefault("new_high", bk)
        if not pd.isna(roll_lo.iloc[i]) and c.iloc[i] <= roll_lo.iloc[i]:
            ev.setdefault("new_low", bk)

    def r(v, nd=4):
        return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), nd)

    s50l, s200l = s50.iloc[-1], s200.iloc[-1]
    return {
        "px": r(last, 2), "c1": r(last / back(1) - 1) if back(1) else None,
        "r5": r(last / back(5) - 1) if back(5) else None, "r21": r(last / back(21) - 1) if back(21) else None,
        "r63": r(last / back(63) - 1) if back(63) else None, "r252": r(last / back(251) - 1) if back(251) else None,
        "hi": r(hi, 2), "lo": r(lo, 2), "fl": r(last / lo - 1) if lo else None, "fh": r(last / hi - 1) if hi else None,
        "pos": r((last - lo) / (hi - lo)) if hi and lo and hi > lo else None,
        "s50": r(s50l, 2) if not pd.isna(s50l) else None, "s200": r(s200l, 2) if not pd.isna(s200l) else None,
        "v50": r(last / s50l - 1) if not pd.isna(s50l) else None, "v200": r(last / s200l - 1) if not pd.isna(s200l) else None,
        "rsi": r(rsi.iloc[-1], 1) if not pd.isna(rsi.iloc[-1]) else None,
        "ab200": None if pd.isna(s200l) else bool(last > s200l), "gold": None if pd.isna(s200l) or pd.isna(s50l) else bool(s50l > s200l),
        "ev": ev,
    }
