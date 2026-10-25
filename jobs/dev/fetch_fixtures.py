#!/usr/bin/env python3
"""Dev only: raw samples of House PTRs, Senate eFD PTRs, options chains and volume for offline parser work."""
import io, json, os, re, time, zipfile, pickle, csv
from datetime import date, timedelta
from pathlib import Path
import requests
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
BUA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
log = []
# ---- House
y = date.today().year
r = requests.get(f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip", headers=BUA, timeout=60)
z = zipfile.ZipFile(io.BytesIO(r.content)); names = z.namelist(); log.append(("house zip", r.status_code, names))
txt = z.read(f"{y}FD.txt").decode("utf-8", "replace")
(OUT / f"house_{y}FD.txt").write_text(txt)
rows = [x for x in csv.DictReader(io.StringIO(txt), delimiter="\t") if x.get("FilingType") == "P"]
log.append(("house PTR rows", len(rows), rows[:2]))
rows.sort(key=lambda x: x["FilingDate"][-4:] + x["FilingDate"][:5], reverse=True)
efiled = [x for x in rows if x["DocID"].startswith("2") and len(x["DocID"]) == 8][:6]
paper = [x for x in rows if len(x["DocID"]) == 7][:1]
pel = [x for x in rows if "Pelosi" in x.get("Last", "")][:2]
for x in efiled + paper + pel:
    u = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{x['DocID']}.pdf"
    rr = requests.get(u, headers=BUA, timeout=60); log.append(("pdf", x["DocID"], x["Last"], rr.status_code, len(rr.content)))
    (OUT / f"house_ptr_{x['DocID']}.pdf").write_bytes(rr.content); time.sleep(0.5)
# ---- Senate
S = requests.Session(); S.headers.update(BUA)
h = S.get("https://efdsearch.senate.gov/search/home/", timeout=60)
tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', h.text).group(1)
a = S.post("https://efdsearch.senate.gov/search/home/", data={"prohibition_agreement": "1", "csrfmiddlewaretoken": tok}, headers={"Referer": "https://efdsearch.senate.gov/search/home/"}, timeout=60)
log.append(("senate agree", a.status_code, a.url))
csrf = S.cookies.get("csrftoken")
form = {"start": "0", "length": "25", "report_types": "[11]", "filer_types": "[]", "submitted_start_date": (date.today() - timedelta(days=60)).strftime("%m/%d/%Y 00:00:00"),
        "submitted_end_date": "", "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "", "csrfmiddlewaretoken": csrf}
d = S.post("https://efdsearch.senate.gov/search/report/data/", data=form, headers={"Referer": "https://efdsearch.senate.gov/search/", "X-CSRFToken": csrf}, timeout=60)
log.append(("senate data", d.status_code, d.text[:300]))
(OUT / "senate_search.json").write_text(d.text)
try:
    for row in d.json()["data"][:6]:
        m = re.search(r'href="([^"]+)"', row[3]);
        if not m: continue
        u = "https://efdsearch.senate.gov" + m.group(1)
        rr = S.get(u, timeout=60); log.append(("senate ptr", u, rr.status_code, len(rr.text)))
        (OUT / ("senate_" + m.group(1).strip("/").split("/")[-1] + ".html")).write_text(rr.text); time.sleep(0.5)
except Exception as e:
    log.append(("senate err", str(e)))
# ---- congress-legislators
for f in ["legislators-current.yaml", "committee-membership-current.yaml", "committees-current.yaml"]:
    rr = requests.get(f"https://raw.githubusercontent.com/unitedstates/congress-legislators/main/{f}", timeout=60); (OUT / f).write_text(rr.text)
# ---- options / volume / short interest
import yfinance as yf
opt = {}
for t in ["AAPL", "JPM", "WDFC"]:
    tk = yf.Ticker(t)
    try:
        ex = tk.options; opt[t] = {"expiries": list(ex[:6])}
        ch = tk.option_chain(ex[0]); opt[t]["calls"] = ch.calls.head(3).to_dict("records"); opt[t]["puts"] = ch.puts.head(3).to_dict("records")
        opt[t]["tot"] = {"call_vol": float(ch.calls["volume"].fillna(0).sum()), "put_vol": float(ch.puts["volume"].fillna(0).sum()), "call_oi": float(ch.calls["openInterest"].fillna(0).sum()), "put_oi": float(ch.puts["openInterest"].fillna(0).sum())}
        i = tk.info; opt[t]["short"] = {k: i.get(k) for k in ["shortPercentOfFloat", "shortRatio", "sharesShort", "sharesShortPriorMonth", "dateShortInterest", "heldPercentInsiders", "heldPercentInstitutions"]}
    except Exception as e:
        opt[t] = f"ERR {type(e).__name__}: {e}"
(OUT / "options.json").write_text(json.dumps(opt, indent=1, default=str))
px = yf.download(["AAPL", "JPM"], period="6mo", auto_adjust=True, progress=False); px.to_pickle(OUT / "prices_vol.pkl")
(OUT / "log.json").write_text(json.dumps(log, indent=1, default=str))
print(json.dumps(log, default=str)[:4000])
