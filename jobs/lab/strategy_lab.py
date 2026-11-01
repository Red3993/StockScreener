"""Strategy Lab: backtest every combination of the app's key factors, with walk-forward validation.

WHAT IS TESTED
  Factors (higher = better), all known on the rebalance date (no look-ahead):
    Fundamental  value, profitability, health, growth, dividends, management
                 = the app's 1-10 category scores from the quarter-end score history (built from filings filed by
                   that quarter end). Analyst is excluded: there is no free history of analyst data.
    Timing       trend (Trend score), rally (Rally Setup score), lowvol (calmest 60-day returns),
                 reversal (worst last month), near_low (closest to the 52-week low), oversold (lowest RSI-14)
    Smart money  congress (bought by a member of Congress, disclosed in the last 90 days),
                 insider (open-market insider buying in the last 90 days; only ~1 year of history)
  A strategy = a set of 1+ factors. Each stock's composite = average of its percentile ranks on those factors.
  Every month-end: buy the top 10% of the universe by composite (equal-weighted), hold one month, pay 0.10% per
  side on the turnover. Every subset of the factors is tested (2^14 - 1 = 16,383 strategies) in two universes
  (all ~3,000 stocks, and the S&P 500).

HOW RESULTS ARE JUDGED
  Excess return = portfolio return minus the equal-weighted average stock of the same universe (so the market
  and small-cap swings don't flatter anything); SPY is shown too.
  Walk-forward: strategies are ranked on the TRAIN window (first ~60% of months) and then checked on the TEST
  window they never saw. Picking the best of 16,383 on one window guarantees an impressive-looking winner by luck,
  so what matters is the test result, how stable the strategy is (share of months ahead), and whether its
  neighbours (the same set plus or minus one factor) also work.
  `train_test_corr` = rank correlation between train and test results across all strategies: near 0 means the
  past ranking carries little information.

CAVEATS (shown in the app)
  ~3 years of monthly data; today's index members only (survivorship bias flatters results); Rally Setup and
  Trend use price only unless volume is supplied; insider factor has ~1 year of history.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.lib import rally as RALLY  # noqa: E402

FACTORS = ["value", "profitability", "health", "growth", "dividends", "management",
           "trend", "rally", "lowvol", "reversal", "near_low", "oversold", "congress", "insider"]
LABEL = {"value": "Value", "profitability": "Profitability", "health": "Financial health", "growth": "Growth",
         "dividends": "Dividends", "management": "Management", "trend": "Trend", "rally": "Rally Setup",
         "lowvol": "Low volatility", "reversal": "Last month's losers", "near_low": "Near 52-week low",
         "oversold": "Oversold (RSI)", "congress": "Congress buying", "insider": "Insider buying"}
GROUP = {f: "fundamental" for f in FACTORS[:6]} | {f: "timing" for f in FACTORS[6:12]} | {"congress": "smart", "insider": "smart"}
TOP_FRAC, COST, MIN_PRICE = 0.10, 0.001, 3.0
TRAIN_FRAC = 0.6


def log(msg: str, prog: Path | None = None, **kw) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    if prog:
        prog.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "msg": msg, **kw}))


def load(app_data: Path):
    idx = json.loads((app_data / "index.json").read_text())
    dates = pd.DatetimeIndex(pd.to_datetime(idx["dates"]))
    det = {}
    for f in sorted((app_data / "detail").glob("*.json")):
        det.update(json.loads(f.read_text()))
    return idx, dates, det


def month_ends(dates: pd.DatetimeIndex) -> list[int]:
    s = pd.Series(range(len(dates)), index=dates)
    return [int(v) for v in s.groupby([dates.year, dates.month]).max().values]


def pct_rank(a: np.ndarray) -> np.ndarray:
    out = np.full(a.shape, np.nan)
    ok = np.isfinite(a)
    if ok.sum() > 1:
        out[ok] = pd.Series(a[ok]).rank(pct=True).to_numpy()
    return out


def build_panel(idx, dates, det, volume: pd.DataFrame | None, prog: Path | None):
    tick = sorted(t for t, v in det.items() if len(v.get("closes") or []) >= len(dates))
    C = pd.DataFrame({t: np.array(det[t]["closes"][-len(dates):], dtype=float) for t in tick}, index=dates)
    ix = {r["t"]: r.get("ix") or [] for r in idx["rows"]}
    sp500 = np.array(["IVV" in ix.get(t, []) for t in tick])
    hd = pd.to_datetime(idx["hist_dates"])
    # insider buy events (for Rally Setup and the insider factor)
    ins_ev, ins_dates = {}, {}
    since = None
    for t in tick:
        ev = []
        for x in (det[t].get("insiders") or {}).get("trades", []):
            if x.get("code") != "P" or not x.get("date"):
                continue
            try:
                dd, fd = date.fromisoformat(x["date"][:10]), date.fromisoformat(str(x.get("filed") or x["date"])[:10])
            except ValueError:
                continue
            ev.append({"date": dd, "filed": fd, "owner": x.get("owner"), "ceo": bool(x.get("ceo") or x.get("cfo"))})
            since = fd if since is None else min(since, fd)
        if ev:
            ins_ev[t] = ev
            ins_dates[t] = sorted(e["filed"] for e in ev)
    cong = {t: sorted(date.fromisoformat(x["filed"][:10]) for x in (det[t].get("congress") or [])
                      if "Purchase" in (x.get("type") or "") and x.get("filed")) for t in tick}
    log(f"panel: {len(tick)} stocks, {len(dates)} days, insiders since {since}", prog, stage="panel")

    # Trend + Rally Setup, scored weekly with only data available each week
    V = volume.reindex(index=dates, columns=tick) if volume is not None else None
    rly = RALLY.compute(C, V, {}, ins_ev, since, {}, log=lambda m: log(m, prog, stage="rally"))
    bt = pd.to_datetime(rly["bt_dates"])
    RH = {t: rly["hist"].get(t) or [[], []] for t in tick}

    r = C.pct_change(fill_method=None)
    vol60 = r.rolling(60, min_periods=50).std()
    lo252 = C.rolling(252, min_periods=200).min()
    rsi = RALLY._rsi(C)
    me = [i for i in month_ends(dates) if i >= 252 and dates[i] >= hd[0]]
    reb = [i for i in me if i < len(dates) - 1]
    panel = []
    for i in reb:
        d = dates[i]
        q = int(np.searchsorted(hd.values, d.to_datetime64(), side="right")) - 1
        f = {}
        sh = [(det[t].get("score_history") or [None] * len(hd))[q] if q >= 0 else None for t in tick]
        for k, name in enumerate(FACTORS[:6], start=1):
            f[name] = np.array([np.nan if s is None or s[k] is None else float(s[k]) for s in sh])
        f["dividends"] = np.where(np.isnan(f["dividends"]), 0.0, f["dividends"])     # non-payers rank last on dividends
        b = int(np.searchsorted(bt.values, d.to_datetime64(), side="right")) - 1
        f["rally"] = np.array([np.nan if b < 0 or b >= len(RH[t][0]) or RH[t][0][b] is None else float(RH[t][0][b]) for t in tick])
        f["trend"] = np.array([np.nan if b < 0 or b >= len(RH[t][1]) or RH[t][1][b] is None else float(RH[t][1][b]) for t in tick])
        f["lowvol"] = -vol60.iloc[i].to_numpy()
        f["reversal"] = -(C.iloc[i] / C.iloc[i - 21] - 1).to_numpy()
        f["near_low"] = -(C.iloc[i] / lo252.iloc[i] - 1).to_numpy()
        f["oversold"] = -rsi.iloc[i].to_numpy()
        dd, d90 = d.date(), (d - timedelta(days=90)).date()
        f["congress"] = np.array([float(sum(1 for x in cong[t] if d90 < x <= dd)) for t in tick])
        if since and dd - timedelta(days=90) >= since:
            f["insider"] = np.array([float(sum(1 for x in ins_dates.get(t, []) if d90 < x <= dd)) for t in tick])
        else:
            f["insider"] = np.full(len(tick), np.nan)            # no insider history yet: factor unavailable this month
        px = C.iloc[i].to_numpy()
        j = reb[reb.index(i) + 1] if reb.index(i) + 1 < len(reb) else len(dates) - 1
        fwd = C.iloc[j].to_numpy() / px - 1
        alive = np.isfinite(px) & (px >= MIN_PRICE) & np.isfinite(fwd)
        R = {}
        for k, a in f.items():
            a = np.where(alive, a, np.nan)
            if k in ("congress", "insider"):
                # event factors: buyers rank above non-buyers; ties stay ties
                R[k] = np.where(alive & np.isfinite(a), np.where(a > 0, 0.75 + 0.25 * pct_rank(np.where(a > 0, a, np.nan)), 0.25), np.nan) if np.isfinite(a).any() else np.full(len(a), np.nan)
                R[k] = np.where(np.isnan(R[k]) & alive & np.isfinite(a), 0.25, R[k])
            else:
                R[k] = pct_rank(a)
        spy = idx["bench"]["SPY"]
        panel.append({"i": i, "j": j, "date": d.date().isoformat(), "end": dates[j].date().isoformat(), "alive": alive, "fwd": np.clip(fwd, -0.9, 3.0),
                      "R": np.vstack([R[k] for k in FACTORS]), "spy": (spy[j] / spy[i] - 1) if spy[i] and spy[j] else None})
    log(f"panel: {len(panel)} monthly rebalances {panel[0]['date']} .. {panel[-1]['date']}", prog, stage="panel")
    return tick, sp500, panel, {"insider_since": since.isoformat() if since else None, "volume": V is not None}


def run(panel, universe_mask, combos, prog: Path | None, uname: str):
    """Monthly excess returns (vs the universe's equal-weighted average) for every combo."""
    nC, nT = len(combos), len(panel)
    ex = np.full((nC, nT), np.nan)
    raw = np.full((nC, nT), np.nan)
    turn = np.zeros((nC, nT))
    M = np.zeros((nC, len(FACTORS)), dtype=np.float32)
    for c, s in enumerate(combos):
        M[c, list(s)] = 1
    prev = [None] * nC
    t0 = time.time()
    for k, p in enumerate(panel):
        u = p["alive"] & universe_mask
        if u.sum() < 100:
            continue
        Rk = p["R"][:, u]
        okf = np.isfinite(Rk)
        Rz = np.where(okf, Rk, 0).astype(np.float32)
        tot = M @ Rz                       # sum of ranks per combo per stock
        cnt = M @ okf.astype(np.float32)   # factors available per combo per stock
        need = np.ceil(M.sum(1, keepdims=True) * 0.6)
        comp = np.where(cnt >= need, tot / np.maximum(cnt, 1), -np.inf)
        fwd = p["fwd"][u]
        avg = float(np.mean(fwd))
        n_top = max(10, int(u.sum() * TOP_FRAC))
        ids = np.where(u)[0]
        top = np.argpartition(-comp, n_top, axis=1)[:, :n_top]
        valid = np.take_along_axis(comp, top, 1) > -np.inf
        for c in range(nC):
            sel = top[c][valid[c]]
            if sel.size < 10:
                continue
            r = float(fwd[sel].mean())
            held = set(ids[sel].tolist())
            to = 1.0 if prev[c] is None else 1 - len(held & prev[c]) / len(held)
            prev[c] = held
            raw[c, k] = r - 2 * COST * to
            ex[c, k] = raw[c, k] - avg
            turn[c, k] = to
        log(f"{uname}: month {k + 1}/{nT} ({p['date']}), {nC:,} strategies, {time.time() - t0:.0f}s", prog, stage="run", universe=uname, done=k + 1, total=nT)
    return ex, raw, turn


def stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if x.size < 3:
        return {"n": int(x.size)}
    sd = float(x.std(ddof=1))
    return {"n": int(x.size), "avg": float(x.mean()), "ann": float((1 + x).prod() ** (12 / x.size) - 1) if (1 + x).min() > 0 else None,
            "t": float(x.mean() / (sd / math.sqrt(x.size))) if sd > 0 else None, "hit": float((x > 0).mean()),
            "ir": float(x.mean() / sd * math.sqrt(12)) if sd > 0 else None}


def download_volume(det: dict, dates: pd.DatetimeIndex, prog: Path | None) -> pd.DataFrame | None:
    """Daily volume for every stock (Rally Setup's gap-and-hold and accumulation signals need it)."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    y2t = {(v.get("yahoo") or t): t for t, v in det.items()}
    ys, parts = list(y2t), []
    for k in range(0, len(ys), 200):
        chunk = ys[k:k + 200]
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=dates[0].date().isoformat(), end=(dates[-1] + pd.Timedelta(days=1)).date().isoformat(),
                                 interval="1d", auto_adjust=False, progress=False, threads=True, group_by="column")
                v = df["Volume"] if isinstance(df.columns, pd.MultiIndex) else df[["Volume"]].rename(columns={"Volume": chunk[0]})
                parts.append(v)
                break
            except Exception as e:  # noqa: BLE001
                log(f"volume chunk {k}: {type(e).__name__}: {e}", prog, stage="volume")
                time.sleep(5 * (attempt + 1))
        log(f"volume {min(k + 200, len(ys))}/{len(ys)}", prog, stage="volume")
    if not parts:
        return None
    V = pd.concat(parts, axis=1)
    V.columns = [y2t.get(c, c) for c in V.columns]
    V.index = pd.to_datetime(V.index).tz_localize(None).normalize()
    V = V.loc[:, ~V.columns.duplicated()].reindex(dates)
    log(f"volume: {V.notna().any().sum()} stocks with data", prog, stage="volume")
    return V if V.notna().any().sum() > 1000 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--volume", help="optional parquet/csv.gz of daily volumes (dates x tickers)")
    ap.add_argument("--download-volume", action="store_true", help="download 3y daily volume from Yahoo (runner only)")
    ap.add_argument("--out", default="lab.json")
    ap.add_argument("--progress")
    a = ap.parse_args()
    prog = Path(a.progress) if a.progress else None
    T0 = time.time()
    idx, dates, det = load(Path(a.app_data))
    vol = None
    if a.volume and Path(a.volume).exists():
        vol = pd.read_parquet(a.volume) if a.volume.endswith(".parquet") else pd.read_pickle(a.volume) if a.volume.endswith(".pkl") else pd.read_csv(a.volume, index_col=0, parse_dates=True)
    if vol is None and a.download_volume:
        vol = download_volume(det, dates, prog)
    tick, sp500, panel, meta = build_panel(idx, dates, det, vol, prog)
    combos = [s for k in range(1, len(FACTORS) + 1) for s in itertools.combinations(range(len(FACTORS)), k)]
    nT = len(panel)
    ntrain = int(round(nT * TRAIN_FRAC))
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "factors": FACTORS, "labels": LABEL, "groups": GROUP,
           "months": [p["date"] for p in panel], "ends": [p["end"] for p in panel], "train_months": ntrain,
           "rules": {"top_frac": TOP_FRAC, "cost_per_side": COST, "min_price": MIN_PRICE, "hold": "1 month", "rebalance": "month-end"},
           "meta": {**meta, "n_strategies": len(combos), "stocks": len(tick)}, "universes": {}}
    spy = np.array([p["spy"] if p["spy"] is not None else np.nan for p in panel])
    for uname, mask in (("all", np.ones(len(tick), bool)), ("sp500", sp500)):
        ex, raw, turn = run(panel, mask, combos, prog, uname)
        avg_u = np.array([float(np.mean(p["fwd"][p["alive"] & mask])) for p in panel])
        tr, te = ex[:, :ntrain], ex[:, ntrain:]
        m_tr = np.nanmean(tr, 1)
        m_te = np.nanmean(te, 1)
        sd_tr = np.nanstd(tr, 1, ddof=1)
        ir_tr = m_tr / np.where(sd_tr > 0, sd_tr, np.nan)
        ok = np.isfinite(ir_tr) & np.isfinite(m_te)
        rho = float(pd.Series(ir_tr[ok]).corr(pd.Series(m_te[ok]), method="spearman"))
        full = np.nanmean(ex, 1)
        # neighbours: same factor set plus/minus one factor
        pos = {s: c for c, s in enumerate(combos)}

        def neigh(s):
            ss = set(s)
            outn = []
            for f in range(len(FACTORS)):
                s2 = tuple(sorted(ss ^ {f}))
                if s2 and s2 in pos:
                    outn.append(pos[s2])
            return outn

        def rec(c, curve=False):
            s = combos[c]
            nb = neigh(s)
            d = {"f": [FACTORS[i] for i in s], "train": stats(tr[c]), "test": stats(te[c]), "full": stats(ex[c]),
                 "turnover": round(float(np.nanmean(turn[c, 1:])), 3),
                 "neighbors_test": round(float(np.nanmean(m_te[nb])), 5) if nb else None,
                 "neighbors_beat": round(float(np.mean(m_te[nb] > 0)), 3) if nb else None}
            if curve:
                d["monthly"] = [None if not np.isfinite(v) else round(float(v), 5) for v in raw[c]]
            return d
        order_tr = np.argsort(-np.nan_to_num(ir_tr, nan=-9))
        order_full = np.argsort(-np.nan_to_num(full, nan=-9))
        # robust: positive in train AND test, neighbours mostly positive, ranked by the worse of the two windows
        rob_score = np.minimum(np.nan_to_num(m_tr, nan=-9), np.nan_to_num(m_te, nan=-9))
        order_rob = np.argsort(-rob_score)
        singles = [pos[(i,)] for i in range(len(FACTORS))]
        top_ids = list(dict.fromkeys(list(order_tr[:40]) + list(order_rob[:40]) + list(order_full[:20]) + singles))
        # how the train-best do on test vs everyone (a direct check on luck)
        q = lambda arr: {"p10": float(np.nanpercentile(arr, 10)), "p50": float(np.nanpercentile(arr, 50)), "p90": float(np.nanpercentile(arr, 90))}  # noqa: E731
        # factor importance: average test excess of strategies containing each factor vs not
        imp = {}
        for fi, fname in enumerate(FACTORS):
            has = np.array([fi in s for s in combos])
            imp[fname] = {"with_train": float(np.nanmean(m_tr[has])), "without_train": float(np.nanmean(m_tr[~has])),
                          "with_test": float(np.nanmean(m_te[has])), "without_test": float(np.nanmean(m_te[~has]))}
        by_size = {}
        for k in range(1, len(FACTORS) + 1):
            sel = np.array([len(s) == k for s in combos])
            by_size[str(k)] = {"n": int(sel.sum()), "test_avg": float(np.nanmean(m_te[sel])), "test_pos": float(np.nanmean(m_te[sel] > 0)),
                               "train_avg": float(np.nanmean(m_tr[sel]))}
        out["universes"][uname] = {
            "avg_stock": [round(float(v), 5) for v in avg_u], "spy": [None if not np.isfinite(v) else round(float(v), 5) for v in spy],
            "train_test_corr": round(rho, 3),
            "test_of_train_top50": q(m_te[order_tr[:50]]), "test_of_all": q(m_te[ok]),
            "share_positive": {"train": float(np.nanmean(m_tr > 0)), "test": float(np.nanmean(m_te > 0))},
            "importance": imp, "by_size": by_size,
            "top_train": [int(i) for i in order_tr[:40]], "top_robust": [int(i) for i in order_rob[:40]], "top_full": [int(i) for i in order_full[:20]],
            "singles": singles,
            "strategies": {str(c): rec(c, curve=True) for c in top_ids},
        }
        log(f"{uname}: done; train/test rank corr {rho:.2f}", prog, stage="summarize", universe=uname)
    out["runtime_s"] = round(time.time() - T0)
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (float, np.floating)):
            return None if not math.isfinite(float(o)) else round(float(o), 5)
        if isinstance(o, np.integer):
            return int(o)
        return o
    Path(a.out).write_text(json.dumps(clean(out), separators=(",", ":"), allow_nan=False))
    log(f"wrote {a.out} ({Path(a.out).stat().st_size / 1e6:.2f} MB) in {out['runtime_s']}s", prog, stage="done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
