#!/usr/bin/env python3
"""Senate PTR trades from the open Kadoa dataset -> senate.json (same shape as scripts/senate/senate_trades.py).

Source: github.com/kadoa-org/congress-trading-monitor (MIT licence), which republishes Senate eFD
periodic transaction reports as static JSON (public/data/filer/senate_*.json), refreshed daily.
Senate eFD itself blocks GitHub runners (403, verified 2026-10-24 with both a browser and an honest UA),
so this open copy is the primary Senate source; the home-computer script stays as a fallback.

    python jobs/politics/senate_kadoa.py --out senate.json --years 3
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO = "https://github.com/kadoa-org/congress-trading-monitor"
TYPES = {"purchase": "Purchase", "sale (full)": "Sale", "sale (partial)": "Sale (partial)", "sale": "Sale", "exchange": "Exchange"}
ASSET = {"stock": "ST", "stock option": "OP", "options": "OP", "corporate bond": "CS", "municipal security": "GS",
         "mutual fund": "MF", "other securities": "OT", "cryptocurrency": "CT"}
BIO = re.compile(r"/([A-Z]\d{6})\.jpg")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="senate.json")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--src", help="already-cloned repo (skip clone)")
    args = ap.parse_args()
    d = Path(args.src) if args.src else Path(tempfile.mkdtemp()) / "kadoa"
    if not args.src:
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", REPO, str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "sparse-checkout", "set", "--no-cone", "public/data/filer/senate_*"], check=True)
    commit = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI %s"], capture_output=True, text=True).stdout.strip()
    cutoff = f"{date.today().year - args.years}-{date.today():%m-%d}"
    trades, filers, reports = [], 0, set()
    for f in sorted(glob.glob(str(d / "public/data/filer/senate_*.json"))):
        j = json.loads(Path(f).read_text())
        fi = j.get("filer") or {}
        m = BIO.search(fi.get("photo_url") or "")
        filers += 1
        name = fi.get("full_name") or ""
        parts = name.replace(".", "").split()
        for t in j.get("trades") or []:
            if t.get("source_id") != "senate_efd" or (t.get("transaction_date") or "") < cutoff:
                continue
            tk = (t.get("ticker") or "").strip().upper()
            tk = tk if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", tk) and tk not in ("N/A", "--") else None
            asset = re.sub(r"\s+", " ", t.get("asset_name") or "").strip()
            at = (t.get("asset_type") or "").strip()
            reports.add(t.get("filing_id"))
            trades.append({"date": t.get("transaction_date"), "owner": t.get("owner") or "Self", "ticker": tk, "asset": asset,
                           "asset_type": ASSET.get(at.lower(), at[:2].upper() or None),
                           "type": TYPES.get((t.get("transaction_type") or "").lower(), t.get("transaction_type")),
                           "amount_low": t.get("amount_range_low"), "amount_high": t.get("amount_range_high"),
                           "desc": t.get("comment") or "", "filed": t.get("filing_date"), "url": t.get("doc_url"),
                           "name": name, "first": parts[0] if parts else "", "last": parts[-1] if parts else "",
                           "bioguide": m.group(1) if m else None, "chamber": "Senate", "report_id": t.get("filing_id")})
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": f"kadoa-org/congress-trading-monitor ({commit})",
           "trades": trades, "stats": {"reports": len(reports), "filers": filers, "trades": len(trades),
                                       "with_ticker": sum(1 for t in trades if t["ticker"])}}
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")))
    print(f"senate (kadoa {commit}): {out['stats']}")
    return 0 if trades else 1


if __name__ == "__main__":
    sys.exit(main())
