"""House Periodic Transaction Report (PTR) parsing.

Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip  (tab-delimited, FilingType P = PTR)
PDFs:  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf
E-filed PTRs (8-digit DocID starting with 2) have a text layer and are parsed here. Paper filings
(7-digit DocIDs) are scanned images and are skipped (counted, not parsed): OCR is out of scope for now.
"""
from __future__ import annotations

import io
import re
from datetime import datetime

START = re.compile(
    r"^(?:(SP|DC|JT)\s+)?(.+?)\s+(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(\$[\d,]+\s*-\s*(?:\$[\d,]+)?|\$[\d,]+ ?-?|Over \$[\d,]+|Spouse/DC Over \$[\d,]+)\s*(.*)$")
TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,7})\)")
ATYPE = re.compile(r"\[([A-Z]{2})\]")
MONEY = re.compile(r"\$([\d,]+)")
STOP = ("* For the complete list", "Initial Public Offerings", "I P O", "C S", "Certification", "I CERTIFY", "Digitally Signed")
SKIP = ("Filing ID", "Name:", "Status:", "State/District:", "ID Owner Asset", "Type Date Gains", "$200?", "Clerk of the House",
        "P T R", "F I", "T ")   # repeated page headers: ignore without ending the current record
OWNER = {"SP": "Spouse", "DC": "Dependent child", "JT": "Joint", None: "Self"}
TYPE = {"P": "Purchase", "S": "Sale", "S (partial)": "Sale (partial)", "E": "Exchange"}


def _amount(s: str) -> tuple[int | None, int | None]:
    nums = [int(x.replace(",", "")) for x in MONEY.findall(s)]
    if not nums:
        return None, None
    if "Over" in s:
        return nums[0], None
    return nums[0], (nums[1] if len(nums) > 1 else None)


def parse_ptr_text(text: str) -> list[dict]:
    lines = [ln.replace("\x00", "").rstrip() for ln in text.splitlines()]
    recs, cur = [], None

    def flush():
        if cur:
            recs.append(cur)

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = START.match(s)
        if m:
            flush()
            owner, asset, typ, d1, d2, amt, rest = m.groups()
            cur = {"owner": owner, "asset": asset + (" " + rest if rest else ""), "type": typ, "date": d1, "notified": d2,
                   "amount_text": amt, "desc": "", "cont": True}
            continue
        if cur is None:
            continue
        if s.startswith(STOP):
            cur["cont"] = False
            continue
        if s.startswith(SKIP) or re.fullmatch(r"[A-Z](\s+[A-Z])*", s):
            continue
        if re.match(r"^F\s+S\s*:", s) or re.match(r"^S\s+O\s*:", s) or s.startswith("L     :") or re.match(r"^L\s+:", s):
            cur["cont"] = False
            continue
        dm = re.match(r"^D\s+:\s*(.*)$", s)
        if dm:
            cur["desc"] = dm.group(1)
            cur["cont"] = False
            continue
        if cur["cont"]:
            # continuation of asset name and/or amount upper bound, e.g. "Common Stock (BE) [ST] $5,000,000"
            mm = re.search(r"(\$[\d,]+)\s*$", s)
            if mm and cur["amount_text"].rstrip().endswith("-"):
                cur["amount_text"] += " " + mm.group(1)
                s = s[:mm.start()].strip()
            cur["asset"] += " " + s
    flush()
    out = []
    for r in recs:
        asset = re.sub(r"\s+", " ", r["asset"]).strip()
        asset = re.sub(r"\(([A-Z0-9.\-]+)\s+([A-Z0-9.\-]+)\)", r"(\1\2)", asset)   # ticker wrapped across lines
        tk = TICKER.findall(asset)
        at = ATYPE.findall(asset)
        lo, hi = _amount(r["amount_text"])
        out.append({"owner": OWNER.get(r["owner"], r["owner"]), "asset": re.sub(r"\s*\[[A-Z]{2}\]\s*$", "", asset),
                    "ticker": tk[-1] if tk else None, "asset_type": at[-1] if at else None,
                    "type": TYPE.get(r["type"], r["type"]),
                    "date": datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(),
                    "notified": datetime.strptime(r["notified"], "%m/%d/%Y").date().isoformat(),
                    "amount_low": lo, "amount_high": hi, "desc": r["desc"][:300]})
    return out


def parse_ptr_pdf(data: bytes) -> list[dict]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return parse_ptr_text(text)
