#!/usr/bin/env python3
"""Full-universe build (~1,650 stocks).

Output (in --out dir):
  index.json         one light row per stock (scores, metric values, derived price signals, momentum, counts)
                     plus shared metadata (dates, hist_dates, labels, directions, macro calendar, health)
  detail/NN.json     full detail per stock, sharded by crc32(ticker) % SHARDS so the app loads one shard on open

Caches (in --cache dir, persisted between runs on the `cache-data` branch):
  yahoo.json.gz      analyst/profile/calendar/options per ticker; top 100 refresh daily, the rest rotate weekly
  insiders.json.gz   Form 4 trades per CIK, incremental by accession number (only new filings fetched)
  news.json.gz       Finnhub company news; top 100 daily, the rest rotate weekly
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
import traceback
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.demo import build as B  # noqa: E402  (shared helpers)
from jobs.app.derive import derive  # noqa: E402
from jobs.lib.form4 import cluster_buys, parse_form4  # noqa: E402
from jobs.lib.metrics import Fundamentals, metric_set_for  # noqa: E402
from jobs.lib.momentum import momentum_block, universe_stats  # noqa: E402
from jobs.lib.politics_match import CommitteeMatcher  # noqa: E402
from jobs.lib.scoring import CATEGORIES, score_rows  # noqa: E402
from jobs.lib.xbrl import CompanyFacts  # noqa: E402

CFG, OVR, TODAY, log, health, clean = B.CFG, B.OVR, B.TODAY, B.log, B.health, B.clean
SHARDS = 96
TOP_N = 100
ROTATE_DAYS = 5          # each non-top stock refreshes Yahoo + news once per ~week
INSIDER_DAYS = 365


def rss_mb() -> int:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    except Exception:  # noqa: BLE001
        return -1


def shard_of(t: str) -> int:
    return zlib.crc32(t.encode()) % SHARDS


def rnd(v, sig=4):
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return float(f"{v:.{sig}g}") if v != 0 else 0.0
    return v


def load_gz(p: Path, default):
    try:
        return json.loads(gzip.decompress(p.read_bytes()))
    except Exception:  # noqa: BLE001
        return default


def save_gz(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(json.dumps(clean(obj), separators=(",", ":")).encode(), 6))


# ------------------------------------------------------------------ yahoo (cached, rotating)
def yahoo_fetch(yt: str) -> dict:
    y = B.yahoo_detail(yt)
    ab, am = B.analyst_block(y, None)
    info = y.get("info") or {}
    return {"fetched": TODAY.isoformat(), "ab": ab, "am": am, "cal": B.calendar_block(y), "prof": B.profile_block(y),
            "opt": y.get("options"), "ymcap": info.get("marketCap"), "yrev": info.get("totalRevenue"),
            "yni": info.get("netIncomeToCommon"), "errors": y.get("_errors", [])}


def needs_refresh(entry: dict | None, t: str, is_top: bool) -> bool:
    if not entry:
        return True
    if is_top:
        return entry.get("fetched") != TODAY.isoformat()
    age = (TODAY - date.fromisoformat(entry.get("fetched", "2000-01-01"))).days
    return age >= ROTATE_DAYS + 2 or (age >= 1 and zlib.crc32(t.encode()) % ROTATE_DAYS == TODAY.toordinal() % ROTATE_DAYS)


# ------------------------------------------------------------------ insiders (incremental)
def insiders_update(sec, cik: int, sub: dict, entry: dict | None, budget: list) -> dict:
    entry = entry or {"seen": {}, "trades": []}
    rec = sub.get("filings", {}).get("recent", {})
    cutoff = (TODAY - timedelta(days=INSIDER_DAYS)).isoformat()
    for form, accn, filed, doc in zip(rec.get("form", []), rec.get("accessionNumber", []), rec.get("filingDate", []),
                                     rec.get("primaryDocument", [])):
        if form != "4" or filed < cutoff or accn in entry["seen"] or budget[0] <= 0:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc.split('/')[-1]}"
        try:
            entry["trades"] += parse_form4(sec.get(url).content, accn, filed)
            entry["seen"][accn] = filed
        except Exception:  # noqa: BLE001
            entry["seen"][accn] = filed   # don't retry a broken filing forever
        budget[0] -= 1
    entry["seen"] = {a: f for a, f in entry["seen"].items() if f >= cutoff}
    entry["trades"] = [t for t in entry["trades"] if (t.get("filed") or "") >= cutoff]
    return entry


def insider_summary(trades: list[dict]) -> dict:
    trades = sorted(trades, key=lambda t: (t.get("date") or "", t.get("filed") or ""), reverse=True)
    buys = [t for t in trades if t["code"] == "P"]
    ds = [t for t in trades if t["code"] == "S" and not t.get("plan_10b5_1")]
    ps = [t for t in trades if t["code"] == "S" and t.get("plan_10b5_1")]
    keep = ("date", "filed", "owner", "roles", "ceo", "cfo", "code", "shares", "price", "value", "plan_10b5_1")
    return {"trades": [{k: t.get(k) for k in keep} for t in trades[:100]], "n_form4": len({t.get("accn") for t in trades}),
            "clusters": cluster_buys(trades),
            "summary": {"buy_value": round(sum(t.get("value") or 0 for t in buys), 2), "buys": len(buys),
                        "ceo_cfo_buys": sum(1 for t in buys if t.get("ceo") or t.get("cfo")),
                        "disc_sale_value": round(sum(t.get("value") or 0 for t in ds), 2), "disc_sales": len(ds),
                        "plan_sale_value": round(sum(t.get("value") or 0 for t in ps), 2), "plan_sales": len(ps)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="app-data")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--zip", default="/tmp/companyfacts.zip")
    ap.add_argument("--politics", default="politics.json")
    ap.add_argument("--hist-quarters", type=int, default=12)
    ap.add_argument("--form4-budget", type=int, default=60000)
    ap.add_argument("--yahoo-threads", type=int, default=4)
    ap.add_argument("--yahoo-budget", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=0, help="debug: only first N tickers")
    args = ap.parse_args()
    out, cdir = ROOT / args.out, ROOT / args.cache
    (out / "detail").mkdir(parents=True, exist_ok=True)
    sec = B.SEC()

    ycache = load_gz(cdir / "yahoo.json.gz", {})
    icache = load_gz(cdir / "insiders.json.gz", {})
    ncache = load_gz(cdir / "news.json.gz", {})
    log(f"caches: yahoo {len(ycache)}, insiders {len(icache)}, news {len(ncache)}")

    uni = B.load_universe()
    B.map_ciks(uni, sec)
    mapped = {t: u for t, u in uni.items() if "cik" in u}
    if args.limit:
        mapped = dict(list(sorted(mapped.items(), key=lambda kv: -kv[1]["ivv_weight"]))[:args.limit])
    top, seen = [], set()
    for t, u in sorted(mapped.items(), key=lambda kv: -kv[1]["ivv_weight"]):
        if u["cik"] in seen or u["ivv_weight"] <= 0:
            continue
        seen.add(u["cik"])
        top.append(t)
        if len(top) >= TOP_N:
            break
    top_set = set(top)
    ytick = {t: u.get("sec_ticker", t) for t, u in mapped.items()}

    # submissions for everyone (SIC, filing dates, Form 4 list, 8-Ks)
    subs: dict[str, dict] = {}
    for i, (t, u) in enumerate(mapped.items()):
        try:
            sub = sec.get(f"https://data.sec.gov/submissions/CIK{u['cik']:010d}.json").json()
            u["sic"] = int(sub.get("sic") or 0) or None
            u["sic_desc"] = sub.get("sicDescription")
            subs[t] = sub
        except Exception as e:  # noqa: BLE001
            u["sic"] = None
            health("submissions", f"{t}: {e}")
        if i % 300 == 0:
            log(f"submissions {i}/{len(mapped)}")

    # prices: 3y for the whole universe, with volume
    uclose, usplit, uvol = B.download_prices(sorted(set(ytick.values())), "3y")
    if len(uclose) and uclose.iloc[-1].isna().mean() > 0.3:
        log(f"dropping incomplete last session {uclose.index[-1].date()} ({uclose.iloc[-1].isna().mean():.0%} missing)")
        uclose, uvol = uclose.iloc[:-1], uvol.iloc[:-1] if len(uvol) else uvol
    log(f"prices: {uclose.shape[1]} tickers x {len(uclose)} days, last {uclose.index[-1].date()}")
    U = universe_stats(uclose)

    # yahoo refresh (threaded, cached)
    todo = [t for t in mapped if needs_refresh(ycache.get(t), t, t in top_set)]
    todo.sort(key=lambda t: (t not in top_set, ycache.get(t, {}).get("fetched", "")))
    todo = todo[:args.yahoo_budget]
    log(f"yahoo: refreshing {len(todo)} tickers ({sum(1 for t in todo if t in top_set)} top)")
    done = 0
    with ThreadPoolExecutor(args.yahoo_threads) as ex:
        futs = {ex.submit(yahoo_fetch, ytick[t]): t for t in todo}
        for f in as_completed(futs):
            t = futs[f]
            try:
                ycache[t] = f.result()
            except Exception as e:  # noqa: BLE001
                health("yahoo", f"{t}: {e}")
            done += 1
            if done % 200 == 0:
                log(f"  yahoo {done}/{len(todo)}")
                save_gz(cdir / "yahoo.json.gz", ycache)

    # insiders (incremental)
    budget = [args.form4_budget]
    for i, t in enumerate(mapped):
        cik = str(mapped[t]["cik"])
        try:
            icache[cik] = insiders_update(sec, int(cik), subs.get(t, {}), icache.get(cik), budget)
        except Exception as e:  # noqa: BLE001
            health("insiders", f"{t}: {e}")
        if i % 200 == 0:
            log(f"insiders {i}/{len(mapped)} (budget left {budget[0]}, SEC calls {sec.calls})")
            save_gz(cdir / "insiders.json.gz", icache)
    save_gz(cdir / "insiders.json.gz", icache)
    if budget[0] <= 0:
        health("insiders", "Form 4 budget exhausted; remaining filings will be fetched on the next run")

    # news (rotating)
    for t in mapped:
        e = ncache.get(t)
        if needs_refresh(e, t, t in top_set):
            ncache[t] = {"fetched": TODAY.isoformat(), "items": B.finnhub_news(t, mapped[t]["name"], keep=10)}
    save_gz(cdir / "news.json.gz", ncache)
    log("news done")

    # politics
    pol = {"trades": [], "members": {}, "stats": {}}
    try:
        pol = json.loads(Path(args.politics).read_text())
    except Exception as e:  # noqa: BLE001
        health("politics", f"no politics.json: {e}")
    pol_by: dict[str, list] = {}
    for tr in pol.get("trades", []):
        if tr.get("ticker") and tr.get("asset_type") in ("ST", "OP", None):
            pol_by.setdefault(tr["ticker"].replace(".", "-").upper(), []).append(tr)
    matcher = CommitteeMatcher(ROOT / "config/committee_sectors.yaml", ROOT / "config/tracked_people.yaml")

    # history dates
    hist_dates, qd = [], date(TODAY.year, ((TODAY.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    for _ in range(args.hist_quarters):
        hist_dates.append(qd)
        qd = date(qd.year, ((qd.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    hist_dates.sort()
    hist_rows: dict[date, list] = {d: [] for d in hist_dates}

    def price_at(yt, d):
        if yt not in uclose.columns:
            return None
        s_ = uclose[yt].loc[:pd.Timestamp(d)].dropna()
        return float(s_.iloc[-1]) if len(s_) and (d - s_.index[-1].date()).days < 10 else None

    # companyfacts
    zp = Path(args.zip)
    if not zp.exists():
        log("downloading companyfacts.zip")
        with sec.get("https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip", stream=True) as r:
            with open(zp, "wb") as fh:
                for chunk in r.iter_content(1 << 22):
                    fh.write(chunk)
    zf = zipfile.ZipFile(zp)
    names = set(zf.namelist())

    def read_cf(cik):
        n = f"CIK{cik:010d}.json"
        return B.loads(zf.read(n)) if n in names else None

    rows, detail, validation = [], {}, []
    preds = OVR.get("predecessor_ciks") or {}
    cut90 = (TODAY - timedelta(days=90)).isoformat()
    cut365 = (TODAY - timedelta(days=365)).isoformat()
    for i, (t, u) in enumerate(mapped.items()):
        try:
            raw = read_cf(u["cik"])
            if raw is None:
                health("xbrl_missing", f"{t}: no companyfacts")
                continue
            cf = CompanyFacts(raw, [p for c in preds.get(t, []) if (p := read_cf(c))])
            f = Fundamentals(cf, CFG["concepts"], TODAY, CFG)
            yt = ytick[t]
            px_series = uclose[yt].dropna() if yt in uclose.columns else pd.Series(dtype=float)
            price = float(px_series.iloc[-1]) if len(px_series) else None
            splits = B.split_list(usplit, yt)
            so = cf.shares_outstanding(TODAY)
            shares_adj = so[0] * B.split_factor(splits, so[1]) if so else None
            mcap, mcap_src = (price * shares_adj, f"XBRL shares ({so[2]}) × price") if price and shares_adj else (None, None)
            yc = ycache.get(t) or {}
            ymcap = yc.get("ymcap")
            if ymcap and (t in (OVR.get("market_cap_from_yahoo") or []) or mcap is None or abs(mcap / ymcap - 1) > 0.25):
                mcap, mcap_src = float(ymcap), "Yahoo"
            ab = yc.get("ab") or None
            extra = dict(yc.get("am") or {})
            tg = (ab or {}).get("targets") or {}
            if tg.get("mean") and price:
                extra["target_upside"] = tg["mean"] / price - 1
            itr = (icache.get(str(u["cik"])) or {}).get("trades", [])
            ins = insider_summary(itr)
            if mcap:
                extra["insider_net_buy_pct"] = (ins["summary"]["buy_value"] - ins["summary"]["disc_sale_value"]) / mcap
            m = f.compute(mcap, extra)
            sector, mset = u["sector"], metric_set_for(u.get("sic"), CFG["sic_routing"])
            row = {"ticker": t, "sector": sector, "set": mset, "metrics": m, "na": dict(f.na), "pays_dividend": f.pays_dividend,
                   "has_analyst": bool(ab and ab.get("n_analysts"))}
            cur = {k: m.get(k) for k in CFG["valuation_metrics_with_own_history"]}
            own, own_na, monthly = B.own_history(cf, subs.get(t, {}), px_series, splits, mcap, shares_adj, cur) if mcap else ({}, {}, {})
            row["own_pct"], row["own_na"] = own, own_na
            rows.append(row)
            usp = splits
            for hd in hist_dates:
                try:
                    ph = price_at(yt, hd)
                    if ph is None:
                        continue
                    fh = Fundamentals(cf, CFG["concepts"], hd, CFG)
                    soh = cf.shares_outstanding(hd)
                    mh = (mcap * ph / price if mcap and price else None) if (mcap_src == "Yahoo" or not soh) else ph * soh[0] * B.split_factor(usp, soh[1])
                    hist_rows[hd].append({"ticker": t, "sector": sector, "set": mset, "metrics": fh.compute(mh if mh and mh > 0 else None),
                                          "na": {}, "pays_dividend": fh.pays_dividend, "has_analyst": False})
                except Exception as e:  # noqa: BLE001
                    health("history", f"{t} @ {hd}: {type(e).__name__}: {e}")
            g = f.inputs
            sc = cf.annual_shares_chain(TODAY)
            rt, ru = CFG["concepts"]["revenue"]["tags"], CFG["concepts"]["revenue"]["unit"]
            qrev, qni = cf.quarters(rt, ru, TODAY), cf.quarters(CFG["concepts"]["net_income"]["tags"], "USD", TODAY)
            qe = sorted(qrev)[-12:]
            prof = yc.get("prof") or {}
            dv = derive(px_series, prof.get("fiftyTwoWeekHigh"), prof.get("fiftyTwoWeekLow")) if len(px_series) else {}
            ins90 = sum(1 for x in itr if x["code"] == "P" and (x.get("date") or "") >= cut90)
            mom = momentum_block(px_series, uvol[yt] if len(uvol) and yt in uvol.columns else None, U, yc.get("opt"), prof,
                                 extra.get("eps_rev_90d"), ins90) if len(px_series) > 210 else {"score": None, "regime": "Not enough history", "reasons": []}
            cong = matcher.annotate(pol_by.get(t.replace(".", "-").upper(), []), pol.get("members", {}), sector, u.get("sic"), t)
            seen_c, cong2 = set(), []
            for x in cong:
                key = (x["name"], x["date"], x["type"], x["asset_type"], x["owner"], x["amount_low"], x.get("desc"), x.get("url"))
                if key in seen_c:
                    continue
                seen_c.add(key)
                if x["relevant"] or (x["date"] or "") >= cut365:
                    cong2.append(x if x["relevant"] else {k: x[k] for k in ("date", "filed", "name", "party", "state", "owner", "type",
                                                                         "asset_type", "amount_low", "amount_high", "committees", "tracked", "relevant")})
            detail[t] = {
                "price": price, "mcap": mcap, "mcap_source": mcap_src, "latest_period": f.latest_period, "shares_source": f.shares_source,
                "analyst": ab, "calendar": yc.get("cal") or {}, "insiders": ins, "tech": B.technicals(px_series),
                "shares_history": [[d.isoformat(), v] for d, v in list(sc.items())[-11:]],
                "quarterly": [{"end": e.isoformat(), "revenue": qrev.get(e), "net_income": qni.get(e)} for e in qe],
                "ttm": {k: g.get(k) for k in ["revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "fcf", "ebitda",
                                               "ffo", "dividends_paid", "debt", "cash_total", "equity", "total_assets"]},
                "news": (ncache.get(t) or {}).get("items", []), "filings": B.filings_block(u["cik"], subs.get(t, {})), "profile": prof,
                "momentum": mom, "congress": cong2, "derived": dv, "yahoo_fetched": yc.get("fetched"),
            }
            if yc.get("yrev") and g.get("revenue"):
                validation.append({"ticker": t, "rev_diff": g["revenue"] / yc["yrev"] - 1, "rev_xbrl": g["revenue"], "rev_yahoo": yc["yrev"],
                                   "ni_diff": (g["net_income"] / yc["yni"] - 1) if g.get("net_income") and yc.get("yni") else None})
        except Exception as e:  # noqa: BLE001
            health("fundamentals", f"{t}: {type(e).__name__}: {e}")
            traceback.print_exc()
        if i % 200 == 0:
            log(f"fundamentals {i}/{len(mapped)}")

    log(f"scoring (rss {rss_mb()} MB)")
    all_t = {r["ticker"] for r in rows}
    scores = score_rows(rows, CFG, score_only=all_t)
    rowmap = {r["ticker"]: r for r in rows}
    log("score history")
    hd_iso = [d.isoformat() for d in hist_dates]
    shist = {t: [] for t in all_t}
    mhist: dict[str, dict] = {t: {} for t in all_t}
    for j, hd in enumerate(hist_dates):
        try:
            hs = score_rows(hist_rows[hd], CFG, score_only=all_t)
        except Exception as e:  # noqa: BLE001
            health("history", f"scoring @ {hd}: {type(e).__name__}: {e}")
            traceback.print_exc()
            hs = {}
        hrow = {r["ticker"]: r for r in hist_rows[hd]}
        for t in all_t:
            if t not in hs:
                shist[t].append(None)
                continue
            cats = [hs[t]["categories"][c].get("score") for c in CATEGORIES if c != "Analyst"]
            av = [v for v in cats if v is not None]
            shist[t].append([round(sum(av) / len(av), 1) if av else None] + cats)
            for k, v in hrow[t]["metrics"].items():
                if isinstance(v, (int, float)):
                    mhist[t].setdefault(k, [None] * len(hist_dates))[j] = rnd(float(v))

    # ------------------------------------------------------------ output
    dates = [d.date().isoformat() for d in uclose.index]
    idx_rows, shards = [], {}
    log(f"output: {len(all_t)} tickers, rss {rss_mb()} MB")
    for t in sorted(all_t):
        if t not in detail or t not in scores:
            continue
        try:
            u, d, sc, row = mapped[t], detail[t], scores[t], rowmap[t]
            yt = ytick[t]
            closes = [None if pd.isna(v) else round(float(v), 2) for v in uclose[yt]] if yt in uclose.columns else []
            dv, mo, ab = d["derived"], d["momentum"] or {}, d["analyst"] or {}
            ins, cg = d["insiders"], d["congress"]
            cats = [sc["categories"][c].get("score") for c in CATEGORIES]
            for c in sc["categories"].values():         # drop repeated labels from detail (the app looks them up)
                for mm in c["metrics"]:
                    mm.pop("label", None)
            own_vals = [mm.get("pct_own") for mm in sc["categories"]["Valuation"]["metrics"] if mm.get("pct_own") is not None]
            cal = d["calendar"] or {}
            nxt = next((e for e in (cal.get("earnings") or []) if e >= TODAY.isoformat()), None)
            idx_rows.append({
                "t": t, "n": u["name"], "sec": u["sector"], "ind": (d["profile"] or {}).get("industry"), "set": row["set"],
                "ix": u["etfs"], "w": u["ivv_weight"], "top": t in top_set, "sh": shard_of(t),
                "mc": rnd(d["mcap"]) if d["mcap"] else None, "ov": sc.get("overall"), "cs": cats, "na": {c: sc["categories"][c].get("na") for c in CATEGORIES if sc["categories"][c].get("na")},
                "m": {k: rnd(v) for k, v in row["metrics"].items() if isinstance(v, (int, float))},
                "own": round(sum(own_vals) / len(own_vals), 1) if own_vals else None,
                "d": dv, "mo": {"s": mo.get("score"), "st": mo.get("stretch"), "rg": mo.get("regime"), "pc": rnd((mo.get("inputs") or {}).get("put_call_volume")),
                                "si": rnd((mo.get("inputs") or {}).get("short_pct_float"))},
                "a": {"n": ab.get("n_analysts"), "rm": ab.get("rating_mean"), "ug": ab.get("upgrades_90d"), "dg": ab.get("downgrades_90d"),
                      "thin": ab.get("thin_coverage"), "tm": (ab.get("targets") or {}).get("mean")},
                "i": {"b30": sum(1 for x in ins["trades"] if x["code"] == "P" and (x["date"] or "") >= (TODAY - timedelta(days=30)).isoformat()),
                      "b90": sum(1 for x in ins["trades"] if x["code"] == "P" and (x["date"] or "") >= cut90),
                      "ceo": any((x["ceo"] or x["cfo"]) and x["code"] == "P" and (x["date"] or "") >= cut90 for x in ins["trades"]),
                      "cl": any(c["end"] >= cut90 for c in ins["clusters"]),
                      "ds90": sum(1 for x in ins["trades"] if x["code"] == "S" and not x["plan_10b5_1"] and (x["date"] or "") >= cut90)},
                "cg": {"b90": sum(1 for x in cg if x["relevant"] and "Purchase" in (x["type"] or "") and (x["date"] or "") >= cut90),
                       "n12": sum(1 for x in cg if x["relevant"] and (x["date"] or "") >= cut365)},
                "e": {"earn": nxt, "exd": cal.get("ex_dividend"), "pay": cal.get("dividend_pay"), "eps": cal.get("eps_estimate")},
                "nw7": sum(1 for n in d["news"] if (n.get("date") or "") >= (TODAY - timedelta(days=7)).isoformat()),
            })
            full = {"ticker": t, "yahoo": yt, "name": u["name"], "sector": u["sector"], "sic": u.get("sic"), "sic_desc": u.get("sic_desc"),
                    "cik": u["cik"], "metric_set": row["set"], "ivv_weight": u["ivv_weight"], "chg_1d": dv.get("c1"),
                    "overall": sc.get("overall"), "excluded": sc.get("excluded"), "categories": sc.get("categories"), "closes": closes,
                    "score_history": shist.get(t), "metric_history": mhist.get(t), **{k: v for k, v in d.items() if k != "derived"}}
            shards.setdefault(shard_of(t), {})[t] = full
        except Exception as e:  # noqa: BLE001
            health("output", f"{t}: {type(e).__name__}: {e}")
            traceback.print_exc()
    log(f"output rows {len(idx_rows)}, rss {rss_mb()} MB")
    for sh, obj in shards.items():
        (out / "detail" / f"{sh:02d}.json").write_text(json.dumps(clean(obj), separators=(",", ":")))
    vv = [v for v in validation if v["rev_diff"] is not None]
    index = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "asof": TODAY.isoformat(),
             "universe_count": len(uni), "fundamentals_rows": len(rows), "count": len(idx_rows), "top_n": len(top), "shards": SHARDS,
             "dates": dates, "hist_dates": hd_iso, "labels": CFG["labels"], "directions": CFG["directions"],
             "macro": B.macro_calendar(), "health": B.HEALTH[:400], "runtime_s": round(time.time() - B.T0), "sec_calls": sec.calls,
             "politics_stats": pol.get("stats", {}), "politics_generated": pol.get("generated"),
             "validation": {"rev_within_2pct": sum(abs(v["rev_diff"]) <= 0.02 for v in vv), "rev_within_5pct": sum(abs(v["rev_diff"]) <= 0.05 for v in vv),
                            "rev_compared": len(vv), "ni_within_5pct": sum(abs(v["ni_diff"]) <= 0.05 for v in validation if v["ni_diff"] is not None),
                            "ni_compared": sum(v["ni_diff"] is not None for v in validation),
                            "worst": sorted(vv, key=lambda v: -abs(v["rev_diff"]))[:12]},
             "cache_stats": {"yahoo": len(ycache), "yahoo_refreshed": len(todo), "insider_ciks": len(icache), "news": len(ncache),
                             "form4_budget_left": budget[0]},
             "rows": idx_rows}
    (out / "index.json").write_text(json.dumps(clean(index), separators=(",", ":")))
    save_gz(cdir / "yahoo.json.gz", ycache)
    sizes = sorted((p.stat().st_size for p in (out / "detail").glob("*.json")), reverse=True)
    log(f"wrote index ({(out / 'index.json').stat().st_size / 1e6:.1f} MB, {len(idx_rows)} rows) + {len(sizes)} shards "
        f"(total {sum(sizes) / 1e6:.1f} MB, largest {sizes[0] / 1e6:.2f} MB); health {len(B.HEALTH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
