"""Trim the per-stock payload so the app stays light on a phone (target: well under 6 MB for 100 stocks)."""
from __future__ import annotations

import math
from datetime import date, timedelta


def _r(v, sig=4):
    if isinstance(v, float) and math.isfinite(v) and v != 0:
        return float(f"{v:.{sig}g}")
    return v


def slim_stock(s: dict, today: date) -> dict:
    s.pop("volume", None)
    cut = (today - timedelta(days=365)).isoformat()
    cong, seen = [], set()
    for t in s.get("congress") or []:
        key = (t.get("name"), t.get("date"), t.get("type"), t.get("asset_type"), t.get("owner"), t.get("amount_low"), t.get("desc"), t.get("url"))
        if key in seen:
            continue   # the same line can appear twice (lookup overlap or an amended filing repeating it)
        seen.add(key)
        if t.get("relevant"):
            cong.append(t)
        elif (t.get("date") or "") >= cut:
            cong.append({k: t.get(k) for k in ("date", "filed", "name", "party", "state", "owner", "type", "asset_type",
                                                 "amount_low", "amount_high", "committees", "tracked", "relevant")})
    s["congress"] = cong
    ins = s.get("insiders") or {}
    keep = ("date", "filed", "owner", "roles", "ceo", "cfo", "code", "shares", "price", "value", "plan_10b5_1")
    ins["trades"] = [{k: t.get(k) for k in keep} for t in (ins.get("trades") or [])[:120]]
    s["metric_history"] = {k: {d: _r(v) for d, v in h.items()} for k, h in (s.get("metric_history") or {}).items()}
    for n in s.get("news") or []:
        n["summary"] = (n.get("summary") or "")[:280]
    a = s.get("analyst") or {}
    a["actions"] = (a.get("actions") or [])[:30]
    return s
