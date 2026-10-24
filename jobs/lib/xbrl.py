"""XBRL fundamentals normalization (section 3, "TTM rules").

Rules (also in CLAUDE.md):
- Flow items: TTM = sum of the last four discrete quarters. Discrete quarters come from
  direct 3-month facts when reported, otherwise from year-to-date differences
  (Q2 = 6M - 3M, Q3 = 9M - 6M, Q4 = FY - 9M). Cash-flow statements are YTD-only in 10-Qs,
  so this matters for OCF, capex and dividends.
- Balance-sheet items: most recent point-in-time value, never summed.
- Duplicates/restatements: for each period, the most recently *filed* value as of `asof` wins.
- Fiscal years: everything is keyed by period start/end dates, never by fiscal labels.
- Concept fallbacks: ordered tag lists are merged per period by priority.
- No look-ahead: every query takes `asof` and ignores facts filed after it.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

Q_MIN, Q_MAX = 75, 105       # a "quarter" duration, in days
Y_MIN, Y_MAX = 340, 380      # a "year" duration (52/53-week years included)
SPLIT_RATIOS = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 8, 1 / 10, 1 / 20]


def D(s: str) -> date:
    return date.fromisoformat(s)


@dataclass(frozen=True)
class Fact:
    start: date | None
    end: date
    val: float
    filed: date
    accn: str
    form: str


class CompanyFacts:
    """Wraps one companyfacts JSON (already parsed)."""

    def __init__(self, cf: dict, predecessors: list[dict] | None = None) -> None:
        """`predecessors`: companyfacts of earlier entities (e.g. after a holding-company
        reorganization the ticker maps to a new CIK with no history). Their facts are appended;
        per-period dedup by latest filed date still applies."""
        self.cik = cf.get("cik")
        self.name = cf.get("entityName")
        self._ns = {ns: {t: {"units": {u: list(v) for u, v in c.get("units", {}).items()}} for t, c in tags.items()}
                    for ns, tags in cf.get("facts", {}).items()}
        for p in predecessors or []:
            for ns, tags in p.get("facts", {}).items():
                for t, c in tags.items():
                    dst = self._ns.setdefault(ns, {}).setdefault(t, {"units": {}})["units"]
                    for u, v in c.get("units", {}).items():
                        dst.setdefault(u, []).extend(v)
        self._cache: dict[tuple, list[Fact]] = {}

    def n_concepts(self) -> int:
        return len(self._ns.get("us-gaap", {}))

    def facts(self, tag: str, unit: str, ns: str = "us-gaap") -> list[Fact]:
        key = (ns, tag, unit)
        if key not in self._cache:
            out = []
            for x in self._ns.get(ns, {}).get(tag, {}).get("units", {}).get(unit, []):
                try:
                    out.append(Fact(D(x["start"]) if "start" in x else None, D(x["end"]), float(x["val"]),
                                    D(x["filed"]), x.get("accn", ""), x.get("form", "")))
                except (KeyError, ValueError, TypeError):
                    continue
            self._cache[key] = out
        return self._cache[key]

    # ---------------------------------------------------------------- merging
    def _merged(self, tags: Iterable[str], unit: str, asof: date, duration: bool) -> dict:
        """period key -> Fact. Key = (start, end) for durations, end for instants.
        Within a tag: latest filed as of `asof`. Across tags: first tag in priority order wins."""
        merged: dict = {}
        for tag in tags:
            best: dict = {}
            for f in self.facts(tag, unit):
                if f.filed > asof or (duration and f.start is None) or (not duration and f.start is not None):
                    continue
                k = (f.start, f.end) if duration else f.end
                if k not in best or f.filed > best[k].filed:
                    best[k] = f
            for k, f in best.items():
                merged.setdefault(k, f)
        return merged

    # ---------------------------------------------------------------- flows
    def quarters(self, tags: Iterable[str], unit: str, asof: date) -> dict[date, float]:
        """Discrete quarterly values keyed by quarter end date (direct facts first, then YTD differences)."""
        m = self._merged(tags, unit, asof, duration=True)
        direct: dict[date, float] = {}
        by_start: dict[date, list[tuple[date, float]]] = {}
        for (s, e), f in m.items():
            days = (e - s).days
            if Q_MIN <= days <= Q_MAX:
                direct.setdefault(e, f.val)
            if Q_MIN <= days <= Y_MAX:
                by_start.setdefault(s, []).append((e, f.val))
        q = dict(direct)
        for s, pts in by_start.items():
            pts.sort()
            # only treat as a YTD chain if it has a cumulative (>= ~6 month) point
            if not any((e - s).days > Q_MAX for e, _ in pts):
                continue
            for (e0, v0), (e1, v1) in zip(pts, pts[1:]):
                if Q_MIN <= (e1 - e0).days <= Q_MAX and e1 not in q:
                    q[e1] = v1 - v0
        return dict(sorted(q.items()))

    def annuals(self, tags: Iterable[str], unit: str, asof: date) -> dict[date, float]:
        m = self._merged(tags, unit, asof, duration=True)
        out = {e: f.val for (s, e), f in m.items() if Y_MIN <= (e - s).days <= Y_MAX}
        return dict(sorted(out.items()))

    def ttm_series(self, tags: Iterable[str], unit: str, asof: date) -> dict[date, float]:
        """TTM value at each quarter end where the last 4 quarters are consecutive; annual facts fill gaps."""
        q = list(self.quarters(tags, unit, asof).items())
        out: dict[date, float] = {}
        for i in range(3, len(q)):
            window = q[i - 3:i + 1]
            if all(Q_MIN <= (b[0] - a[0]).days <= Q_MAX for a, b in zip(window, window[1:])):
                out[q[i][0]] = sum(v for _, v in window)
        for e, v in self.annuals(tags, unit, asof).items():
            out.setdefault(e, v)
        return dict(sorted(out.items()))

    def ttm(self, tags: Iterable[str], unit: str, asof: date, max_age_days: int = 250) -> tuple[float, date] | None:
        s = self.ttm_series(tags, unit, asof)
        if not s:
            return None
        e = max(s)
        if (asof - e).days > max_age_days:
            return None
        return s[e], e

    def latest_quarter_value(self, tags: Iterable[str], unit: str, asof: date) -> dict[date, float]:
        """For 'average' items (weighted shares): direct quarter values, plus annual values as fallback."""
        m = self._merged(tags, unit, asof, duration=True)
        out: dict[date, float] = {}
        for (s, e), f in m.items():
            if Q_MIN <= (e - s).days <= Q_MAX:
                out[e] = f.val
        for (s, e), f in m.items():
            if Y_MIN <= (e - s).days <= Y_MAX:
                out.setdefault(e, f.val)
        return dict(sorted(out.items()))

    # ---------------------------------------------------------------- instants
    def instants(self, tags: Iterable[str], unit: str, asof: date) -> dict[date, float]:
        m = self._merged(tags, unit, asof, duration=False)
        return dict(sorted((e, f.val) for e, f in m.items()))

    def instant(self, tags: Iterable[str], unit: str, asof: date, max_age_days: int = 250) -> tuple[float, date] | None:
        s = self.instants(tags, unit, asof)
        if not s:
            return None
        e = max(s)
        if (asof - e).days > max_age_days:
            return None
        return s[e], e

    # ---------------------------------------------------------------- shares
    def annual_shares_chain(self, asof: date, tags=("WeightedAverageNumberOfDilutedSharesOutstanding",
                                                     "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
                                                     "WeightedAverageNumberOfSharesOutstandingBasic")) -> dict[date, float]:
        """Split-consistent fiscal-year diluted share counts, in current share units.

        Old filings are never restated for later splits, so a raw series jumps at every split.
        Each 10-K, though, is internally consistent across the 2-3 years it reports. We start
        from the newest 10-K and walk back, scaling each older filing by the ratio on a fiscal
        year it shares with the already-adjusted series. No split table needed."""
        for tag in tags:
            by_accn: dict[str, dict[date, float]] = {}
            filed: dict[str, date] = {}
            for f in self.facts(tag, "shares"):
                if f.filed > asof or f.start is None or not (Y_MIN <= (f.end - f.start).days <= Y_MAX):
                    continue
                by_accn.setdefault(f.accn, {})[f.end] = f.val
                filed[f.accn] = max(filed.get(f.accn, f.filed), f.filed)
            if not by_accn:
                continue
            order = sorted(by_accn, key=lambda a: filed[a], reverse=True)
            out: dict[date, float] = dict(by_accn[order[0]])
            for a in order[1:]:
                vals = by_accn[a]
                overlap = [e for e in vals if e in out and vals[e] > 0]
                if not overlap:
                    continue
                e0 = max(overlap)
                k = out[e0] / vals[e0]
                for e, v in vals.items():
                    out.setdefault(e, v * k)
            return dict(sorted(out.items()))
        return {}

    def shares_outstanding(self, asof: date) -> tuple[float, date, str] | None:
        """Best current share count. Candidates: dei cover-page (summed across share classes
        reported under one accession), us-gaap CommonStockSharesOutstanding, latest diluted
        weighted average. Most recent end date wins; cover page preferred on near-ties."""
        cands: list[tuple[date, float, str]] = []
        by_accn: dict[str, list[Fact]] = {}
        for f in self.facts("EntityCommonStockSharesOutstanding", "shares", ns="dei"):
            if f.filed <= asof:
                by_accn.setdefault(f.accn, []).append(f)
        if by_accn:
            accn = max(by_accn, key=lambda a: max(f.filed for f in by_accn[a]))
            fs = by_accn[accn]
            e = max(f.end for f in fs)
            if (asof - e).days < 540:   # some filers stopped tagging the cover page years ago
                cands.append((e + timedelta(days=30), sum(f.val for f in fs if f.end == e), "cover page (dei)"))
        cs = self.instants(["CommonStockSharesOutstanding"], "shares", asof)
        if cs:
            e = max(cs)
            cands.append((e, cs[e], "balance sheet"))
        wd = self.latest_quarter_value(["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares", asof)
        if wd:
            e = max(wd)
            cands.append((e - timedelta(days=30), wd[e], "diluted weighted avg"))
        if not cands:
            return None
        e, v, src = max(cands, key=lambda c: c[0])
        return v, e, src


# ---------------------------------------------------------------- split handling
def detect_splits(series: dict[date, float]) -> list[tuple[date, float]]:
    """Find stock splits from jumps in a share-count series. Returns (date, ratio) where
    ratio = new/old (2.0 for a 2-for-1 split, 0.1 for 1-for-10 reverse)."""
    pts = sorted(series.items())
    out = []
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if v0 <= 0 or v1 <= 0:
            continue
        r = v1 / v0
        if 0.6 < r < 1.6:
            continue
        best = min(SPLIT_RATIOS, key=lambda s: abs(r / s - 1))
        if abs(r / best - 1) < 0.12:
            out.append((d1, best))
    return out


def split_factor_after(splits: list[tuple[date, float]], d: date, until: date | None = None) -> float:
    """Product of split ratios after date d (and up to `until`)."""
    f = 1.0
    for sd, r in splits:
        if sd > d and (until is None or sd <= until):
            f *= r
    return f


def adjust_series(series: dict[date, float], splits: list[tuple[date, float]], per_share: bool) -> dict[date, float]:
    """Express a share count (or a per-share value) in today's share units."""
    out = {}
    for d, v in series.items():
        f = split_factor_after(splits, d)
        out[d] = v / f if per_share else v * f
    return out


def nearest(series: dict[date, float], target: date, tol_days: int = 45) -> float | None:
    if not series:
        return None
    keys = sorted(series)
    i = bisect_right(keys, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(keys) and abs((keys[j] - target).days) <= tol_days:
            if best is None or abs((keys[j] - target).days) < abs((best - target).days):
                best = keys[j]
    return series[best] if best else None
