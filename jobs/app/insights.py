#!/usr/bin/env python3
"""Insights layer: runs AFTER build_all.py on its output folder and writes `insights.json` next to index.json.
It only reads what the build already produced (index.json + detail shards), so it adds no network calls and no cost,
and the page treats the file as optional (everything it drives hides itself if the file is missing).

    python jobs/app/insights.py --app-data app-data [--universe-log cache/universe_log.json.gz]

What it adds
1. Signal report card (`card`): does each 1-10 score actually predict returns?
   - Category scores + Overall: the 12 quarter-end score snapshots (filed-date, no look-ahead), entry at the first
     close AFTER the quarter end, forward 1 / 3 / 6 month returns.
   - Trend and Rally Setup: the weekly point-in-time history (bt_dates), entry at the next close.
   - Per score bucket: average return minus the equal-weighted universe that day, share beating the median stock.
   - Spread = buckets 8-10 minus 1-3, rank IC (Spearman) per date, t-stats on NON-overlapping periods.
   - Top bucket vs SPY and IWM over the same days. Split by market breadth (share of stocks above their 200-day).
   - Luck bar: with S scores x H horizons tested, the best |t| you'd expect from pure noise is about sqrt(2 ln(S*H)).
2. Evidence weights (`weights`) and an evidence-weighted Overall per stock (`ow`): a deliberately small tilt
   (0.5x-1.5x per category) from the report card, because 10-odd quarterly snapshots is thin evidence.
   The plain average stays the app's Overall score. Analyst has no history, so it keeps weight 1.
3. Quality check per stock (`q`): Piotroski-style F-score (9 tests, year-over-year from metric_history), accruals,
   Altman Z zone, dilution, interest cover, cash burn -> red flags and a "possible value trap" marker.
4. Insider cluster score per stock (`ic`, 0-100): distinct open-market buyers in the best 30-day window of the last
   90 days, size vs market cap, CEO/CFO, first buying in 9+ months, and whether discretionary selling offsets it.
5. Honesty notes (`honesty`): what the backtests cannot see (stocks that left the indexes), how many tests were run.
   With --universe-log it also appends this month's membership so future backtests can be point-in-time.
"""
from __future__ import annotations
import argparse, glob, gzip, json, math, sys, time
from datetime import date, timedelta
from pathlib import Path
import numpy as np

CATS6 = ["Valuation", "Profitability", "Financial Health", "Growth", "Dividends", "Management"]   # order of score_history[1:]
HZ = (21, 63, 126)
FIN_SETS = {"bank", "insurance", "reit"}     # balance-sheet tests that don't apply to financials / REITs


def rnd(x, n=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), n)


