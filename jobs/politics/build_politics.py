#!/usr/bin/env python3
"""Congressional trades (House PTRs) -> politics.json, with an incremental cache.

Reads the previous politics.json (if any) so already-processed DocIDs are never fetched again.
Writes: trades (stocks/options with tickers), members (bioguide, party, state, committees), stats.
Senate eFD blocks datacenter IPs (403 from GitHub runners, verified 2026-10-24), so the Senate is
not covered here; see CLAUDE.md.

    python jobs/politics/build_politics.py --cache politics.json --out politics.json --years 3
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.politics.house import parse_ptr_pdf  # noqa: E402

BUA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
CL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"
T0 = time.time()


def log(m):
    print(f"[{time.time() - T0:7.1f}s] {m}", flush=True)


def members_index():
    leg = yaml.safe_load(requests.get(CL + "legislators-current.yaml", timeout=60).text)
    cm = yaml.safe_load(requests.get(CL + "committee-membership-current.yaml", timeout=60).text)
    cmeta = yaml.safe_load(requests.get(CL + "committees-current.yaml", timeout=60).text)
    names = {}
    for c in cmeta:
        names[c["thomas_id"]] = c["name"]
        for sc in c.get("subcommittees", []) or []:
            names[c["thomas_id"] + sc["thomas_id"]] = c["name"] + " — " + sc["name"]
    by_bio, by_district = {}, {}
    for p in leg:
        t = p["terms"][-1]
        bio = p["id"]["bioguide"]
        nm = p["name"].get("official_full") or f'{p["name"]["first"]} {p["name"]["last"]}'
        by_bio[bio] = {"bioguide": bio, "name": nm, "last": p["name"]["last"], "party": t.get("party"), "state": t["state"],
                       "chamber": "House" if t["type"] == "rep" else "Senate", "district": t.get("district"), "committees": []}
        if t["type"] == "rep":
            by_district[f'{t["state"]}{int(t.get("district") or 0):02d}'] = bio
    for cid, mem in cm.items():
        for m in mem or []:
            b = m.get("bioguide")
            if b in by_bio:
                by_bio[b]["committees"].append({"id": cid, "name": names.get(cid, cid), "title": m.get("title")})
    return by_bio, by_district


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="politics.json")
    ap.add_argument("--out", default="politics.json")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--max-new", type=int, default=2500)
    ap.add_argument("--senate", default="senate.json", help="output of scripts/senate/senate_trades.py (run on a home computer)")
    args = ap.parse_args()
    cache = {"processed": {}, "trades": []}
    if Path(args.cache).exists():
        try:
            cache = json.loads(Path(args.cache).read_text())
            log(f"cache: {len(cache['processed'])} filings, {len(cache['trades'])} trades")
        except Exception as e:  # noqa: BLE001
            log(f"cache unreadable, starting fresh: {e}")
    processed, trades = cache.get("processed", {}), cache.get("trades", [])
    by_bio, by_district = members_index()
    s = requests.Session()
    s.headers.update(BUA)
    y0 = date.today().year
    new = 0
    for year in range(y0 - args.years + 1, y0 + 1):
        r = s.get(f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip", timeout=120)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        txt = z.read(f"{year}FD.txt").decode("utf-8", "replace")
        rows = [x for x in csv.DictReader(io.StringIO(txt), delimiter="\t") if x.get("FilingType") == "P"]
        log(f"{year}: {len(rows)} PTR filings in index")
        for x in rows:
            doc = x["DocID"].strip()
            if doc in processed or new >= args.max_new:
                continue
            filed = datetime.strptime(x["FilingDate"], "%m/%d/%Y").date().isoformat()
            url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
            bio = by_district.get(x["StateDst"].strip())
            if bio and by_bio[bio]["last"].lower() not in (x["Last"] or "").lower():
                bio = None   # seat changed hands; don't attribute to the current holder
            member = {"name": f'{x["First"]} {x["Last"]}'.strip(), "district": x["StateDst"], "bioguide": bio}
            if not (len(doc) == 8 and doc.startswith("2")):
                processed[doc] = {"status": "paper", "filed": filed, **member}
                continue
            try:
                pr = s.get(url, timeout=60)
                pr.raise_for_status()
                rows_ = parse_ptr_pdf(pr.content)
                for t in rows_:
                    t.update(doc_id=doc, filed=filed, url=url, chamber="House", **member)
                trades += rows_
                processed[doc] = {"status": "ok", "n": len(rows_), "filed": filed}
            except Exception as e:  # noqa: BLE001
                processed[doc] = {"status": "error", "error": str(e)[:200], "filed": filed}
            new += 1
            if new % 100 == 0:
                log(f"  parsed {new} new filings ({len(trades)} trades total)")
            time.sleep(0.25)
    # Senate (from the local-runner script); replaces any Senate rows from earlier runs
    trades = [t for t in trades if t.get("chamber") != "Senate"]
    senate_stats = None
    if Path(args.senate).exists():
        sj = json.loads(Path(args.senate).read_text())
        sens = [b for b, m in by_bio.items() if m["chamber"] == "Senate"]
        def match(t):
            last, first = (t.get("last") or "").lower(), (t.get("first") or "").lower()
            c = [b for b in sens if by_bio[b]["last"].lower() == last]
            if len(c) > 1:
                c = [b for b in c if first[:3] and first[:3] in by_bio[b]["name"].lower()] or c[:1]
            return c[0] if c else None
        n = 0
        for t in sj.get("trades", []):
            b = match(t)
            t2 = {k: t.get(k) for k in ("date", "owner", "ticker", "asset", "asset_type", "type", "amount_low", "amount_high", "desc", "filed", "url")}
            t2.update(chamber="Senate", name=t.get("name"), bioguide=b, district=by_bio[b]["state"] if b else None)
            trades.append(t2)
            n += 1
        senate_stats = {"trades": n, "generated": sj.get("generated"), "reports": (sj.get("stats") or {}).get("reports")}
        log(f"senate: merged {n} trades (file generated {sj.get('generated')})")
    else:
        log("senate: no senate.json (run scripts/senate/senate_trades.py on a home computer)")
    cutoff = f"{y0 - args.years}-{date.today():%m-%d}"
    trades = [t for t in trades if (t.get("date") or "") >= cutoff]
    # refresh member attributes on every run (committees change)
    members = {}
    for t in trades:
        b = t.get("bioguide")
        if b and b in by_bio:
            members[b] = by_bio[b]
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "processed": processed, "trades": trades,
           "members": members,
           "stats": {"filings": len(processed), "paper_skipped": sum(1 for v in processed.values() if v["status"] == "paper"),
                     "errors": sum(1 for v in processed.values() if v["status"] == "error"), "trades": len(trades),
                     "with_ticker": sum(1 for t in trades if t.get("ticker")), "new_this_run": new,
                     "senate": senate_stats,
                     "coverage": "House + Senate" if senate_stats else "House only (Senate script not run yet)"}}
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")))
    log(f"wrote {args.out}: {out['stats']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
