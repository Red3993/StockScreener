"""Attach congressional trades to stocks and decide which ones matter.

A trade is shown by default only when:
  - the member sits on a committee (or subcommittee) whose oversight covers the stock's GICS sector or
    SIC industry (config/committee_sectors.yaml), or
  - the member is on the tracked list (config/tracked_people.yaml), e.g. Pelosi.
Everything else is kept but marked as noise, so the app can show it behind a toggle.
Committee membership is CURRENT membership; older trades may predate an assignment.
"""
from __future__ import annotations

import yaml


def _flatten(ranges):
    out = []
    for r in ranges or []:
        if r and isinstance(r[0], list):
            out += _flatten(r)
        elif r:
            out.append((int(r[0]), int(r[1])))
    return out


class CommitteeMatcher:
    def __init__(self, committee_cfg_path, tracked_cfg_path):
        cfg = yaml.safe_load(open(committee_cfg_path))
        self.comm = {}
        for cid, c in (cfg.get("committees") or {}).items():
            self.comm[cid] = {"name": c.get("name", cid), "sectors": set(c.get("sectors") or []), "sic": _flatten(c.get("sic")),
                              "tickers": {str(x).upper() for x in (c.get("tickers") or [])}}
        self.tracked = yaml.safe_load(open(tracked_cfg_path)).get("people") or []

    def covers(self, cid: str, sector: str, sic: int | None, ticker: str = ""):
        c = self.comm.get(cid) or self.comm.get(cid[:4])
        if not c:
            return None
        if sector in c["sectors"] or (sic and any(lo <= sic <= hi for lo, hi in c["sic"])) or ticker.upper() in c["tickers"]:
            return c["name"]
        return None

    def tracked_reason(self, t: dict):
        for p in self.tracked:
            if t.get("bioguide") and t["bioguide"] == p.get("bioguide"):
                return p.get("reason") or "Tracked"
            m = p.get("match") or {}
            if m.get("last") and m["last"].lower() in (t.get("name") or "").lower() and (t.get("district") or "").startswith(m.get("district_prefix", "")):
                return p.get("reason") or "Tracked"
        return None

    def annotate(self, trades: list[dict], members: dict, sector: str, sic: int | None, ticker: str = "") -> list[dict]:
        out = []
        for t in trades:
            mem = members.get(t.get("bioguide") or "", {})
            hits = []
            for c in mem.get("committees", []):
                nm = self.covers(c["id"], sector, sic, ticker)
                if nm and nm not in hits:
                    hits.append(nm)
            tr = self.tracked_reason(t)
            out.append({"date": t.get("date"), "filed": t.get("filed"), "name": mem.get("name") or t.get("name"),
                        "party": mem.get("party"), "state": mem.get("state"), "chamber": t.get("chamber", "House"),
                        "owner": t.get("owner"), "type": t.get("type"), "asset_type": t.get("asset_type"),
                        "amount_low": t.get("amount_low"), "amount_high": t.get("amount_high"), "desc": t.get("desc"),
                        "url": t.get("url"), "committees": hits[:4], "tracked": tr,
                        "relevant": bool(hits or tr)})
        out.sort(key=lambda x: x["date"] or "", reverse=True)
        return out
