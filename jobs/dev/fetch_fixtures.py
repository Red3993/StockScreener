#!/usr/bin/env python3
"""Dev only: inspect open congress-trade projects (scraper code + data freshness)."""
import json, os, subprocess, glob
from pathlib import Path
OUT = Path("fixtures"); OUT.mkdir(exist_ok=True)
rep = {}
for r in ["amodi24/congress-stock-tracker", "kadoa-org/congress-trading-monitor", "anabolic-capital/congress-trades"]:
    d = Path("/tmp/" + r.replace("/", "_"))
    p = subprocess.run(["git", "clone", "--depth", "1", f"https://github.com/{r}", str(d)], capture_output=True, text=True)
    info = {"clone": p.returncode, "files": []}
    if p.returncode == 0:
        info["last_commit"] = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI %s"], capture_output=True, text=True).stdout.strip()
        for f in sorted(glob.glob(str(d) + "/**/*", recursive=True)):
            fp = Path(f)
            if fp.is_file() and ".git/" not in f:
                info["files"].append([f.replace(str(d) + "/", ""), fp.stat().st_size])
        lic = [x for x in info["files"] if x[0].upper().startswith("LICENSE")]
        if lic: info["license_head"] = (d / lic[0][0]).read_text()[:300]
        # copy small source files for reading
        for rel, size in info["files"]:
            if rel.endswith((".py", ".yml", ".yaml", ".md", ".ts", ".js")) and size < 120000 and not rel.startswith(("node_modules", "public/")):
                dst = OUT / r.replace("/", "_") / rel; dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text((d / rel).read_text(errors="replace"))
        # data summaries
        for rel, size in info["files"]:
            if rel.endswith(".json") and size > 10000:
                try:
                    j = json.loads((d / rel).read_text())
                    rows = j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), [])
                    info.setdefault("data", {})[rel] = {"size": size, "n": len(rows), "keys": list(rows[0].keys())[:25] if rows and isinstance(rows[0], dict) else None,
                                                        "sample": rows[:2] if rows else None,
                                                        "max_dates": sorted({str(x.get(k)) for x in rows[:200000] if isinstance(x, dict) for k in ("filing_date", "filed", "date_received", "disclosure_date", "transaction_date") if x.get(k)})[-3:]}
                except Exception as e:
                    info.setdefault("data", {})[rel] = {"size": size, "err": str(e)[:100]}
    rep[r] = info
(OUT / "repos.json").write_text(json.dumps(rep, indent=1, default=str)[:3_000_000])
print(json.dumps({k: {"last": v.get("last_commit"), "nfiles": len(v["files"]), "data": {a: {kk: b.get(kk) for kk in ("size", "n", "max_dates")} for a, b in v.get("data", {}).items()}} for k, v in rep.items()}, indent=1, default=str)[:6000])
