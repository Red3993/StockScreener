#!/usr/bin/env python3
"""Senate periodic transaction reports (PTRs) -> senate.json, uploaded to the repo's `senate-data` branch.

Runs on YOUR computer: efdsearch.senate.gov blocks cloud servers (GitHub runners get "Access Denied"),
but it serves normal home internet connections.

    python3 senate_trades.py            # fetch new reports, write senate.json, upload it
    python3 senate_trades.py --no-upload
    python3 senate_trades.py --test     # fetch 3 recent reports and print what was parsed; nothing is uploaded

Upload needs a GitHub token with "Contents: read and write" on the StockScreener repo, in the environment
variable STOCKSCREENER_TOKEN or in a file ~/.stockscreener_token (one line). See README.md in this folder.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing package: run  python3 -m pip install requests")

REPO = "Red3993/StockScreener"
BRANCH = "senate-data"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "senate_cache.json"
OUT = HERE / "senate.json"
BASE = "https://efdsearch.senate.gov"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
AMOUNT = re.compile(r"\$([\d,]+)")
TYPES = {"purchase": "Purchase", "sale (full)": "Sale", "sale (partial)": "Sale (partial)", "sale": "Sale", "exchange": "Exchange"}
ASSET = {"stock": "ST", "stock option": "OP", "options": "OP", "corporate bond": "CS", "municipal security": "GS",
         "mutual fund": "MF", "other securities": "OT", "cryptocurrency": "CT"}


def log(m):
    print(f"{datetime.now():%H:%M:%S} {m}", flush=True)


class Senate:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.token = None

    def agree(self):
        r = self.s.get(BASE + "/search/home/", timeout=60)
        if r.status_code == 403:
            sys.exit("The Senate site returned 403 (blocked). This script must run from a home/office connection, not a VPN or cloud server.")
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
        if not m:
            sys.exit("Couldn't find the agreement form on efdsearch.senate.gov (the site may have changed).")
        self.s.post(BASE + "/search/home/", data={"csrfmiddlewaretoken": m.group(1), "prohibition_agreement": "1"},
                    headers={"Referer": BASE + "/search/home/"}, timeout=60)
        self.token = self.s.cookies.get("csrftoken") or self.s.cookies.get("csrf")
        if not self.token:
            sys.exit("Agreement step didn't return a session token (the site may have changed).")

    def search(self, start_date: date, offset: int, length: int = 100):
        data = {"start": str(offset), "length": str(length), "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": start_date.strftime("%m/%d/%Y 00:00:00"), "submitted_end_date": "",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": self.token}
        r = self.s.post(BASE + "/search/report/data/", data=data, timeout=60,
                        headers={"Referer": BASE + "/search/", "X-CSRFToken": self.token})
        r.raise_for_status()
        return r.json()

    def report(self, path: str) -> str:
        r = self.s.get(BASE + path, timeout=60)
        if r.url.rstrip("/").endswith("/search/home"):   # session expired: agree again and retry once
            self.agree()
            r = self.s.get(BASE + path, timeout=60)
        r.raise_for_status()
        return r.text


def strip(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def parse_ptr(page: str) -> list[dict]:
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not body:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
        cells = [strip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 8:
            continue
        _, tdate, owner, ticker, asset, atype, ttype, amount = cells[:8]
        comment = cells[8] if len(cells) > 8 else ""
        nums = [int(x.replace(",", "")) for x in AMOUNT.findall(amount)]
        try:
            d = datetime.strptime(tdate, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        asset = re.sub(r"\s*Rate/Coupon:.*$", "", asset).strip()
        tk = ticker if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", ticker or "") else None
        out.append({"date": d, "owner": owner or "Self", "ticker": tk, "asset": asset, "asset_type": ASSET.get(atype.lower(), atype[:2].upper() if atype else None),
                    "type": TYPES.get(ttype.lower(), ttype), "amount_low": nums[0] if nums else None,
                    "amount_high": nums[1] if len(nums) > 1 else None, "desc": comment if comment not in ("--", "") else ""})
    return out


def upload(content: bytes):
    tok = os.environ.get("STOCKSCREENER_TOKEN")
    tf = Path.home() / ".stockscreener_token"
    if not tok and tf.exists():
        tok = tf.read_text().strip()
    if not tok:
        log("No GitHub token found (STOCKSCREENER_TOKEN or ~/.stockscreener_token); skipping upload.")
        return False
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{REPO}"
    r = requests.get(f"{api}/git/ref/heads/{BRANCH}", headers=h, timeout=60)
    if r.status_code == 404:
        main = requests.get(f"{api}/git/ref/heads/main", headers=h, timeout=60).json()["object"]["sha"]
        requests.post(f"{api}/git/refs", headers=h, json={"ref": f"refs/heads/{BRANCH}", "sha": main}, timeout=60).raise_for_status()
    cur = requests.get(f"{api}/contents/senate.json", headers=h, params={"ref": BRANCH}, timeout=60)
    sha = cur.json().get("sha") if cur.ok else None
    body = {"message": f"senate {date.today()}", "branch": BRANCH, "content": base64.b64encode(content).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{api}/contents/senate.json", headers=h, json=body, timeout=120)
    if not r.ok:
        log(f"Upload failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    log("Uploaded senate.json to GitHub.")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"reports": {}, "trades": []}
    sen = Senate()
    sen.agree()
    start = date.today() - timedelta(days=365 * args.years + 10)
    rows, off = [], 0
    while True:
        j = sen.search(start, off)
        batch = j.get("data", [])
        rows += batch
        log(f"search: {len(rows)} of {j.get('recordsTotal', '?')} reports")
        if len(batch) < 100 or args.test:
            break
        off += 100
        time.sleep(0.5)
    new = 0
    for first, last, full, link, received in rows[:3] if args.test else rows:
        m = re.search(r'href="([^"]+)"', link)
        if not m:
            continue
        path = m.group(1)
        rid = path.strip("/").split("/")[-1]
        if rid in cache["reports"] and not args.test:
            continue
        filed = datetime.strptime(received, "%m/%d/%Y").date().isoformat()
        name = re.sub(r"\s*\(.*?\)\s*", "", strip(full)) or f"{first} {last}"
        if "/paper/" in path:
            cache["reports"][rid] = {"status": "paper", "filed": filed, "name": name}
            continue
        try:
            tx = parse_ptr(sen.report(path))
        except Exception as e:  # noqa: BLE001
            log(f"  {name} {filed}: {e}")
            continue
        for t in tx:
            t.update(name=name, first=first.strip(), last=last.strip(), filed=filed, url=BASE + path, chamber="Senate", report_id=rid)
        if args.test:
            log(f"TEST {name} {filed}: {len(tx)} transactions")
            for t in tx[:5]:
                log(f"     {t['date']} {t['ticker']} {t['type']} {t['amount_low']}-{t['amount_high']} {t['owner']} | {t['asset'][:50]}")
            continue
        cache["trades"] += tx
        cache["reports"][rid] = {"status": "ok", "filed": filed, "name": name, "n": len(tx)}
        new += 1
        if new % 50 == 0:
            log(f"  parsed {new} new reports")
            CACHE.write_text(json.dumps(cache))
        time.sleep(0.4)
    if args.test:
        log("Test finished. Nothing was saved or uploaded.")
        return
    cutoff = start.isoformat()
    cache["trades"] = [t for t in cache["trades"] if (t.get("date") or "") >= cutoff]
    CACHE.write_text(json.dumps(cache))
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "trades": cache["trades"],
           "stats": {"reports": len(cache["reports"]), "paper_skipped": sum(1 for v in cache["reports"].values() if v["status"] == "paper"),
                     "trades": len(cache["trades"]), "new_reports_this_run": new}}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    log(f"Wrote {OUT.name}: {out['stats']}")
    if not args.no_upload:
        upload(OUT.read_bytes())


if __name__ == "__main__":
    main()
