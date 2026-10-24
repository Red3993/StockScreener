"""Form 4 parsing (section 4).

Keeps open-market purchases (P) and sales (S) only; option exercises, tax withholding,
gifts and grants are dropped. Sales are tagged with how 10b5-1 status was determined:
- "checkbox": the aff10b5One element (Forms filed from April 2023 on)
- "footnote": no checkbox element, but footnotes/remarks mention a trading plan
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

PLAN_RE = re.compile(r"10b5-?1|trading plan|pre-?arranged (trading )?plan", re.I)
CEO_RE = re.compile(r"\bCEO\b|chief executive", re.I)
CFO_RE = re.compile(r"\bCFO\b|chief financial", re.I)


def _t(el, path):
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else None


def _f(el, path):
    v = _t(el, path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def parse_form4(xml_bytes: bytes, accn: str, filed: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        title = None
        if rel is not None:
            if _t(rel, "isDirector") in ("1", "true"):
                roles.append("Director")
            if _t(rel, "isOfficer") in ("1", "true"):
                title = _t(rel, "officerTitle")
                roles.append(title or "Officer")
            if _t(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
            if _t(rel, "isOther") in ("1", "true"):
                roles.append(_t(rel, "otherText") or "Other")
        owners.append({"name": _t(ro, "reportingOwnerId/rptOwnerName"), "cik": _t(ro, "reportingOwnerId/rptOwnerCik"),
                       "roles": roles, "title": title})
    if not owners:
        return []
    checkbox = _t(root, "aff10b5One")
    notes = " ".join((fn.text or "") for fn in root.iter("footnote")) + " " + (_t(root, "remarks") or "")
    footnote_plan = bool(PLAN_RE.search(notes))
    out = []
    for tx in root.iter("nonDerivativeTransaction"):
        code = _t(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _f(tx, "transactionAmounts/transactionShares/value")
        price = _f(tx, "transactionAmounts/transactionPricePerShare/value")
        if not shares:
            continue
        plan = None
        if code == "S":
            if checkbox is not None:
                plan = "checkbox" if checkbox in ("1", "true") else None
            elif footnote_plan:
                plan = "footnote"
        o = owners[0]
        title = o["title"] or ""
        out.append({
            "date": _t(tx, "transactionDate/value"), "filed": filed, "accn": accn,
            "owner": o["name"], "owners": len(owners), "roles": o["roles"],
            "ceo": bool(CEO_RE.search(title)), "cfo": bool(CFO_RE.search(title)),
            "code": code, "shares": shares, "price": price,
            "value": round(shares * price, 2) if price else None,
            "after": _f(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            "direct": _t(tx, "ownershipNature/directOrIndirectOwnership/value"),
            "plan_10b5_1": plan,
            "plan_method": ("checkbox" if checkbox is not None else ("footnote scan" if code == "S" else None)),
        })
    return out


def cluster_buys(trades: list[dict], window_days: int = 14, min_insiders: int = 3) -> list[dict]:
    """Windows where >= min_insiders distinct insiders made open-market buys."""
    buys = sorted((t for t in trades if t["code"] == "P" and t["date"]), key=lambda t: t["date"])
    clusters, i = [], 0
    while i < len(buys):
        d0 = date.fromisoformat(buys[i]["date"])
        win = [t for t in buys[i:] if date.fromisoformat(t["date"]) <= d0 + timedelta(days=window_days)]
        names = {t["owner"] for t in win}
        if len(names) >= min_insiders:
            clusters.append({"start": buys[i]["date"], "end": win[-1]["date"], "insiders": sorted(names),
                             "value": round(sum(t["value"] or 0 for t in win), 2)})
            i += len(win)
        else:
            i += 1
    return clusters