# ------------------------------------------------------------------ quality
def quality(row: dict, det: dict) -> dict | None:
    m, mh, ttm = row.get("m") or {}, det.get("metric_history") or {}, det.get("ttm") or {}
    fin = (row.get("set") or det.get("metric_set")) in FIN_SETS

    def prior(k):                      # value 4 quarter-ends before the latest snapshot
        h = mh.get(k) or []
        return h[-5] if len(h) >= 5 else None

    def cmp(k, better_up=True):
        a, b = m.get(k), prior(k)
        if a is None or b is None:
            return None
        return (a > b) if better_up else (a < b)

    ni, ocf, ta, rev = ttm.get("net_income"), ttm.get("ocf"), ttm.get("total_assets"), ttm.get("revenue")
    tests = {
        "roa_pos": None if m.get("roa") is None else m["roa"] > 0,
        "ocf_pos": None if ocf is None else ocf > 0,
        "roa_up": cmp("roa"),
        "cash_gt_ni": None if (ocf is None or ni is None) else ocf > ni,
        "lev_down": None,
        "liq_up": None if fin else cmp("current_ratio"),
        "no_dilution": None if m.get("share_change_1y") is None else m["share_change_1y"] <= 0.005,
        "margin_up": cmp("gross_margin") if m.get("gross_margin") is not None else cmp("operating_margin"),
        "turnover_up": None,
    }
    de, de0 = m.get("debt_to_equity"), prior("debt_to_equity")
    if de is not None and de0 is not None and de >= 0 and de0 >= 0:
        tests["lev_down"] = de <= de0
    r0, nm0 = prior("roa"), prior("net_margin")
    if rev and ta and ta > 0 and r0 is not None and nm0 not in (None, 0) and r0 / nm0 > 0:
        tests["turnover_up"] = (rev / ta) > (r0 / nm0)            # ROA / net margin = revenue / assets a year ago
    used = [v for v in tests.values() if v is not None]
    f = sum(used) if len(used) >= 6 else None
    f9 = None if f is None else round(f / len(used) * 9)

    flags = []                          # [code, severity 2 = red / 1 = amber]
    acc = (ni - ocf) / ta if (not fin and ni is not None and ocf is not None and ta and ta > 0) else None
    if acc is not None and acc > 0.10:
        flags.append(["accruals", 2])
    elif acc is not None and acc > 0.05:
        flags.append(["accruals", 1])
    z = None if fin else m.get("altman_z")
    if z is not None and z < 1.81:
        flags.append(["distress", 2])
    elif z is not None and z < 2.99:
        flags.append(["grey_zone", 1])
    d1 = m.get("share_change_1y")
    if d1 is not None and d1 > 0.15:
        flags.append(["dilution", 2])
    elif d1 is not None and d1 > 0.05:
        flags.append(["dilution", 1])
    ic = m.get("interest_coverage")
    if ic is not None and ic < 1.5:
        flags.append(["interest", 2 if ic < 1 else 1])
    if ni is not None and ocf is not None and ni < 0 and ocf < 0:
        flags.append(["burn", 2])
    elif ni is not None and ocf is not None and ni > 0 and ocf < 0 and not fin:
        flags.append(["paper_profit", 2])
    if f9 is not None and f9 <= 3:
        flags.append(["weak_f", 2])
    red = sum(1 for _, s in flags if s == 2)
    amber = len(flags) - red
    cs = row.get("cs") or []
    cheap = (cs[0] is not None and cs[0] >= 7) if cs else False
    near_low = ((row.get("d") or {}).get("fl") is not None and row["d"]["fl"] <= 0.10)
    trap = bool((cheap or near_low) and (red >= 2 or (f9 is not None and f9 <= 3 and red >= 1)))
    if f9 is None and not flags:
        return None
    grade = 3 if red >= 2 else 2 if red == 1 else 1 if (amber or (f9 is not None and f9 < 7)) else 0   # 0 solid .. 3 red flags
    return {"f": f9, "n": len(used), "t": {k: (None if v is None else int(v)) for k, v in tests.items()},
            "acc": rnd(acc), "fl": flags, "red": red, "g": grade, "trap": trap}


# ------------------------------------------------------------------ insider cluster score
def insider_cluster(row: dict, det: dict, asof: date) -> dict | None:
    tr = (det.get("insiders") or {}).get("trades") or []
    buys = []
    for t in tr:
        if t.get("code") != "P" or t.get("plan_10b5_1") or not t.get("date"):
            continue
        try:
            d = date.fromisoformat(t["date"][:10])
        except ValueError:
            continue
        buys.append((d, t.get("owner") or "?", float(t.get("value") or 0), bool(t.get("ceo") or t.get("cfo"))))
    cut = asof - timedelta(days=90)
    recent = sorted(b for b in buys if b[0] >= cut)
    if not recent:
        return None
    best = None                          # best 30-day window: most distinct buyers, then most dollars
    for i, b in enumerate(recent):
        win = [x for x in recent[i:] if x[0] <= b[0] + timedelta(days=30)]
        key = (len({x[1] for x in win}), sum(x[2] for x in win))
        if best is None or key > best[0]:
            best = (key, win)
    win = best[1]
    n, val = len({x[1] for x in win}), sum(x[2] for x in win)
    exec_buy = any(x[3] for x in win)
    first = min(x[0] for x in win)
    novel = not any(first - timedelta(days=270) <= b[0] < first for b in buys)
    mc = row.get("mc") or 0
    share = val / mc if mc else 0
    s = {1: 15, 2: 40, 3: 60}.get(n, 75)
    s += 15 if share >= 0.001 else 8 if share >= 0.0002 else 0
    s += 5 if val >= 1e6 else 0
    s += 10 if exec_buy else 0
    s += 10 if novel else 0
    sells = sum(float(t.get("value") or 0) for t in tr if t.get("code") == "S" and not t.get("plan_10b5_1") and t.get("date") and t["date"][:10] >= cut.isoformat())
    mixed = sells > val
    if mixed:
        s -= 15
    s = max(0, min(100, s))
    return {"s": s, "n": n, "v": round(val), "last": max(x[0] for x in win).isoformat(), "exec": exec_buy, "novel": novel, "mixed": mixed,
            "tier": 3 if s >= 70 else 2 if s >= 40 else 1}


