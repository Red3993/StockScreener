#!/usr/bin/env python3
"""Research tab data: 13F holdings of notable funds and of companies' own portfolios -> research.json.

For each filer in config/funds.yaml:
  - resolve the CIK (hint checked against the SEC entity name, else EDGAR full-text search for 13F-HR filings),
  - read up to 13 quarterly 13F-HR filings (original filings; amendments skipped) and their information tables,
  - aggregate long stock positions by CUSIP (options kept apart), map CUSIPs to tickers (app-universe names first,
    then the free OpenFIGI API, cached across runs),
  - latest quarter: top holdings with weight and change vs the prior quarter (new / added / trimmed / sold out),
  - "copycat" track record: buy the reported portfolio (top positions, value-weighted) on each filing date, hold until
    the next filing, chain the quarters; compared with SPY over the same days. This is what a follower could have
    done, since 13Fs are filed up to 45 days after the quarter ends.
Plus consensus across the `fund` group: most owned, most bought and most sold in the latest quarter.

13F limits: long US-listed equities and options only, no shorts, no cash, no non-US holdings, 45-day lag.

    SEC_USER_AGENT="Name email@x.com" python jobs/research/build_research.py --app-data app-data --cache rcache --out research.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.demo import build as B  # noqa: E402

QUARTERS = 13
TOP_N = 60          # positions kept per quarter (and used for the copycat portfolio)
LOG: list[str] = []


def log(m: str) -> None:
    print(m, flush=True)
    LOG.append(m)


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    stop = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "the", "class", "cl", "com", "new", "holdings", "holding",
            "group", "sa", "nv", "ag", "lp", "llc", "shs", "a", "b", "c", "ord", "common", "stock", "del", "de", "sponsored", "adr", "ads"}
    return " ".join(w for w in s.split() if w not in stop)


# ------------------------------------------------------------------ SEC
def resolve(sec, f: dict) -> tuple[int | None, str | None, dict | None]:
    toks = [t.lower() for t in f.get("match", [])]

    def ok(name):
        n = (name or "").lower()
        return all(t in n for t in toks)

    def subs(cik):
        try:
            return sec.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json").json()
        except Exception as e:  # noqa: BLE001
            log(f"  submissions {cik}: {e}")
            return None
    if f.get("cik"):
        s = subs(f["cik"])
        if s and ok(s.get("name")) and "13F-HR" in (s.get("filings", {}).get("recent", {}).get("form") or []):
            return int(f["cik"]), s.get("name"), s
        log(f"  {f['name']}: CIK hint {f['cik']} -> {s.get('name') if s else None!r} rejected; searching")
    # EDGAR full-text search for 13F-HR filings mentioning the name
    try:
        q = " ".join(f.get("match") or [f["name"]])
        r = sec.get("https://efts.sec.gov/LATEST/search-index?q=" + requests.utils.quote(f'"{q}"') + "&forms=13F-HR")
        hits = r.json().get("hits", {}).get("hits", [])
        cnt = Counter()
        for h in hits:
            src = h.get("_source", {})
            for cik, dn in zip(src.get("ciks", []), src.get("display_names", [])):
                if ok(dn):
                    cnt[int(cik)] += 1
        for cik, _ in cnt.most_common(3):
            s = subs(cik)
            if s and ok(s.get("name")) and "13F-HR" in (s.get("filings", {}).get("recent", {}).get("form") or []):
                log(f"  {f['name']}: resolved to CIK {cik} ({s.get('name')})")
                return cik, s.get("name"), s
    except Exception as e:  # noqa: BLE001
        log(f"  search {f['name']}: {e}")
    return None, None, None


def filings_13f(s: dict) -> list[dict]:
    r = s.get("filings", {}).get("recent", {})
    rows = []
    for form, acc, filed, rep, doc in zip(r.get("form", []), r.get("accessionNumber", []), r.get("filingDate", []),
                                          r.get("reportDate", []), r.get("primaryDocument", [])):
        if form == "13F-HR" and rep:
            rows.append({"acc": acc, "filed": filed, "period": rep})
    best = {}
    for x in sorted(rows, key=lambda x: x["filed"]):     # latest original filing per period
        best[x["period"]] = x
    return sorted(best.values(), key=lambda x: x["period"], reverse=True)[:QUARTERS]


def info_table(sec, cik: int, acc: str) -> list[dict]:
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    items = sec.get(base + "/index.json").json().get("directory", {}).get("item", [])
    xmls = [it for it in items if it.get("name", "").lower().endswith(".xml") and it["name"].lower() != "primary_doc.xml"]
    if not xmls:
        return []
    xmls.sort(key=lambda it: -int(it.get("size") or 0))
    root = ET.fromstring(sec.get(f"{base}/{xmls[0]['name']}").content)
    out = []
    for el in root.iter():
        if not el.tag.lower().endswith("infotable"):
            continue
        d = {}
        for ch in el.iter():
            tag = ch.tag.split("}")[-1]
            if ch.text and ch.text.strip():
                d[tag] = ch.text.strip()
        try:
            out.append({"name": d.get("nameOfIssuer", ""), "cls": d.get("titleOfClass", ""), "cusip": (d.get("cusip") or "").upper()[:9],
                        "value": float(d.get("value", 0) or 0), "shares": float(d.get("sshPrnamt", 0) or 0),
                        "kind": d.get("sshPrnamtType", "SH"), "pc": (d.get("putCall") or "").title() or None})
        except ValueError:
            continue
    return out


def aggregate(rows: list[dict], period: str) -> dict:
    # values are in dollars for filings since 2023; earlier ones were in thousands
    mult = 1000.0 if period < "2022-12-31" else 1.0
    eq, opt = defaultdict(lambda: {"value": 0.0, "shares": 0.0}), defaultdict(float)
    names = {}
    for r in rows:
        if not r["cusip"]:
            continue
        if r["pc"]:
            opt[(r["cusip"], r["pc"])] += r["value"] * mult
            names[r["cusip"]] = r["name"]
            continue
        if r["kind"] != "SH":
            continue
        p = eq[r["cusip"]]
        p["value"] += r["value"] * mult
        p["shares"] += r["shares"]
        names[r["cusip"]] = r["name"]
    total = sum(p["value"] for p in eq.values())
    keep = dict(sorted(eq.items(), key=lambda kv: -kv[1]["value"])[:400])   # quant funds hold thousands; the tail doesn't matter here
    names = {c: names[c] for c in list(keep) + [c for c, _ in opt]}
    return {"total": total, "n": len(eq), "eq": keep, "opt": {f"{c}|{pc}": v for (c, pc), v in opt.items()}, "names": names}


# ------------------------------------------------------------------ CUSIP -> ticker
def map_cusips(cusips: dict[str, str], universe: dict[str, str], cache: dict) -> dict[str, str | None]:
    """cusips: cusip -> issuer name. universe: normalized company name -> ticker."""
    out = {}
    todo = []
    for c, nm in cusips.items():
        if c in cache:
            out[c] = cache[c]
            continue
        t = universe.get(norm_name(nm))
        if t:
            out[c] = cache[c] = t
        else:
            todo.append(c)
    log(f"cusips: {len(cusips)} total, {len(cusips) - len(todo)} known, {len(todo)} to look up on OpenFIGI")
    s = requests.Session()
    for i in range(0, len(todo), 10):
        part = todo[i:i + 10]
        body = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in part]
        for attempt in range(4):
            try:
                r = s.post("https://api.openfigi.com/v3/mapping", json=body, timeout=30)
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                res = r.json()
                break
            except Exception as e:  # noqa: BLE001
                log(f"openfigi: {e}")
                time.sleep(10)
        else:
            res = []
        for c, x in zip(part, res if isinstance(res, list) else []):
            data = (x or {}).get("data") or []
            eqs = [d for d in data if (d.get("marketSector") == "Equity")] or data
            tk = (eqs[0].get("ticker") if eqs else None)
            out[c] = cache[c] = tk.replace("/", "-").replace(" ", "-") if tk else None
        time.sleep(2.6)    # 25 requests / minute without an API key
    return out


# ------------------------------------------------------------------ prices + copycat
def download(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf
    frames = []
    for i in range(0, len(tickers), 150):
        part = tickers[i:i + 150]
        for attempt in range(3):
            try:
                df = yf.download(part, period="4y", auto_adjust=True, progress=False, threads=True, group_by="column")
                break
            except Exception as e:  # noqa: BLE001
                log(f"prices retry {attempt}: {e}")
                time.sleep(15 * (attempt + 1))
        else:
            continue
        if df is not None and not df.empty:
            frames.append(df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": part[0]}))
        time.sleep(1.5)
    if not frames:
        return pd.DataFrame()
    c = pd.concat(frames, axis=1)
    return c.loc[:, ~c.columns.duplicated()].sort_index().ffill(limit=5)


def px_on(P: pd.DataFrame, t: str, d: str, after: bool = True) -> float | None:
    if t not in P.columns:
        return None
    s = P[t]
    ts = pd.Timestamp(d)
    k = s.index.searchsorted(ts, side="right" if after else "left")
    if k >= len(s):
        k = len(s) - 1
    v = s.iloc[k]
    return None if pd.isna(v) or v <= 0 else float(v)


def copycat(quarters: list[dict], P: pd.DataFrame, today: str) -> dict | None:
    """quarters: oldest first, each {filed, top: [{t, value}]}. Buy at first close after filed, hold to next filed date."""
    if len(quarters) < 2 or "SPY" not in P.columns:
        return None
    cum, spy, curve, rets, srets, cover = 1.0, 1.0, [], [], [], []
    for i, q in enumerate(quarters):
        start, end = q["filed"], (quarters[i + 1]["filed"] if i + 1 < len(quarters) else today)
        if start >= end:
            continue
        legs, wsum, wall = [], 0.0, sum(x["value"] for x in q["top"]) or 1
        for x in q["top"]:
            if not x.get("t"):
                continue
            p0, p1 = px_on(P, x["t"], start), px_on(P, x["t"], end)
            if p0 and p1:
                legs.append((x["value"], p1 / p0 - 1))
                wsum += x["value"]
        if wsum <= 0:
            continue
        r = sum(w * g for w, g in legs) / wsum
        s0, s1 = px_on(P, "SPY", start), px_on(P, "SPY", end)
        sr = (s1 / s0 - 1) if s0 and s1 else 0.0
        cum *= 1 + r
        spy *= 1 + sr
        rets.append(r)
        srets.append(sr)
        cover.append(wsum / wall)
        curve.append([end, round(cum - 1, 4), round(spy - 1, 4)])
    if not rets:
        return None
    d0, d1 = quarters[0]["filed"], today
    yrs = max(0.25, (pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25)
    peak, dd = 1.0, 0.0
    for _, c, _ in curve:
        peak = max(peak, 1 + c)
        dd = min(dd, (1 + c) / peak - 1)
    return {"start": d0, "periods": len(rets), "cum": round(cum - 1, 4), "spy_cum": round(spy - 1, 4),
            "ann": round(cum ** (1 / yrs) - 1, 4), "spy_ann": round(spy ** (1 / yrs) - 1, 4),
            "beat": sum(1 for a, b in zip(rets, srets) if a > b), "maxdd": round(dd, 4),
            "coverage": round(sum(cover) / len(cover), 3), "curve": curve}


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-data", default="app-data")
    ap.add_argument("--cache", default="rcache")
    ap.add_argument("--out", default="research.json")
    ap.add_argument("--only", default="", help="debug: comma-separated fund names")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/funds.yaml").read_text())["funds"]
    if a.only:
        keep = {x.strip().lower() for x in a.only.split(",")}
        cfg = [f for f in cfg if f["name"].lower() in keep]
    cdir = Path(a.cache)
    cdir.mkdir(parents=True, exist_ok=True)
    cache = json.loads((cdir / "cusip_map.json").read_text()) if (cdir / "cusip_map.json").exists() else {}
    fcache = json.loads((cdir / "filings.json").read_text()) if (cdir / "filings.json").exists() else {}   # acc -> aggregated
    universe = {}
    try:
        idx = json.loads((Path(a.app_data) / "index.json").read_text())
        for r in idx["rows"]:
            universe.setdefault(norm_name(r["n"]), r["t"])
        app_tickers = {r["t"] for r in idx["rows"]}
    except Exception as e:  # noqa: BLE001
        log(f"no app index: {e}")
        app_tickers = set()
    sec = B.SEC()
    filers = []
    for f in cfg:
        log(f"== {f['name']}")
        cik, ename, s = resolve(sec, f)
        if not cik:
            log(f"  could not resolve {f['name']}; skipped")
            filers.append({**f, "error": "No 13F filings found on EDGAR"})
            continue
        qs = []
        for fl in filings_13f(s):
            key = fl["acc"]
            if key not in fcache:
                try:
                    fcache[key] = aggregate(info_table(sec, cik, fl["acc"]), fl["period"])
                except Exception as e:  # noqa: BLE001
                    log(f"  {fl['acc']}: {type(e).__name__}: {e}")
                    continue
            ag = fcache[key]
            qs.append({**fl, "agg": ag})
        log(f"  CIK {cik} {ename}: {len(qs)} quarters" + (f", latest {qs[0]['period']} filed {qs[0]['filed']}, ${qs[0]['agg']['total'] / 1e9:.1f}B, {qs[0]['agg']['n']} positions" if qs else ""))
        filers.append({**f, "cik": cik, "entity": ename, "qs": qs})
    # keep the filings cache to the quarters still in use
    used = {q["acc"] for f in filers for q in f.get("qs", [])}
    fcache = {k: v for k, v in fcache.items() if k in used}

    # CUSIP -> ticker for the top positions of every quarter
    need = {}
    for f in filers:
        for q in f.get("qs", []):
            top = sorted(q["agg"]["eq"].items(), key=lambda kv: -kv[1]["value"])[:TOP_N]
            for c, _ in top:
                need[c] = q["agg"]["names"].get(c, "")
    cmap = map_cusips(need, universe, cache)
    tickers = sorted({t for t in cmap.values() if t}) + ["SPY"]
    log(f"prices: {len(tickers)} tickers")
    P = download(tickers)
    today = date.today().isoformat()

    out_f = []
    for f in filers:
        base = {k: f.get(k) for k in ("name", "manager", "style", "group", "cik", "entity")}
        if f.get("error") or not f.get("qs"):
            out_f.append({**base, "error": f.get("error") or "No 13F filings found"})
            continue
        qs = f["qs"]

        def topl(q):
            tot = q["agg"]["total"] or 1
            return [{"cusip": c, "name": q["agg"]["names"].get(c, ""), "t": cmap.get(c), "value": round(v["value"]), "shares": v["shares"],
                     "pct": round(v["value"] / tot, 5)} for c, v in sorted(q["agg"]["eq"].items(), key=lambda kv: -kv[1]["value"])[:TOP_N]]
        cur, prev = qs[0], (qs[1] if len(qs) > 1 else None)
        top = topl(cur)
        pe = prev["agg"]["eq"] if prev else {}
        for x in top:
            p = pe.get(x["cusip"])
            if not prev:
                x["chg"] = None
            elif not p:
                x["chg"] = "new"
            else:
                ch = x["shares"] / p["shares"] - 1 if p["shares"] else 0
                x["chg"], x["chg_pct"] = ("add" if ch > 0.02 else "trim" if ch < -0.02 else "same"), round(ch, 4)
            x["in_app"] = x["t"] in app_tickers
        sold = []
        if prev:
            ptot = prev["agg"]["total"] or 1
            for c, p in sorted(pe.items(), key=lambda kv: -kv[1]["value"]):
                if c not in cur["agg"]["eq"] and p["value"] / ptot >= 0.002:
                    sold.append({"cusip": c, "name": prev["agg"]["names"].get(c, ""), "t": cmap.get(c), "prev_pct": round(p["value"] / ptot, 5)})
        opts = sorted(cur["agg"]["opt"].items(), key=lambda kv: -kv[1])[:10]
        hist = [{"period": q["period"], "filed": q["filed"], "value": round(q["agg"]["total"]), "n": q["agg"]["n"]} for q in qs]
        cc = copycat([{"filed": q["filed"], "top": [{"t": cmap.get(c), "value": v["value"]} for c, v in
                                                    sorted(q["agg"]["eq"].items(), key=lambda kv: -kv[1]["value"])[:TOP_N]]} for q in reversed(qs)],
                     P, today)
        top10 = sum(x["pct"] for x in top[:10])
        out_f.append({**base, "period": cur["period"], "filed": cur["filed"], "value": round(cur["agg"]["total"]), "n": cur["agg"]["n"],
                      "top10_pct": round(top10, 4), "top": top, "sold": sold[:25],
                      "options": [{"cusip": k.split("|")[0], "pc": k.split("|")[1], "name": cur["agg"]["names"].get(k.split("|")[0], ""),
                                   "t": cmap.get(k.split("|")[0]), "value": round(v)} for k, v in opts],
                      "history": hist, "copycat": cc})

    # consensus across funds (latest quarter each)
    own, buy, sell = defaultdict(list), defaultdict(list), defaultdict(list)
    names = {}
    for f in out_f:
        if f.get("error") or f.get("group") != "fund":
            continue
        for x in f["top"]:
            k = x["t"] or x["cusip"]
            names[k] = x["name"]
            own[k].append({"fund": f["name"], "pct": x["pct"]})
            if x.get("chg") in ("new", "add"):
                buy[k].append({"fund": f["name"], "chg": x["chg"], "pct": x["pct"]})
        for x in f["sold"]:
            k = x["t"] or x["cusip"]
            names[k] = x["name"]
            sell[k].append({"fund": f["name"], "prev_pct": x["prev_pct"]})
        for x in f["top"]:
            if x.get("chg") == "trim" and (x.get("chg_pct") or 0) <= -0.25:
                k = x["t"] or x["cusip"]
                sell[k].append({"fund": f["name"], "trim": x["chg_pct"]})

    def rank(d, n=25):
        rows = [{"k": k, "t": k if k in cmap.values() else None, "name": names.get(k, ""), "in_app": k in app_tickers, "funds": v}
                for k, v in d.items()]
        rows.sort(key=lambda r: (-len(r["funds"]), -sum(x.get("pct", x.get("prev_pct", 0)) or 0 for x in r["funds"])))
        return rows[:n]
    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "filers": out_f,
           "consensus": {"most_owned": rank(own), "most_bought": rank(buy), "most_sold": rank(sell)},
           "notes": {"top_n": TOP_N, "quarters": QUARTERS}, "log": LOG[-200:]}
    Path(a.out).write_text(json.dumps(res, separators=(",", ":"), default=lambda o: None if isinstance(o, float) and math.isnan(o) else str(o)))
    (cdir / "cusip_map.json").write_text(json.dumps(cache, separators=(",", ":")))
    (cdir / "filings.json").write_text(json.dumps(fcache, separators=(",", ":")))
    ok = sum(1 for f in out_f if not f.get("error"))
    log(f"research.json: {ok}/{len(out_f)} filers, {Path(a.out).stat().st_size / 1e6:.2f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
