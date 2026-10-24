"""Percentile scoring (section 3, "Scoring method").

- Each metric -> percentile within the stock's GICS sector (direction-adjusted, so 100 = best).
  Fewer than `min_sector_peers` peers with a value -> ranked against the whole universe instead
  (and labelled so).
- Valuation ratios blend sector percentile with own-history percentile (config weights).
- Category = mean of available metric percentiles -> 1-10 by decile.
- Overall = simple mean of available category scores, one decimal. N/A categories excluded and listed.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

CATEGORIES = ["Valuation", "Profitability", "Financial Health", "Growth", "Dividends", "Management", "Analyst"]


def resolve_sets(cfg: dict) -> dict[str, dict[str, list[str]]]:
    sets = {}
    for name, body in cfg["metric_sets"].items():
        sets[name] = cfg["metric_sets"][body["inherit"]] if "inherit" in body else body
    return sets


def decile(p: float) -> int:
    return 1 + min(9, int(p // 10))


class Ranker:
    def __init__(self, rows: list[dict], directions: dict[str, int], min_peers: int) -> None:
        self.dir = directions
        self.min_peers = min_peers
        self.by_sector: dict[tuple[str, str], list[float]] = {}
        self.all: dict[str, list[float]] = {}
        for r in rows:
            for k, v in r["metrics"].items():
                if v is None or k not in directions:
                    continue
                self.by_sector.setdefault((k, r["sector"]), []).append(v)
                self.all.setdefault(k, []).append(v)
        for d in (self.by_sector, self.all):
            for k in d:
                d[k].sort()

    def pct(self, k: str, sector: str, v: float) -> tuple[float, str, int]:
        vals = self.by_sector.get((k, sector), [])
        basis = "sector"
        if len(vals) < self.min_peers:
            vals, basis = self.all.get(k, []), "universe"
        n = len(vals)
        if n == 0:
            return 50.0, basis, 0
        lo, hi = bisect_left(vals, v), bisect_right(vals, v)
        p = (lo + 0.5 * (hi - lo)) / n * 100        # share of peers below (ties count half)
        if self.dir.get(k, 1) < 0:
            p = 100 - p
        return p, basis, n


def score_rows(rows: list[dict], cfg: dict, score_only: set[str] | None = None) -> dict[str, dict]:
    """rows: {ticker, sector, set, metrics{}, own_pct{}, na{}, pays_dividend, has_analyst}.
    Returns ticker -> score payload (only for tickers in score_only, if given)."""
    sets = resolve_sets(cfg)
    ranker = Ranker(rows, cfg["directions"], cfg["min_sector_peers"])
    bl = cfg["valuation_blend"]
    own_metrics = set(cfg["valuation_metrics_with_own_history"])
    out = {}
    for r in rows:
        if score_only is not None and r["ticker"] not in score_only:
            continue
        mset = sets[r["set"]]
        cats, excluded = {}, []
        for cat in CATEGORIES:
            keys = mset.get(cat, [])
            detail, pcts = [], []
            for k in keys:
                v = r["metrics"].get(k)
                item = {"key": k, "label": cfg["labels"].get(k, k), "value": v}
                if v is None:
                    item["na"] = r["na"].get(k, "not available")
                    detail.append(item)
                    continue
                p, basis, n = ranker.pct(k, r["sector"], v)
                item.update(pct_peer=round(p, 1), basis=basis, peers=n)
                if cat == "Valuation" and k in own_metrics:
                    op = r.get("own_pct", {}).get(k)
                    if op is not None:
                        item["pct_own"] = round(op, 1)
                        p = bl["sector_weight"] * p + bl["own_history_weight"] * op
                    else:
                        item["own_na"] = r.get("own_na", {}).get(k, "less than 1 year of history")
                item["pct"] = round(p, 1)
                pcts.append(p)
                detail.append(item)
            reason = None
            if cat == "Dividends" and not r.get("pays_dividend"):
                reason = "Doesn't pay a dividend"
            elif cat == "Analyst" and not r.get("has_analyst"):
                reason = "No analyst coverage data"
            elif len(pcts) < min(2, len(keys)):
                reason = "Not enough data"
            if reason:
                cats[cat] = {"score": None, "na": reason, "metrics": detail}
                excluded.append(cat)
            else:
                avg = sum(pcts) / len(pcts)
                cats[cat] = {"score": decile(avg), "avg_pct": round(avg, 1), "metrics": detail,
                             "n_used": len(pcts), "n_total": len(keys)}
        avail = [c["score"] for c in cats.values() if c["score"] is not None]
        out[r["ticker"]] = {"categories": cats,
                            "overall": round(sum(avail) / len(avail), 1) if avail else None,
                            "excluded": excluded}
    return out


def own_history_pct(series: list[float], current: float, lower_is_better: bool = True) -> float | None:
    """Percentile of the current ratio within its own history (100 = cheapest it has been)."""
    vals = sorted(v for v in series if v is not None and v > 0)
    if not vals or current is None or current <= 0:
        return None
    lo, hi = bisect_left(vals, current), bisect_right(vals, current)
    below = (lo + 0.5 * (hi - lo)) / len(vals) * 100
    return 100 - below if lower_is_better else below
