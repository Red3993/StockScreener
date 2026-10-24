"""Metric formulas (section 3). Inputs come from xbrl.CompanyFacts; outputs are raw metric
values plus an N/A reason for anything that can't be computed. No fabricated values: if an
input is missing, the metric is None and the reason says why."""
from __future__ import annotations

from datetime import date, timedelta

from .xbrl import CompanyFacts, nearest


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _cagr(now, then, years):
    if now is None or then is None or then <= 0 or now <= 0:
        return None
    return (now / then) ** (1 / years) - 1


def metric_set_for(sic: int | None, routing: list[dict]) -> str:
    if sic:
        for r in routing:
            for lo, hi in r["ranges"]:
                if lo <= sic <= hi:
                    return r["set"]
    return "default"


class Fundamentals:
    """All derived fundamentals for one company as of a date."""

    def __init__(self, cf: CompanyFacts, concepts: dict, asof: date, settings: dict) -> None:
        self.cf, self.c, self.asof, self.s = cf, concepts, asof, settings
        self.inputs: dict[str, float | None] = {}
        self.periods: dict[str, str] = {}
        self.na: dict[str, str] = {}
        self._load()

    # ---------------------------------------------------------------- inputs
    def _tags(self, k):
        return self.c[k]["tags"], self.c[k]["unit"]

    def ttm(self, k):
        t, u = self._tags(k)
        r = self.cf.ttm(t, u, self.asof)
        if r:
            self.periods[k] = str(r[1])
        return r[0] if r else None

    def inst(self, k):
        t, u = self._tags(k)
        r = self.cf.instant(t, u, self.asof)
        if r:
            self.periods[k] = str(r[1])
        return r[0] if r else None

    def ttm_series(self, k):
        t, u = self._tags(k)
        return self.cf.ttm_series(t, u, self.asof)

    def _pick_revenue_tag(self) -> None:
        """Companies often tag a partial revenue line (e.g. contract revenue only) alongside the
        total. Among revenue tags whose latest TTM is current, use the largest as primary for this
        company, so the per-period merge prefers the total. Banks: also consider net interest
        income + noninterest income ("total net revenue")."""
        tags, unit = self.c["revenue"]["tags"], self.c["revenue"]["unit"]
        best, best_end = None, None
        cands = []
        for t in tags:
            r = self.cf.ttm([t], unit, self.asof)
            if r:
                cands.append((r[1], r[0], t))
        if cands:
            latest = max(e for e, _, _ in cands)
            cur = [(v, t) for e, v, t in cands if (latest - e).days <= 100]
            best = max(cur)[1]
        if best:
            self.c = dict(self.c)
            self.c["revenue"] = {**self.c["revenue"], "tags": [best] + [t for t in tags if t != best]}

    def _load(self) -> None:
        self._pick_revenue_tag()
        g = self.inputs
        for k in ["revenue", "cost_of_revenue", "gross_profit", "operating_income", "pretax_income", "income_tax",
                  "net_income", "ocf", "capex", "da", "interest_expense", "dividends_paid", "dps",
                  "property_sale_gain", "noninterest_expense", "noninterest_income", "net_interest_income"]:
            g[k] = self.ttm(k)
        for k in ["total_assets", "current_assets", "current_liabilities", "total_liabilities", "liabilities_and_equity",
                  "equity", "retained_earnings", "cash", "st_investments", "lt_debt_total", "lt_debt_noncurrent",
                  "lt_debt_current", "st_borrowings", "goodwill", "intangibles"]:
            g[k] = self.inst(k)
        if g["net_interest_income"] is not None and g["noninterest_income"] is not None:
            total_net_rev = g["net_interest_income"] + g["noninterest_income"]
            if g["revenue"] is None or total_net_rev > g["revenue"] * 1.15:
                g["revenue"] = total_net_rev
                self.periods["revenue"] = self.periods.get("net_interest_income", self.periods.get("revenue", ""))
                self.revenue_note = "net interest income + noninterest income"
        if g["gross_profit"] is None and g["revenue"] is not None and g["cost_of_revenue"] is not None:
            g["gross_profit"] = g["revenue"] - g["cost_of_revenue"]
        if g["total_liabilities"] is None and g["liabilities_and_equity"] is not None and g["equity"] is not None:
            g["total_liabilities"] = g["liabilities_and_equity"] - g["equity"]
        if g["lt_debt_total"] is not None:
            debt = g["lt_debt_total"]
        elif g["lt_debt_noncurrent"] is not None or g["lt_debt_current"] is not None:
            debt = (g["lt_debt_noncurrent"] or 0) + (g["lt_debt_current"] or 0)
        else:
            debt = 0.0 if g["equity"] is not None else None   # no debt tags at all -> treated as debt-free
        g["debt"] = None if debt is None else debt + (g["st_borrowings"] or 0)
        g["cash_total"] = None if g["cash"] is None else g["cash"] + (g["st_investments"] or 0)
        g["fcf"] = None if g["ocf"] is None else g["ocf"] - abs(g["capex"] or 0)
        if g["operating_income"] is not None and g["da"] is not None:
            g["ebitda"] = g["operating_income"] + g["da"]
        else:
            g["ebitda"] = None
        if g["net_income"] is not None and g["da"] is not None:
            g["ffo"] = g["net_income"] + g["da"] - (g["property_sale_gain"] or 0)
        else:
            g["ffo"] = None
        tr = _div(g["income_tax"], g["pretax_income"])
        g["tax_rate"] = tr if tr is not None and 0 <= tr <= 0.5 else self.s["tax_rate_default"]
        self.shares_chain = self.cf.annual_shares_chain(self.asof)
        so = self.cf.shares_outstanding(self.asof)
        g["shares"] = so[0] if so else None
        self.shares_source = so[2] if so else None
        # latest reported period for staleness display
        self.latest_period = max(self.periods.values()) if self.periods else None

    # ---------------------------------------------------------------- per-share history
    def _eps_series(self):
        """TTM EPS by quarter end = TTM net income / split-consistent diluted shares (nearest fiscal year)."""
        ni = self.ttm_series("net_income")
        out = {}
        for d, v in ni.items():
            sh = nearest(self.shares_chain, d, tol_days=200)
            if sh:
                out[d] = v / sh
        return out

    def _growth(self, series: dict, years: int):
        if not series:
            return None
        end = max(series)
        if (self.asof - end).days > 250:
            return None
        then = nearest(series, end - timedelta(days=round(365.25 * years)), tol_days=45)
        now = series[end]
        if years == 1:
            if then is None or then <= 0 or now is None:
                return None
            return now / then - 1
        return _cagr(now, then, years)

    # ---------------------------------------------------------------- metrics
    def compute(self, mcap: float | None, extra: dict | None = None) -> dict[str, float | None]:
        g, m, na = self.inputs, {}, self.na
        extra = extra or {}
        ev = None if mcap is None or g["debt"] is None or g["cash_total"] is None else mcap + g["debt"] - g["cash_total"]
        tangible = None if g["equity"] is None else g["equity"] - (g["goodwill"] or 0) - (g["intangibles"] or 0)

        def pos_ratio(key, num, den, reason_neg):
            if num is None or den is None:
                m[key] = None; na[key] = "input not reported in filings"
            elif den <= 0:
                m[key] = None; na[key] = reason_neg
            else:
                m[key] = num / den

        pos_ratio("pe", mcap, g["net_income"], "negative earnings")
        pos_ratio("pfcf", mcap, g["fcf"], "negative free cash flow")
        pos_ratio("ps", mcap, g["revenue"], "no revenue")
        pos_ratio("ev_ebitda", ev, g["ebitda"], "negative EBITDA")
        pos_ratio("pb", mcap, g["equity"], "negative book value")
        pos_ratio("ptbv", mcap, tangible, "negative tangible book value")
        pos_ratio("pffo", mcap, g["ffo"], "negative FFO")

        m["gross_margin"] = _div(g["gross_profit"], g["revenue"])
        m["operating_margin"] = _div(g["operating_income"], g["revenue"])
        m["net_margin"] = _div(g["net_income"], g["revenue"])
        m["fcf_margin"] = _div(g["fcf"], g["revenue"])
        m["roe"] = _div(g["net_income"], g["equity"]) if (g["equity"] or 0) > 0 else None
        m["roa"] = _div(g["net_income"], g["total_assets"])
        invested = None if g["debt"] is None or g["equity"] is None else g["debt"] + g["equity"] - (g["cash_total"] or 0)
        if g["operating_income"] is not None and invested and invested > 0:
            m["roic"] = g["operating_income"] * (1 - g["tax_rate"]) / invested
        else:
            m["roic"] = None
        rev_s, op_s = self.ttm_series("revenue"), self.ttm_series("operating_income")
        if rev_s and op_s:
            e = max(set(rev_s) & set(op_s), default=None)
            if e and (self.asof - e).days <= 250:
                then = e - timedelta(days=365)
                r0, o0 = nearest(rev_s, then), nearest(op_s, then)
                m["op_margin_trend"] = (op_s[e] / rev_s[e] - o0 / r0) if r0 and o0 is not None and rev_s[e] else None
        m.setdefault("op_margin_trend", None)
        denom = None if g["net_interest_income"] is None else g["net_interest_income"] + (g["noninterest_income"] or 0)
        m["efficiency_ratio"] = _div(g["noninterest_expense"], denom)
        m["nii_to_assets"] = _div(g["net_interest_income"], g["total_assets"])

        m["current_ratio"] = _div(g["current_assets"], g["current_liabilities"])
        m["debt_to_equity"] = _div(g["debt"], g["equity"]) if (g["equity"] or 0) > 0 else None
        if (g["equity"] or 0) <= 0 and g["equity"] is not None:
            na["debt_to_equity"] = "negative equity"
        ie = abs(g["interest_expense"]) if g["interest_expense"] else None
        m["interest_coverage"] = _div(g["operating_income"], ie)
        if ie is None:
            na["interest_coverage"] = "no interest expense reported"
        ta = g["total_assets"]
        if None not in (g["current_assets"], g["current_liabilities"], g["retained_earnings"], g["operating_income"],
                        g["total_liabilities"], g["revenue"], ta, mcap) and ta and g["total_liabilities"]:
            m["altman_z"] = (1.2 * (g["current_assets"] - g["current_liabilities"]) / ta + 1.4 * g["retained_earnings"] / ta
                             + 3.3 * g["operating_income"] / ta + 0.6 * mcap / g["total_liabilities"] + 1.0 * g["revenue"] / ta)
        else:
            m["altman_z"] = None
        m["net_cash_to_mcap"] = None if mcap is None or g["cash_total"] is None or g["debt"] is None else (g["cash_total"] - g["debt"]) / mcap
        m["tangible_equity_ratio"] = _div(tangible, ta)

        fcf_s = {}
        ocf_s, cap_s = self.ttm_series("ocf"), self.ttm_series("capex")
        for d, v in ocf_s.items():
            if d in cap_s:
                fcf_s[d] = v - abs(cap_s[d])
        eps_s = self._eps_series()
        ffo_s = {}
        ni_s, da_s, gain_s = self.ttm_series("net_income"), self.ttm_series("da"), self.ttm_series("property_sale_gain")
        for d, v in ni_s.items():
            if d in da_s:
                ffo_s[d] = v + da_s[d] - gain_s.get(d, 0)
        for y in (1, 3, 5):
            m[f"rev_growth_{y}y"] = self._growth(rev_s, y)
            m[f"eps_growth_{y}y"] = self._growth(eps_s, y)
        for y in (1, 3):
            m[f"fcf_growth_{y}y"] = self._growth(fcf_s, y)
            m[f"ffo_growth_{y}y"] = self._growth(ffo_s, y)
        t, u = self._tags("revenue")
        q = self.cf.quarters(t, u, self.asof)
        ends = sorted(q)[-12:]
        yoy = [q[e] > v0 for e in ends if (v0 := nearest(q, e - timedelta(days=364), tol_days=20)) is not None]
        m["growth_consistency"] = (sum(yoy) / len(yoy)) if len(yoy) >= 4 else None
        e3 = m["eps_growth_3y"]
        m["peg"] = (m["pe"] / (e3 * 100)) if m["pe"] and e3 and e3 > 0 else None
        if m["peg"] is None:
            na["peg"] = "needs positive P/E and positive 3y EPS growth"

        div = abs(g["dividends_paid"]) if g["dividends_paid"] else None
        if not div and g["dps"] and g["shares"]:
            div = g["dps"] * g["shares"]
        self.pays_dividend = bool(div)
        if div:
            m["div_yield"] = _div(div, mcap)
            m["payout_earnings"] = _div(div, g["net_income"]) if (g["net_income"] or 0) > 0 else None
            m["payout_fcf"] = _div(div, g["fcf"]) if (g["fcf"] or 0) > 0 else None
            m["payout_ffo"] = _div(div, g["ffo"]) if (g["ffo"] or 0) > 0 else None
            m["div_fcf_coverage"] = _div(g["fcf"], div)
            t, u = self._tags("dividends_paid")
            ann = self.cf.annuals(t, u, self.asof)
            per_share = {}
            for d, v in ann.items():
                sh = nearest(self.shares_chain, d, tol_days=60)
                if sh:
                    per_share[d] = abs(v) / sh
            streak, pts = 0, sorted(per_share.items(), reverse=True)
            for (d1, v1), (d0, v0) in zip(pts, pts[1:]):
                if (d1 - d0).days > 400 or v1 <= v0 * 1.001:
                    break
                streak += 1
            m["div_growth_streak"] = float(streak) if len(pts) >= 2 else None
        else:
            for k in ["div_yield", "payout_earnings", "payout_fcf", "payout_ffo", "div_growth_streak", "div_fcf_coverage"]:
                m[k] = None; na[k] = "does not pay a dividend"

        sc = self.shares_chain
        if sc:
            e = max(sc)
            for y in (1, 3):
                then = nearest(sc, e - timedelta(days=round(365.25 * y)), tol_days=60)
                m[f"share_change_{y}y"] = (sc[e] / then - 1) if then and (self.asof - e).days < 500 else None
        else:
            m["share_change_1y"] = m["share_change_3y"] = None
        m["roic_minus_coc"] = None if m["roic"] is None else m["roic"] - self.s["cost_of_capital"]
        m.update(extra)
        for k, v in m.items():
            if v is None and k not in na:
                na[k] = "input not reported in filings"
        self.metrics = m
        return m