# ------------------------------------------------------------------ report card
def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 30:
        return None
    ra, rb = a[ok].argsort().argsort().astype(float), b[ok].argsort().argsort().astype(float)
    # scores have many ties: average the ranks within each tied score
    for arr, src in ((ra, a[ok]),):
        for v in np.unique(src):
            msk = src == v
            arr[msk] = arr[msk].mean()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def tstat(x: list[float], overlap: float) -> tuple[float | None, int]:
    """t of the mean, with the sample size shrunk for overlapping periods."""
    v = np.array([y for y in x if y is not None], float)
    if len(v) < 3:
        return None, len(v)
    n_eff = max(2.0, len(v) / max(1.0, overlap))
    sd = v.std(ddof=1)
    if sd == 0:
        return None, len(v)
    return float(v.mean() / (sd / math.sqrt(n_eff))), len(v)


def report_card(ix: dict, C: np.ndarray, tick: list[str], S: dict[str, np.ndarray], when: dict[str, list[int]], step: dict[str, int]) -> dict:
    """C: closes [stocks x days]; S[name]: scores [stocks x dates]; when[name]: entry day index per date; step: days between dates."""
    n_days = C.shape[1]
    bench = {k: np.array([np.nan if v is None else v for v in ix["bench"][k]], float) for k in ("SPY", "IWM") if ix.get("bench", {}).get(k)}
    sma200 = np.full(C.shape, np.nan)
    cs = np.nancumsum(np.where(np.isnan(C), 0, C), axis=1)
    cnt = np.cumsum(~np.isnan(C), axis=1)
    sma200[:, 200:] = (cs[:, 200:] - cs[:, :-200]) / np.maximum(1, cnt[:, 200:] - cnt[:, :-200])
    full = np.zeros(C.shape, bool); full[:, 200:] = (cnt[:, 200:] - cnt[:, :-200]) >= 190
    above = np.where(full, C > sma200, np.nan)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            breadth = np.nanmean(above, axis=0)    # share of stocks above their 200-day, per day
    out, n_tests = {}, 0
    for name, sc in S.items():
        res = {"dates": len(when[name]), "h": {}}
        for h in HZ:
            ov = max(1.0, h / step[name])
            B = {b: [] for b in range(1, 11)}; H = {b: [] for b in range(1, 11)}; N = {b: 0 for b in range(1, 11)}
            spread, ics, top_spy, top_iwm, reg = [], [], [], [], {"strong": [], "weak": []}
            for j, e in enumerate(when[name]):
                if e is None or e + h >= n_days:
                    continue
                fwd = C[:, e + h] / C[:, e] - 1
                s = sc[:, j].astype(float)
                ok = ~(np.isnan(fwd) | np.isnan(s))
                if ok.sum() < 200:
                    continue
                f, s2 = fwd[ok], s[ok]
                if name == "Overall":                # a 1-decimal average: turn into deciles that day
                    rk = s2.argsort().argsort()
                    s2 = np.floor(rk / len(rk) * 10) + 1
                ex, med = f - f.mean(), np.median(f)
                for b in range(1, 11):
                    mk = s2 == b
                    if mk.sum() >= 5:
                        B[b].append(float(ex[mk].mean())); H[b].append(float((f[mk] > med).mean())); N[b] += int(mk.sum())
                top, bot = s2 >= 8, s2 <= 3
                if top.sum() >= 10 and bot.sum() >= 10:
                    sp = float(f[top].mean() - f[bot].mean()); spread.append(sp)
                    br = breadth[e] if e < len(breadth) else np.nan
                    if not np.isnan(br):
                        reg["strong" if br >= 0.5 else "weak"].append(sp)
                    for k, acc in (("SPY", top_spy), ("IWM", top_iwm)):
                        if k in bench and not np.isnan(bench[k][e]) and not np.isnan(bench[k][e + h]):
                            acc.append(float(f[top].mean() - (bench[k][e + h] / bench[k][e] - 1)))
                ic = spearman(s[ok], f)
                if ic is not None:
                    ics.append(ic)
            t_sp, n_sp = tstat(spread, ov)
            t_ic, _ = tstat(ics, ov)
            if n_sp >= 3:
                n_tests += 1
            res["h"][str(h)] = {
                "n": n_sp, "n_eff": rnd(max(2.0, n_sp / ov), 1) if n_sp else 0,
                "buckets": [{"b": b, "ex": rnd(np.mean(B[b])) if B[b] else None, "hit": rnd(np.mean(H[b]), 3) if H[b] else None, "n": N[b]} for b in range(1, 11)],
                "spread": rnd(np.mean(spread)) if spread else None, "t": rnd(t_sp, 2),
                "win": rnd(np.mean([x > 0 for x in spread]), 2) if spread else None,
                "ic": rnd(np.mean(ics)) if ics else None, "t_ic": rnd(t_ic, 2),
                "top_vs_spy": rnd(np.mean(top_spy)) if top_spy else None, "top_vs_iwm": rnd(np.mean(top_iwm)) if top_iwm else None,
                "regime": {k: {"n": len(v), "spread": rnd(np.mean(v)) if v else None} for k, v in reg.items()},
            }
        out[name] = res
    luck = math.sqrt(2 * math.log(max(2, n_tests)))
    for name, res in out.items():           # verdict from the 3- and 6-month spreads (the horizons with real signal-to-noise)
        ts = [res["h"][h]["t"] for h in ("63", "126") if res["h"][h]["t"] is not None and res["h"][h]["n"] >= 4]
        if not ts:
            res["verdict"] = "thin"
        else:
            best, avg = max(ts, key=abs), sum(ts) / len(ts)
            res["verdict"] = ("works" if avg > 0 and abs(best) >= luck else "leans" if avg >= 1.0 else
                              "backwards" if avg <= -1.0 and abs(best) >= luck else "leans_wrong" if avg <= -1.0 else "none")
        res["t_avg"] = rnd(sum(ts) / len(ts), 2) if ts else None
    return {"scores": out, "n_tests": n_tests, "luck_t": rnd(luck, 2), "horizons": list(HZ),
            "breadth_now": rnd(float(breadth[-1]), 3) if not np.isnan(breadth[-1]) else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-data", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--universe-log", default=None)
    a = ap.parse_args()
    t0 = time.time()
    root = Path(a.app_data)
    ix = json.loads((root / "index.json").read_text())
    rows = {r["t"]: r for r in ix["rows"]}
    dates, hist_dates, bt_dates = ix["dates"], ix.get("hist_dates") or [], ix.get("bt_dates") or []
    asof = date.fromisoformat(dates[-1])
    nD = len(dates)
    tick = sorted(rows)
    pos = {t: i for i, t in enumerate(tick)}
    C = np.full((len(tick), nD), np.nan)
    SQ = np.full((7, len(tick), len(hist_dates)), np.nan)        # overall + 6 categories at quarter ends
    SW = np.full((2, len(tick), len(bt_dates)), np.nan)          # rally, trend weekly
    Q, IC, short_hist = {}, {}, 0
    for f in sorted(glob.glob(str(root / "detail" / "*.json"))):
        for t, det in json.loads(Path(f).read_text()).items():
            if t not in rows:
                continue
            i, r = pos[t], rows[t]
            cl = det.get("closes") or []
            if len(cl) == nD:
                C[i] = [np.nan if v is None else v for v in cl]
            if np.isnan(C[i, :260]).any():
                short_hist += 1
            for j, sh in enumerate(det.get("score_history") or []):
                if j < len(hist_dates) and sh:
                    for k in range(min(7, len(sh))):
                        if sh[k] is not None:
                            SQ[k, i, j] = sh[k]
            rh = det.get("rally_hist") or []
            for k in range(min(2, len(rh))):
                for j, v in enumerate(rh[k][:len(bt_dates)]):
                    if v is not None:
                        SW[k, i, j] = v
            q = quality(r, det)
            if q:
                Q[t] = q
            c = insider_cluster(r, det, asof)
            if c:
                IC[t] = c

    def entry_after(d):          # first trading day strictly after d
        for k, x in enumerate(dates):
            if x > d:
                return k
        return None
    wq = [entry_after(d) for d in hist_dates]
    ww = [entry_after(d) for d in bt_dates]
    S = {"Overall": SQ[0]}
    for k, c in enumerate(CATS6):
        S[c] = SQ[k + 1]
    S["Rally Setup"], S["Trend"] = SW[0], SW[1]
    when = {k: (ww if k in ("Rally Setup", "Trend") else wq) for k in S}
    step = {k: (5 if k in ("Rally Setup", "Trend") else 63) for k in S}
    card = report_card(ix, C, tick, S, when, step)

    # evidence weights: small tilt from each category's 3/6-month record; Analyst (no history) stays at 1
    mult = {}
    for c in CATS6:
        z = card["scores"][c].get("t_avg")
        mult[c] = 1.0 if z is None else round(1 + 0.5 * max(-2.0, min(2.0, z)) / 2, 2)
    mult["Analyst"] = 1.0
    order = CATS6 + ["Analyst"]
    OW = {}
    for t, r in rows.items():
        cs = r.get("cs") or []
        num = sum(mult[order[k]] * v for k, v in enumerate(cs[:7]) if v is not None)
        den = sum(mult[order[k]] for k, v in enumerate(cs[:7]) if v is not None)
        if den and sum(v is not None for v in cs[:7]) >= 5:
            OW[t] = round(num / den, 1)

    honesty = {
        "universe": "today's index members",
        "stocks": len(tick), "short_history": short_hist,
        "note": "Backtests only see stocks that are in the indexes today. Companies that went bankrupt, were acquired or fell out are missing, "
                "which flatters buy-the-dip style results the most (near 52-week low, oversold, low price).",
        "n_tests": card["n_tests"], "luck_t": card["luck_t"],
    }
    if a.universe_log:                       # append this month's membership so later backtests can be point-in-time
        p = Path(a.universe_log)
        try:
            log = json.loads(gzip.open(p, "rt").read()) if p.exists() else {}
        except Exception:
            log = {}
        log[dates[-1][:7]] = {"asof": dates[-1], "tickers": {t: (rows[t].get("ix") or []) for t in tick}}
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt") as fh:
            fh.write(json.dumps(log, separators=(",", ":")))
        honesty["universe_log_months"] = len(log)

    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "asof": dates[-1], "card": card,
           "weights": mult, "weights_note": "0.5x-1.5x tilt from each category's 3- and 6-month report-card t-stat; in-sample, thin evidence",
           "honesty": honesty, "q": Q, "ic": IC, "ow": OW, "runtime_s": round(time.time() - t0, 1)}
    dst = Path(a.out) if a.out else root / "insights.json"
    dst.write_text(json.dumps(out, separators=(",", ":")))
    print(f"insights: {len(Q)} quality, {len(IC)} insider clusters, {len(OW)} weighted overall, {card['n_tests']} tests, "
          f"luck bar t={card['luck_t']}, {dst.stat().st_size / 1e6:.2f} MB, {out['runtime_s']}s", file=sys.stderr)


if __name__ == "__main__":
    main()
