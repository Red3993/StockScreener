#!/usr/bin/env python3
"""Dev only: (a) try Senate eFD from the runner with an honest User-Agent; (b) inspect Kadoa's Senate filer files."""
import json, re, subprocess, glob, time
from pathlib import Path
import requests
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
rep = {}
# (a) eFD with honest UA (amodi24 style)
for ua in ["StockScreener/1.0 (personal, non-commercial research)", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"]:
    s = requests.Session(); s.headers.update({"User-Agent": ua}); r = {}
    try:
        h = s.get("https://efdsearch.senate.gov/search/home/", timeout=30); r["home"] = h.status_code
        tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', h.text)
        a = s.post("https://efdsearch.senate.gov/search/home/", data={"csrfmiddlewaretoken": tok.group(1) if tok else "", "prohibition_agreement": "1"}, headers={"Referer": "https://efdsearch.senate.gov/search/home/"}, timeout=30); r["agree"] = a.status_code
        c = s.cookies.get("csrftoken")
        d = s.post("https://efdsearch.senate.gov/search/report/data/", data={"draw": 1, "start": 0, "length": 5, "report_types": "[11]", "filer_types": "[]", "submitted_start_date": "09/01/2026 00:00:00", "submitted_end_date": "", "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": ""},
                   headers={"X-CSRFToken": c or "", "X-Requested-With": "XMLHttpRequest", "Referer": "https://efdsearch.senate.gov/search/"}, timeout=30)
        r["data"] = d.status_code; r["body"] = d.text[:400]
    except Exception as e:
        r["err"] = str(e)[:200]
    rep["efd " + ua[:20]] = r; time.sleep(2)
# (b) kadoa senate filers via sparse checkout
d = Path("/tmp/kadoa")
subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", "https://github.com/kadoa-org/congress-trading-monitor", str(d)], check=True)
subprocess.run(["git", "-C", str(d), "sparse-checkout", "set", "--no-cone", "public/data/filer/senate_*", "public/data/filers.json", "public/data/stats.json", "LICENSE", "README.md", "scripts", "scraper", "pipeline"], check=True)
files = sorted(glob.glob(str(d / "public/data/filer/senate_*.json")))
rep["kadoa_senate_files"] = len(files)
rep["kadoa_license"] = (d / "LICENSE").read_text()[:200] if (d / "LICENSE").exists() else None
rep["kadoa_stats"] = (d / "public/data/stats.json").read_text()[:1200] if (d / "public/data/stats.json").exists() else None
rep["kadoa_last_commit"] = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI %s"], capture_output=True, text=True).stdout.strip()
samples = {}
for f in files[:3] + [x for x in files if "tuberville" in x or "mullin" in x][:2]:
    j = json.loads(Path(f).read_text()); samples[Path(f).name] = json.dumps(j)[:2500]
rep["kadoa_samples"] = samples
fj = json.loads((d / "public/data/filers.json").read_text()) if (d / "public/data/filers.json").exists() else None
rep["kadoa_filers_head"] = json.dumps(fj)[:1500] if fj else None
other = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and "public/data" not in str(p) and ".git/" not in str(p)]
rep["kadoa_other_files"] = other[:80]
(OUT / "senate_probe.json").write_text(json.dumps(rep, indent=1))
print(json.dumps(rep, indent=1)[:5000])
